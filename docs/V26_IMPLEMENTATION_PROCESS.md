# v26 Implementation Process

Operational checklist for delivering the [v26 Profitability Spec](../IG_Agent_v26_PROFITABILITY_SPEC.md) on a **~£10k** account with **multiple trades per day**.

---

## Prerequisites

- v25.6+ on `main` (profitability batch, ML retrain pipeline, weekly report plist)
- `learning_db.sqlite3` with reconciled epics (`profitability_report.py --reconcile`)
- ML model trained (`training_meta.json` labelled_rows ≥ 500)

---

## Milestone ladder (do not skip)

| Milestone | Daily target | Min median 14d | Advance when |
|-----------|--------------|----------------|--------------|
| **M0** | Measure only | — | Segmented reports reliable 14d |
| **M1** | £100 | £100 | `E£ ≥ £12`, `PF ≥ 1.2`, `WR ≥ 50%` |
| **M2** | £250 | £250 | `E£ ≥ £25`, `PF ≥ 1.3`, `N ≥ 8/day` |
| **M3** | £500 | £500 | `E£ ≥ £42`, `PF ≥ 1.4`, `N ≥ 10/day` |
| **M4** | £1,000 | £500 median* | `E£ ≥ £65` on best-book days |

\*M4 **median** £500 is the sustainable bar; £1,000 is **target best days**, not a daily guarantee on £10k.

---

## Phase 0 — Measure (v26.0)

**Goal:** Expectancy visible per setup; no new trading logic.

| # | Task | Owner | Done when |
|---|------|-------|-----------|
| 0.1 | Implement `expectancy_engine.py` (read-only) | Dev | Unit tests green |
| 0.2 | Extend `profitability_report.py --expectancy --milestones` | Dev | JSON + markdown output |
| 0.3 | Write `expectancy_snapshot.json` daily | Dev | Dashboard can read |
| 0.4 | PROFIT tab skeleton (milestone + setup table) | Dev | Shows live snapshot |
| 0.5 | Run 14d baseline collection | Operator | Baseline pack archived |

**Weekly commands:**

```bash
PYTHONPATH=src python3 scripts/profitability_report.py --days 14 --expectancy --milestones
PYTHONPATH=src python3 -m pytest tests/test_v26_expectancy_engine.py -q
```

**Exit gate:** 14 consecutive days of segmented reports without manual DB fixes.

---

## Phase 1 — Edge (v26.1)

**Goal:** Stop trading proven losers; ML veto on marginal prob.

| # | Task | Done when |
|---|------|-----------|
| 1.1 | `setup_registry.json` + BAN/SUSPEND/PROBE/ACTIVE | Gate `expectancy_ok` live |
| 1.2 | `ml_veto` config + gate 6b | Blocks when prob < threshold |
| 1.3 | `config/config_v26.json` skeleton | Validator accepts new blocks |
| 1.4 | Ban bottom setups from baseline | WR < 45%, N ≥ 20 |
| 1.5 | Japan 85% / US overlap-only unchanged | Confirmed in registry |

**Exit gate:** **M1** metrics held 14 trading days.

---

## Phase 2 — Frequency (v26.2)

**Goal:** More qualified trades without diluting edge.

| # | Task | Done when |
|---|------|-----------|
| 2.1 | `shadow_expectancy.py` — counterfactual on blocks | Top blockers ranked by £ |
| 2.2 | Relax gates that block **positive** shadow E£ only | Documented in weekly pack |
| 2.3 | Enable **one** new epic via checklist (Section 7 of spec) | 7d PROBE band |
| 2.4 | Per-epic trail tuning from replay MFE/MAE | `capture_ratio` tracked |

**Exit gate:** **M2** metrics held 14 trading days; `N_day` median ≥ 8.

---

## Phase 3 — Capital (v26.3)

**Goal:** Deploy £10k across multiple positions safely.

| # | Task | Done when |
|---|------|-----------|
| 3.1 | `capital_budget.py` — concurrent + daily deployed caps | Gate 5c live |
| 3.2 | Risk bands probe/core/conviction on ACTIVE setups | Allocator respects bands |
| 3.3 | Correlation guard uses £ heat | Not count-only |
| 3.4 | `capital_envelope` in config | max_concurrent_risk £1,200 |

**Exit gate:** **M3** metrics held 14 trading days.

---

## Phase 4 — Intelligence (v26.4)

**Goal:** Regime awareness; per-epic ML thresholds.

| # | Task | Done when |
|---|------|-----------|
| 4.1 | `regime_filter.py` + `config/calendar.json` | High-impact blocks work |
| 4.2 | `ml_model/thresholds.json` walk-forward | Per-epic veto thresholds |
| 4.3 | Second epic enable if checklist passes | 6+ epics target |
| 4.4 | Optional: flatten losers only at session end | A/B in replay |

**Exit gate:** M4 **best day** ≥ £1,000 observed; 14d median ≥ £500.

---

## Phase 5 — Autonomy (v26.5)

**Goal:** Weekly AI proposals; operator approves in dashboard.

| # | Task | Done when |
|---|------|-----------|
| 5.1 | Auto `weekly_v26_pack.md` | Sunday launchd |
| 5.2 | Dashboard approve/reject setup changes | Audit log written |
| 5.3 | Bounded auto-tune (threshold ±2%, band caps) | Within config guardrails |

---

## Daily operator checklist (5 min)

1. Open dashboard **PROFIT** tab — confirm milestone `current` and running E£.  
2. If daily loss > £400 → no manual threshold cuts; review setups only.  
3. If STOP latched → run `profitability_report.py --days 3`; identify epic/setup.  
4. End of day: note `N`, net £, friction — compare to milestone path.

---

## Sunday operator checklist (30 min)

1. Run full weekly command suite (spec Section 8.2).  
2. Read `docs/weekly/YYYY-MM-DD_v26_pack.md`.  
3. Approve setup status changes (ban/probe/active).  
4. Only then edit `config_v26.json` or instrument blocks.  
5. Restart agent if config changed; run `pre_flight_check.py --live`.

---

## Stop rules (capital preservation)

| Condition | Action |
|-----------|--------|
| Daily loss ≥ £500 | Halt (existing v25 gate) |
| 14d `E£_portfolio` < 0 | Drop to previous milestone; re-ban setups |
| Live WR 5% below replay | Pause ML veto; retrain; probe band only |
| Friction > 30% of gross wins | Widen spread gates; review session windows |

---

## Files created by v26 (reference)

| Path | Phase |
|------|-------|
| `IG_Agent_v26_PROFITABILITY_SPEC.md` | — |
| `config/config_v26.json` | 1 |
| `src/trading/expectancy_engine.py` | 0 |
| `src/system/setup_registry.py` | 1 |
| `src/execution/capital_budget.py` | 3 |
| `src/signals/regime_filter.py` | 4 |
| `scripts/shadow_expectancy.py` | 2 |
| `tests/test_v26_*.py` | per phase |

---

*Process doc v1 — aligns with IG_Agent_v26_PROFITABILITY_SPEC.md*
