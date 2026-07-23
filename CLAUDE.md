# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with this repository.

## Project

**IG Agent v31.1** — DEMO Trading Desk for IG spreadbet. Python backend (FastAPI + trading loops) on `localhost:8080`, canonical desktop UI = **Quantum Terminal** (`terminal/` on `:3000`) via `Trading_Desk.app` → `scripts/trading_desk_silent.sh`.

| Doc | Role |
|-----|------|
| **`docs/DESK_DEPLOY_RUNBOOK.md`** | **Ops spine** — audit / deploy / sync-wrappers / anti-zombie |
| **`.cursor/rules/2026-07-07-trading-desk-session.mdc`** | Live desk session profile (DOW hot path, sizes, supervision) |
| `docs/V31_RUNTIME_MODE_MAP.md` | Runtime mode map (verify version labels) |
| `IG_Agent_v29.1_COMPLETE_SPEC.md` | Historical v29.1 behaviour reference |
| `docs/V29.1_ARCHITECTURE.md` | Historical module map |
| `docs/LIVE_PROMOTION_CHECKLIST.md` | Live funds gate |

Version source of truth: `src/system/identity/app_identity.py` (`APP_VERSION = 31.1.0`).

## Commands

**All Python commands require `PYTHONPATH=src`.**

```bash
# Preferred config
export IG_AGENT_CONFIG=config/config_v31_demo_throughput.json

# Run the agent
PYTHONPATH=src python3 src/main.py

# Run tests
PYTHONPATH=src python3 -m pytest tests/ -q

# Desk deploy (flat sessions only)
./scripts/desk_deploy.sh audit
./scripts/desk_deploy.sh deploy

# Unify data root (legacy src/data → IG_DATA_ROOT)
PYTHONPATH=src python3 scripts/unify_data_root.py --check
PYTHONPATH=src python3 scripts/unify_data_root.py --apply
```

**Desktop:** `Trading_Desk.app` or `Launch_Trading_Desk.command` → Quantum Terminal `:3000` (pywebview).  
**Legacy Vite dashboard** is still served from `dashboard/dist` on `:8080/` — not the product Desk UI.

```bash
cd dashboard && npm run build   # after dashboard/ JSX changes
cd terminal && npm run build    # after terminal/ changes (or start_ui_background.sh)
```

## Configuration

- **Primary overlay:** `config/config_v31_demo_throughput.json`
- Merge chain: v31_demo → v31 → v30 → v29 → v25
- Hot path (authoritative): **DOW only** until Nikkei JPY PnL certified (`dual_core.exclude_from_hot_path`)
- Broker stop at entry: `micro_risk.omit_broker_limit_at_entry: false` (requires flat deploy to load)

## Data plane (unified)

When `APP_MODE` applies, `IG_DATA_ROOT` and `IG_AGENT_DATA_DIR` both point at:

`src/data/v31-production/`

`system.paths.data_dir()` follows that tree and bridges critical files from legacy `src/data/` (learning DB symlink, trade_support status, broker_snapshot).

**Do not** treat empty stubs under `v31-production` as truth without checking the bridge / legacy tree.

## Open-position truth ranking

1. `GET /api/trade_support/status` — broker REST supervisor
2. `GET /api/positions/live` — check `verdict`, `critical`, `broker_open_sot`, not just `count`
3. `GET /api/trading_desk/liveness`
4. `GET /api/position_manager/status`
5. `GET /api/health` — process readiness only

## Supervision

| Process | Role |
|---------|------|
| `src/main.py` | Agent + API + in-process OpenPositionManager |
| `runtime.trade_support_wrapper` | Broker-authoritative open-trade supervisor |
| `runtime.desk_support_wrapper` | Out-of-process health / recovery |

Never `kill -9` main.py — use anti-zombie protocol in `.cursorrules` / `desk_deploy.sh`.

## Architecture (v31.1 desk)

- Dual-core rotation + gated `TradingLoop` (also matrix/scalp lanes — prefer gated path)
- Post-fill risk stack: GBP exit + virtual stop + dynamic trail
- Night matrix 24/7; sole scheduled block = rollover **21:58–22:05 BST**
- `RestApiBudget`: 3 non-essential REST calls/min hard cap
