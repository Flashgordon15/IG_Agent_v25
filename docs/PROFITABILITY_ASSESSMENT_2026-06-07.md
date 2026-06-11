# IG Agent v25 — Profitability Assessment & Improvements

**Date:** 7 June 2026  
**App version:** 25.5.0+ (post-improvement batch)  
**Status:** Implemented — see changelog below

---

## Executive summary

| Area | Before | After this batch |
|------|--------|------------------|
| Live WR (DB) | ~46% (31W/37L) | Measure with `profitability_report.py` |
| Replay analysis | 0% WR (label bug) | **Fixed** — `label_3` / `label_3bar` aligned |
| ML blend | 11 training records | **Skipped until 500** records |
| Correlation cap | 15/day/direction | **5/day/direction** |
| HEALTHY points | >+6 cumulative | **>+4** — faster full-size recovery |
| Partial close | Ran when disabled | **Config-gated**; enabled in config |
| US session noise | london_morning on indices | **Removed** from Wall St / Nasdaq |
| Japan threshold | 70% | **85%** (replay-aligned) |
| Stacking | `one_position_per_epic: false` | **`true`** |
| High vol | Hard block only | **Soft −10%** score penalty |

---

## Implemented changes

### P0 — Measurement

- `scripts/profitability_report.py` — per-epic WR, pts, GBP, shadow blockers
- `--reconcile` — backfill `epic` from market name; tag `legacy_pnl_suspect` rows

### P0 — Replay fix

- `scripts/analyse_replay.py` — reads `label_3` or `label_3bar`
- `scripts/replay_signals.py` — batch path writes both label keys

### P1 — Config & risk

- `config/config_v25.json` — see table above
- `correlation_guard.py` — `MAX_NEW_PER_DIRECTION = 5`

### P1 — Signal quality

- `signal_engine.py` — high vol regime: ×0.9 score (soft penalty)

### P2 — Partial close

- `config.py` — `partial_close_enabled` accessors
- `trade_manager.py` — respects config flag
- `trailing_stop.partial_close_enabled: true`

### P3 — ML & points

- `trading_loop.py` — ML blend requires ≥500 `MLTrainingStore` records
- `points_engine.py` — `HEALTHY_CUMULATIVE_MIN = 4.0`

### Tests

- `tests/test_profitability_improvements.py`

---

## Commands

```bash
# Weekly report
PYTHONPATH=src python3 scripts/profitability_report.py --days 14

# Fix DB epics + tag legacy rows
PYTHONPATH=src python3 scripts/profitability_report.py --reconcile

# Re-run replay analysis (should show non-zero WR)
PYTHONPATH=src python3 scripts/analyse_replay.py
```

---

## Still recommended (not automated)

1. **Retrain ML** after multi-market replay with fixed labels
2. **Enable ML blend** once `MLTrainingStore` ≥500 records
3. **Germany 40** — only after DEMO epic/stream verified
4. **Per-week config review** using shadow log top blockers

---

*Supersedes `PROFITABILITY_ASSESSMENT_2026-05-27.md` for operational status.*
