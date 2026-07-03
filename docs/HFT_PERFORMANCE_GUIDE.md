# HFT Performance Guide

Lightweight performance notes for IG Agent v31 tick-to-trade path. **No core rewrites** — prefer cached snapshots and narrow lock scope.

## Tick-to-Trade Path

```
Yahoo poller / hub publish
  → market_data_hub snapshot (per-epic, lock per epic)
  → dual_core stacked sweep (_snapshots, velocity deque)
  → signal_engine.evaluate (shadow log append)
  → risk_manager / execution routing
  → IG REST deal placement (execution-only)
```

IG REST is **not** on the signal quote path when `pricing.reference_transport` is `yahoo`.

## Lock Avoidance Patterns

| Layer | Pattern |
|-------|---------|
| HTTP APIs | Background refresh + `dict` copy (`health_light`, `multimarket_eval`, `trade_quality`) |
| Rotation read | `get_rotation_state()` uses timed lock acquire; stale snapshot flag on timeout |
| Rotation write | Pre-compute scores outside lock; append history inside lock |
| Triage DB | WAL + `busy_timeout=5000` for writers; readonly connections for dashboard |
| Iron Cage | 1s TTL in-memory cache — no external HTTP on hot path |

## O(1) Endpoints (cockpit poll safe)

- `/api/health_light`
- `/api/iron_cage_status`
- `/api/multimarket_eval`
- `/api/trade_quality`
- `/api/tuning_params`
- `/api/rotation_state` (cached rotation state; scoring precomputed in rotation lock path)
- `/api/regime_state` — Markov regime + Kalman-smoothed states per epic
- `/api/risk_state` — vol-adaptive sizing, circuit breakers L1/L2
- `/api/latency_trace` — tick-to-trade ring buffer p50/p95
- `/api/reconciliation_state` — broker vs internal drift (~1s daemon)

## Latency Trace Stages

| Stage | Hook |
|-------|------|
| `feed_hub` | `market_data_hub.publish()` |
| `decision` | `execution_engine.execute_trade()` |
| `ig_rest` | post `_execute_trade_body` |

Ring buffer: pre-allocated deque (512 slots) — no per-tick dict churn on hot path.

## Regime + Risk Integration

- **Regime engine** refreshes every 2s from 1440m OHLC window (288×5m bars)
- **Vol risk engine** applies regime size/stop factors in `RiskManager.assess()`
- **Iron Cage** blocks `trade_ready` on circuit breaker L2 or reconciliation drift

## Packet Validation

Invalid quotes (non-finite, inverted spread, >10% mid spread) dropped at hub publish — never touch engine state.

- Rotation scoring computed before stack mutation lock
- Multimarket eval reads hub snapshots only in background thread
- Dashboard polls use `cache: no-store` with DOM diff guards to prevent flicker

## What Not to Do

- Do not call IG REST from HTTP handlers
- Do not expand `trading_loop` or `signal_engine` locks for dashboard data
- Do not disable iron cage checks for tuning experiments

## Profiling

```bash
curl -s http://127.0.0.1:8080/api/health | jq '.endpoint_profile'
```

Target: `health_light` p50 < 5ms, `multimarket_eval` p50 < 3ms (snapshot copy only).
