# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

IG Agent v25 — automated CFD trading agent for the IG platform. Python backend (FastAPI + trading loop) served on `localhost:8080`, with a React/Vite dashboard in `dashboard/`.

Operational spec (shipped): `IG_Agent_v25_COMPLETE_SPEC_v8.md`  
v25→v26 strategy: `docs/V25_TO_V26_STRATEGY.md` + `docs/V26_VISION.md` (feeder + separate v26 agent)  
v26 framework: `IG_Agent_v26_FRAMEWORK.md` (£50k, multi-strategy, certification ladder)  
Live funds gate: `docs/LIVE_PROMOTION_CHECKLIST.md`

## Commands

**All Python commands require `PYTHONPATH=src`.**

```bash
# Run the agent
PYTHONPATH=src python3 src/main.py

# Run all tests
PYTHONPATH=src python3 -m pytest tests/ -x -q

# Run a single test file
PYTHONPATH=src python3 -m pytest tests/test_trading_loop.py -x -v

# Pre-flight check before a live session
PYTHONPATH=src python3 scripts/pre_flight_check.py --live

# Full E2E platform validation
PYTHONPATH=src python3 scripts/e2e_platform_validation.py
```

**Dashboard:**

```bash
cd dashboard && npm run build   # rebuild after any dashboard/ change (served from dist/ by agent)
cd dashboard && npm run dev     # dev server on :5173, proxies /api → :8080
```

After editing `dashboard/src/`, always rebuild (`npm run build`) — the agent serves `dist/` directly.

## Configuration

- Primary config: `config/config_v25.json`
- Config loader resolves: `config_v25.json` → `config_v24.json` → legacy v23 fallback
- IG credentials are loaded interactively at startup (no `.env` file); stored in memory only, never persisted to disk
- Market watch open-time calendars live in `config/market_watch/`

## State files & gotchas

- **Instance lock**: `src/data/.ig_agent_v25.lock` — remove if stale after a crash: `rm -f src/data/.ig_agent_v25.lock`
- **SQLite WAL**: `src/data/learning_db.sqlite3` uses WAL mode; `.sqlite3-wal` and `.sqlite3-shm` are normal and must not be deleted while the agent is running
- **Rate limit state**: `src/data/logs/rate_limit_state.json` — persisted backoff stage; inherited on restart. Deleting it resets escalating backoff to zero
- **Session state**: `src/data/state/` — JSON snapshots written at runtime; safe to inspect but don't edit while agent is running
- **Quote freshness**: Lightstreamer ticks expire after 45 s (`FRESH_STREAM_TICK_MAX_AGE_SEC`); `quote_source()` uses `hub.get_snapshot()` only — no REST fallback to avoid rate-limit contention
- **Order in-flight timeout**: 30 s; prevents trading-loop deadlock on missed order confirmations (see `live_executor.py`)

## Testing

- Test env flag `IG_AGENT_PYTEST=1` is set automatically by `conftest.py`
- `conftest.py` isolates `engine_log` and resets `RateLimitManager` singleton between tests — do not bypass these fixtures
- Tests mock IG REST/streaming; they do not hit live IG endpoints

## Architecture notes

- `trading_loop.py` is the main per-market loop; one thread per epic via `MarketOrchestrator`
- ML scorer blends rule-based signal with XGBoost probability; skip blend when model confidence is within ±15% of 50% (near-random)
- ML features are normalised by stop distance (`atr_ratio = atr/stop_pts`) for cross-instrument generalisation
- `RestApiBudget` enforces a hard 3-calls/min cap checked atomically; first call waits a full interval to prevent startup bursts
- `correlation_guard.py` blocks correlated entries; `drawdown_monitor.py` enforces daily loss limit (£500)

## Supervision lifecycle (launch / stop / overnight)

**Safe to Leave** = launchd bundle (`com.igagent.v25.watchdog` + `com.igagent.v25.caffeinate`). Install once: `./scripts/install_launchd.sh`

| Action | Expected behaviour |
|--------|-------------------|
| Dashboard **Stop Agent** | Agent exits cleanly; **launchd watchdog stays loaded**; `manual_stop.json` blocks auto-restart ~10 min |
| Desktop launcher | If launchd watchdog active and agent down, **wait for watchdog** — do not spawn duplicate `main.py` |
| `install_launchd.sh` | If agent already healthy, **handoff without kill**; clears `manual_stop.json` |
| Agent crash | Watchdog restarts via `start_agent_launchd.py` (unless `manual_stop` active) |

**Built-in operator (AI + runtime):**

```bash
# Supervision drift check (issues, warnings, launchd state)
PYTHONPATH=src python3 scripts/supervision_check.py

# With auto-repair of missing launchd jobs
PYTHONPATH=src python3 scripts/supervision_check.py --repair

# Full overnight readiness
./scripts/ensure_overnight_ready.sh
```

While running, `trading_health_monitor` calls `supervision_monitor.run_supervision_monitor_tick()` every 60s — logs drift to `engine.log`, Telegram on sustained issues. `/api/health` exposes `supervision_drift` and `supervision_warnings`.

**AI operators:** Before declaring "ready for overnight", run preflight + `supervision_check.py --repair`. Never bootout launchd watchdog on Stop Agent. If `overnight_armed` but launchd missing, run `install_launchd.sh` or `--repair`.
