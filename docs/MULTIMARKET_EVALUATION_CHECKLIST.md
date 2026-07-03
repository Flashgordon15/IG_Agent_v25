# Multi-Market Evaluation Checklist

Operational checklist for verifying per-market health before enabling live rotation.

## Pre-Session

- [ ] Iron Cage `trade_ready` is true (`GET /api/iron_cage_status`)
- [ ] Primary feed is Yahoo (not IG on signal path)
- [ ] All 7 night-matrix epics show `feed_health: ok` or `degraded` in `GET /api/multimarket_eval`
- [ ] Active stack epics match expected session profile (indices/metals vs forex lock)

## Per-Market Fields

| Field | Healthy | Investigate |
|-------|---------|-------------|
| `ticks_per_minute` | ≥ 3 on active stack | 0 for > 60s → feed stall |
| `z_score` | Within pierce band for Core B | Stuck at 0 with no stream |
| `vol_regime` | `compressed` or `neutral` for scalper | `expansion` → macro sentinel |
| `signals_1h` | Non-zero during active session | Zero with fresh ticks → signal path |
| `orders_open` | Matches broker ledger | Drift → lifecycle reconcile |
| `pnl_open_gbp` | Within risk budget | Large negative → exposure review |
| `feed_health` | `ok` | `offline` blocks iron cage |

## API

```bash
curl -s http://127.0.0.1:8080/api/multimarket_eval | python3 -m json.tool
```

## Escalation

1. Feed starvation → check `GET /api/data_feed_state` backoff
2. Lifecycle drift → `GET /api/trade_lifecycle` + broker positions
3. Rotation stuck → `GET /api/rotation_state` scores and history
