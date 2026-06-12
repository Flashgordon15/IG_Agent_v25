# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**IG Agent v29.1** — automated CFD trading agent for IG (DEMO deployment). Python backend (FastAPI + `MarketOrchestrator` trading loops) on `localhost:8080`, React/Vite dashboard in `dashboard/`.

| Doc | Role |
|-----|------|
| **`IG_Agent_v29.1_COMPLETE_SPEC.md`** | **Current shipped spec** — gates, learning, P&L, config |
| **`docs/V29.1_ARCHITECTURE.md`** | Module map, snapshot flow, learning plane |
| `IG_Agent_v25_COMPLETE_SPEC_v8.md` | Historical v25.5 detail |
| `IG_Agent_v26_FRAMEWORK.md` | Future multi-strategy / £50k vision (not separate agent yet) |
| `docs/LIVE_PROMOTION_CHECKLIST.md` | Live funds gate |

Version source of truth: `src/system/app_identity.py` (`APP_VERSION = 29.1.0`).

## Commands

**All Python commands require `PYTHONPATH=src`.**

```bash
# Run the agent
PYTHONPATH=src python3 src/main.py

# Run all tests (~794)
PYTHONPATH=src python3 -m pytest tests/ -q

# Run a single test file
PYTHONPATH=src python3 -m pytest tests/test_trading_loop.py -x -v

# Learning health report
PYTHONPATH=src python3 scripts/learning_health_report.py

# Pre-flight check before a live session
PYTHONPATH=src python3 scripts/pre_flight_check.py --live

# Full E2E platform validation
PYTHONPATH=src python3 scripts/e2e_platform_validation.py
```

**Dashboard:**

```bash
cd dashboard && npm run build   # rebuild after any dashboard/ change (served from dist/)
cd dashboard && npm run dev     # dev server :5173, proxies /api → :8080
```

## Configuration

- **Primary overlay:** `config/config_v29.json` (extends v25)
- **Instrument base:** `config/config_v25.json`
- Loader: `ConfigLoader` merges v29 → v25 → v24
- IG credentials loaded at startup (memory only)

## State files & gotchas

- **Instance lock:** `src/data/.ig_agent_v29.lock` — remove if stale: `rm -f src/data/.ig_agent_v29.lock`
- **SQLite WAL:** `src/data/learning_db.sqlite3` — do not delete `-wal`/`-shm` while running
- **Rate limit state:** `src/data/logs/rate_limit_state.json`
- **Runtime state:** `src/data/state/` — inspect only; don't edit while agent runs
- **Quote freshness:** Hub snapshot only for trading quotes; 45s max tick age typical
- **Order in-flight timeout:** 30s (`live_executor.py`)

## Architecture (v29.1 highlights)

- `MarketOrchestrator` → one `TradingLoop` thread per epic
- **Protective learning:** floors at 62% conf / 55 fitness when enabled (`protective_learning`)
- **Clean learning:** IG-import setups excluded (`learning_trade_policy`)
- **Open P&L:** hub quote refresh + FX pip scaling + quote-trust guard (`open_position_view.py`)
- **Daily P&L:** `realized_daily_pnl_gbp` + open unrealized; v29.1 baseline reset on startup
- **ML:** blend at ≥500 records; `meta.json` filter overrides at signal time
- **Shadow log:** appended on all `SignalEngine.evaluate()` return paths
- `RestApiBudget`: 3 REST calls/min hard cap

## Supervision lifecycle

**Safe to Leave** = launchd watchdog + caffeinate. Install: `./scripts/install_launchd.sh`

| Action | Behaviour |
|--------|-----------|
| Dashboard Stop | Clean exit; `manual_stop.json` blocks auto-restart ~10 min |
| Watchdog | Restarts unless manual stop |
| Overnight | `./scripts/ensure_overnight_ready.sh` + `supervision_check.py --repair` |

While running, `trading_health_monitor` runs supervision tick every 60s. `/api/health` exposes drift warnings.
