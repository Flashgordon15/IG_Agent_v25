# Clean launch — dual desk — 2026-07-24

## Pre-dev audit

| Check | Result |
|---|---|
| Books flat pre-stop? | **YES** — both FLAT / broker_open=0 |
| Watchdog hold? | `mark_manual_stop(source=operator_clean_launch)` then v32 stop |
| Active PIDs cleaned? | Dual TERM via `v32_runtime_start.sh stop` (no kill -9) |

## Sequence

1. Flat gate both ports — pass
2. A2 marker held active on disk
3. `mark_manual_stop` → `./scripts/v32_runtime_start.sh stop` → ports unbound
4. Purge `__pycache__` / `*.pyc` + stale session locks
5. `./scripts/v32_runtime_start.sh start` (CFD hydrate → ≥4s → SB)
6. Posture: `POST /api/stop` :8080 · `POST /api/start` :8081
7. UI :3000 already up (viewer)
8. Supervisor LaunchAgent left enabled; run-once score captured

## Posture card

| Item | Value |
|---|---|
| CFD PID (:8080) | `96866` |
| SB PID (:8081) | `97127` |
| UI PID (:3000) | `64445` |
| CFD trading_paused | **True** |
| CFD block_reason | `api_trading_paused` |
| A2 marker active | **True** (`A2_SB_ONLY`, hard_block=True) |
| Hard-block live | **Y** — valve `a2_entries_paused` + `api_trading_paused` |
| SB trading_paused | **False** (Step 2 armed) |
| Instant/micro | HARD OFF — `sb_disable_instant_micro=True` / `sb_disable_core_b_micro=True` |
| Books | CFD FLAT/0 · SB FLAT/0 |
| trade_support | CFD open=0 ok=True · SB open=0 ok=True |
| Supervisor score | **WATCH** · a2_cfd_pause PASS · cfd_paused=True sb_paused=False |
| Abort | **N** |

## Return

- CFD paused+blocked: **Y**
- SB armed: **Y**
- Flat: **Y**

_Generated 2026-07-24T16:22:17.401487+01:00_
