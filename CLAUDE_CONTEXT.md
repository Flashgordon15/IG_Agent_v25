# IG Agent v29.1 — Claude Code Context

## Current State (June 2026)

| Field | Value |
|-------|-------|
| Branch | `main` |
| App version | **29.1.0** (`src/system/app_identity.py`) |
| Config overlay | `config/config_v29.json` |
| Mode | **DEMO** (`demo_only_deployment: true`) |
| Profile | **B** — learning demo with protective phase |
| Spec | `IG_Agent_v29.1_COMPLETE_SPEC.md` |
| Architecture | `docs/V29.1_ARCHITECTURE.md` |

## v29.1 Shipped Features

1. **Protective learning** — conf floor 62%, fitness floor 55; demo soak tightened (no ML veto bypass)
2. **Learning health** — `GET /api/learning-health`, System panel, `scripts/learning_health_report.py`
3. **Clean learning inputs** — IG-import setups excluded from stats/ML/registry
4. **Setup registry** — agent-only rebuild at startup; bans when N≥5 and bad WR
5. **Live P&L** — streaming quote refresh, FX pip math, quote-scale trust guard
6. **Daily loss v29.1** — baseline reset on startup (`v291_upgrade.py`); soft pause £400
7. **ML/shadow fixes** — missing imports repaired; shadow log on all evaluate paths
8. **FX risk** — pip-aware stops in `trade_risk.py`

## Key Config (`config/config_v29.json`)

| Setting | Value |
|---------|-------|
| `protective_learning.enabled` | true |
| `protective_learning.signal_threshold_floor` | 62 |
| `protective_learning.fitness_min_floor` | 55 |
| `demo_soak_mode.bypass_ml_veto` | false |
| `demo_soak_mode.disable_rotation_filter` | false |
| `learning_demo_mode.daily_loss_soft_pause_gbp` | 400 |
| `sentiment_guard.crowded_long_pct` | 70 |

Instrument sizes/thresholds: `config/config_v25.json`.

## Enabled Markets

Japan 225, Wall Street, US Tech 100, Gold, EUR/USD, GBP/USD (US Oil disabled pending OHLC/stream).

## Architecture Decisions (v29.1)

- Open P&L: streaming quote wins over stale IG UPL **when quote scale matches entry**
- FX P&L: price delta ÷ pip size (0.0001 majors) × size × £/pip
- Daily dashboard P&L: realized (SQLite effective) + sum open unrealized; idempotent on WS re-read
- ML training writes require `is_ig_import_setup_key` import — never call policy helpers without import
- Shadow counterfactuals: `source='shadow'` included in setup stats rebuild

## Dashboard

- URL: `http://localhost:8080`
- Rebuild: `cd dashboard && npm run build`
- New: **System → Learning Health** panel

## Restart

```bash
pkill -f "src/main.py" || true
rm -f src/data/.ig_agent_v29.lock
PYTHONPATH=src python3 src/main.py
```

Or use Desktop **IG Agent v29.0** launcher.

## Tests

```bash
PYTHONPATH=src python3 -m pytest tests/ -q   # expect 794+ pass
```

## Document Map

| File | Use when |
|------|----------|
| `IG_Agent_v29.1_COMPLETE_SPEC.md` | Operator behaviour, gates, limits |
| `docs/V29.1_ARCHITECTURE.md` | Code navigation, data flow |
| `CLAUDE.md` | Commands + conventions for AI |
| `IG_Agent_v25_COMPLETE_SPEC_v8.md` | Deep v25 history only |
