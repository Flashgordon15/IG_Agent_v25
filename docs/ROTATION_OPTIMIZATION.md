# Rotation Optimization

Multi-source auto-rotation ranks the night-matrix universe and maintains a 2–3 slot active stack.

## Scoring Model

Each epic receives a composite score from weighted components:

| Component | Weight (default) | Source |
|-----------|------------------|--------|
| Volatility / velocity | 0.35 | `ticks_per_minute`, velocity deque, Z-band escape |
| Spread | 0.20 | `_channel_health_ok`, spread points |
| Feed health | 0.25 | Hub quote age (ok ≤15s, degraded ≤45s) |
| Recent P&L | 0.10 | Reserved for triage closed-position hook |
| Regime | 0.10 | compressed / neutral / expansion from Z |

Weights are configurable via `GET/POST /api/tuning_params` (`rotation_weight_*` keys).

## API

```bash
curl -s http://127.0.0.1:8080/api/rotation_state
```

Response includes:

- `rotation.rotation_scores` — ranked epics with component breakdown
- `rotation.rotation_history` — last 15 stack changes with reason and score snapshot
- `rotation.active_instruments` / `eligible_instruments` / `inactive_instruments`

## Rotation Triggers

- Stagnant dead-zone (Z in quiet band + low TPM)
- Velocity ranking sweep (`rotation_sweep_count`)
- Escape hatch when all stack TPM = 0 for >60s
- Forex failover lock (EUR/USD + GBP/USD) bypasses index rotation
- **DOW entry failover** (gated, default OFF) — see [`ROTATION_FAILOVER_POLICY.md`](./ROTATION_FAILOVER_POLICY.md) for temporary SB allowlist expansion to Gold when DOW stays WAIT/low-confidence

## Tuning Tips

1. Increase `rotation_weight_feed` when Yahoo primary is flaky
2. Increase `rotation_weight_volatility` for throughput/demo sessions
3. Keep spread weight ≥ 0.15 to avoid wide-spread epics on stack
4. Review `rotation_history` after session — repeated churn indicates threshold mismatch

## Iron Cage Interaction

Rotation can run while `trade_ready` is false (warming feeds). Execution still requires iron cage pass — rotation alone does not open the order valve.
