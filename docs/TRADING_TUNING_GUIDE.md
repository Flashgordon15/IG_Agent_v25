# Trading Tuning Guide

Runtime tuning parameters are stored in `config/tuning_overlay.json` and exposed via API. **Tuning never overrides Iron Cage hard limits** (`max_daily_loss_gbp`, REST budget caps, `trade_ready` contract).

## Parameters

| Key | Default | Range | Purpose |
|-----|---------|-------|---------|
| `z_score_entry_min` | -2.0 | -5..0 | Lower pierce threshold |
| `z_score_entry_max` | 2.0 | 0..5 | Upper pierce threshold |
| `vol_filter_min_tpm` | 3.0 | 0..60 | Minimum tick velocity for eligibility |
| `risk_per_trade_gbp` | 40.0 | 1..100 | Sizing reference (not hard cap) |
| `stop_distance_points` | 45.0 | 5..200 | Default stop distance |
| `limit_distance_points` | 60.0 | 5..300 | Default limit distance |
| `trailing_sensitivity` | 1.0 | 0.1..2 | Trailing stop aggressiveness |
| `dynamic_limit_scale` | 1.0 | 0.5..3 | Dynamic limit multiplier |
| `rotation_weight_*` | see overlay | 0..1 each | Rotation scoring weights (should sum ≈ 1.0) |

## Read current params

```bash
curl -s http://127.0.0.1:8080/api/tuning_params
```

## Apply update

```bash
curl -s -X POST http://127.0.0.1:8080/api/tuning_update \
  -H 'Content-Type: application/json' \
  -d '{"params":{"vol_filter_min_tpm":5,"rotation_weight_volatility":0.4}}'
```

Invalid keys (e.g. `max_daily_loss_gbp`) are rejected with `forbidden_key`.

## Cockpit

The Neon Quant Cockpit polls tuning state indirectly via rotation scores. Use API for explicit updates until a tuning editor UI is added.

## Safety

- Changes persist to overlay file only — main `config_v31.json` unchanged
- Agent restart not required for rotation weights (read on next score refresh)
- Signal engine core thresholds remain governed by merged config chain unless overlay keys are wired in future releases
