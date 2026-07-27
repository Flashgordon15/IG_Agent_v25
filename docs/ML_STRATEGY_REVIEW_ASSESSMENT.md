# ML / Strategy Review Assessment

**Date:** 2026-07-25  
**Scope:** Code/docs only (desk locked — no deploy, no process kill, no config hot-reload).  
**Question:** What already reviews whether strategy/ML is working, what is missing, and what to build next.

### Pre-dev audit (read-only)

| Check | Result |
|---|---|
| Market sessions / open risk | `runtime_state` open list empty (len 0); operator declared desk locked |
| Watchdog hold | No `manual_stop.json` observed |
| Agent health | `:8080` health curl empty/unreachable at audit time — treat as offline / do not touch |

---

## 1) What ALREADY exists

| Module / artifact | What it does | Gaps |
|---|---|---|
| **`src/runtime/strategy_improvement_tracker.py`** + `strategy_improvement.json` + `/api/desk/strategy_improvement` | Records managed closes; epochs on overlay hash / ML retrain; rolling WR/PnL windows; `improving` vs 70% WR target; exit-reason buckets; hooks profit-tier + intraday slots | **Path fixed 2026-07-25:** persists under `data_dir()`; still loads legacy `src/data/` if present. Snapshot is descriptive — use **`ml_strategy_review`** for APP/LOGIC verdict. Legacy file may still show **null holds / contaminated WR** until new closes rewrite under data_dir |
| **`src/runtime/desk_self_assess.py`** | “Why idle?” — hub stale, fulfillment fail-closed, gate stack, strategy-quality pause; optional Yahoo hub heal | Ops/idle heal only — **not** “is edge working?” |
| **`src/ml/decision_engine.py`** | Live blend: feed quality → setup memory → interim/XGB → profit policy | Entry-time actuator; no offline effectiveness report |
| **`src/ml/interim_scorer.py`** | Rules interim score + live state vector until enough labels | No calibration vs realized outcomes |
| **`src/ml/auto_trainer.py`** | Retrain from `ml_training_store.jsonl` when label thresholds hit; calls `note_ml_model_trained()` | Trains; does **not** prove lift (pre/post epoch, AUC, Brier, PnL by score bucket) |
| **`src/ml/setup_memory.py`** | Setup WR penalty/veto from ML store lookback | Local veto only; no desk-level “memory helping?” scorecard |
| **`src/ml/profit_policy.py`** | Marginal ML veto + session hot/cold confidence adj | Policy actuator; no A/B or veto-regret analysis |
| **`src/ml/feed_quality.py` / `core_b_entry_gate.py`** | Quote quality / Core-B ML gate pieces | Gate hardware, not review |
| **`src/diagnostics/performance_journal.py`** | CSV SoT (`daily_journal.csv`) with HoldSec, MlScoreAtEntry, regime, style | Stamp gaps still treated as APP per learning loop; journal ≠ ML calibration |
| **`src/diagnostics/ml_trade_outcomes.py`** + `metrics/ml_trade_outcomes.jsonl` (~138 rows) | Structured close rows for ML feedback / supervisor hold enrichment | Scrape/append; no rollup “ML working?” |
| **`src/data/ml_training_store.py`** + bridged `ml_training_store.jsonl` (~131 WIN/LOSS labels) | Label store; triggers auto-train + setup-memory cache invalidate | Labels exist; quality / leakage / clean-date enforcement not reviewed in one place |
| **`src/data/learning_store.py`** | Trade learning DB; journal hooks | Learning SoT for trades; not a strategy scorecard |
| **`src/diagnostics/trade_lifecycle_witness.py`** + `scripts/trade_lifecycle_witness.py` | Per-deal reconstruct + **APP / LOGIC / UNKNOWN**; day loss autopsy reports | Step-1 evidence tool — excellent for losses; **not scheduled continuous ML/strategy scorecard**; does not answer “model lift” |
| **`src/runtime/gui_desk_supervisor.py`** | Outcome checks: bleed, micro-hold, GUI-lie, session kill, journal quality, gate funnel read, recent closes | **Safety / integrity supervisor**, not expectancy or ML calibration. Will halt bleed; will not say “edge positive” |
| **`src/system/strategy_quality_gate.py`** | Session/rolling WR, loss streak, slot/hour gates — can pause entries | Reactive fail-safe from managed closes; inherits bad stamps/£0 noise |
| **`src/runtime/intraday_slot_tracker.py`** + `intraday_slot_performance.json` | Slot WR/PnL (e.g. overnight 0% WR on halt day sample) | Slot telemetry; not unified with ML score buckets |
| **`gate_funnel_report.json`** | First-block funnel counters | **Stale/empty** (ticks=0) — not useful as current review input |
| **`docs/LEARNING_LOOP_PLAN.md`** | Authoritative process: classify losses → APP then LOGIC → ML leads later (Step 5) | Plan exists; **no single module implements the continuous “is strategy/ML working” check** Step 5 assumes |

**Verdict on “is there a module reviewing this?”**  
Pieces exist (tracker snapshot, quality gate, autopsy, supervisor, ML actuators). **There is no single module that answers: “Given stamped closes, is the trading strategy and ML filter actually producing edge?”** Closest: `strategy_improvement_tracker.snapshot()` + loss autopsy — neither is a closed-loop review with ML calibration and APP/LOGIC rollup.

---

## 2) What is MISSING for “is strategy/ML actually working”

Aligned with learning-loop definition of working (autopsy + supervisor catch + one logged step):

1. **Unified scorecard** — one read-only report joining journal + ml_outcomes + strategy_improvement + autopsy classes + ML score-at-entry buckets.
2. **ML effectiveness** — calibration / lift: WR & expectancy by `MlScoreAtEntry` quintile; veto regret (skipped vs taken); interim vs XGB mode attribution; pre/post `strategy_epoch` delta after retrain.
3. **Stamp integrity gate for measurement** — exclude or flag £0 / null-hold / fail-safe closes so WR is not fake (current improvement file is contaminated).
4. **APP vs LOGIC rollup cadence** — daily autopsy already can classify; missing automatic “top APP tickets / top LOGIC hypotheses” into one operator-facing MD+JSON without manual CLI each time.
5. **Path authority** — strategy_improvement (and possibly other metrics) should live under `IG_DATA_ROOT` / `data_dir()`, not hardcoded `src/data/`.
6. **Funnel truth** — live gate funnel or explicit “funnel unavailable” so idle vs no-edge is distinguishable.
7. **No auto-resume / no strategy mutate** in the review module (locked-dev + learning-loop constraints).

---

## 3) Module status — **IMPLEMENTED** (operator-approved 2026-07-25)

**Name:** `src/diagnostics/ml_strategy_review.py`  
**CLI:** `scripts/ml_strategy_review.py` or `python -m diagnostics.ml_strategy_review --day YYYY-MM-DD`  
**Tests:** `tests/test_ml_strategy_review.py`

| | |
|---|---|
| **Role** | Offline/on-demand **review orchestrator** — wraps existing pieces; does not train, does not change config, does not start trading |
| **Inputs** | `data_dir()/metrics/daily_journal.csv`, `ml_trade_outcomes.jsonl`, `strategy_improvement.json` (prefer `data_dir()`; legacy `src/data/` noted as contaminated), latest `loss_autopsy_*.json` |
| **Outputs** | `data_dir()/reports/ml_strategy_review_YYYY-MM-DD.json` + `.md` with: (a) measurement_health, (b) strategy_edge, (c) ml_lift, (d) loss_mix, (e) **verdict** `NOT_MEASURABLE` \| `APP_BLOCKED` \| `NO_EDGE` \| `EDGE_WEAK` \| `EDGE_OK`, (f) next_one_step hint (never auto-apply) |
| **Path fix** | `strategy_improvement_tracker` now persists via `data_dir()/strategy_improvement.json` (loads legacy fallback if present) |
| **Cadence** | Locked window: **on-demand**. Overnight scrape / post-reopen N-closes can call the same CLI later |
| **Fit** | Learning loop Steps 1→5: evidence first; ML lead only when stamps trusted and APP rate low |
| **UI** | Deferred (P3) — no Quantum Terminal surface this pass |

### Verdict gates (code defaults)

1. Stamp completeness (HoldSec ≥40%, MlScore ≥35%, clean stamped closes ≥8) → else `NOT_MEASURABLE`
2. Autopsy APP share ≥40% among classified losers → `APP_BLOCKED`
3. Measurable but WR/expectancy poor → `NO_EDGE`
4. Mild positive / thin lift → `EDGE_WEAK`
5. Clean positive expectancy (+ lift when scored enough) → `EDGE_OK`

---

## 4) Priority backlog (locked-dev window)

Order matches `LEARNING_LOOP_PLAN.md` (APP before LOGIC before ML lead) with ML assessment as a **measurement** workstream (safe while locked).

### P0 — Measurement / APP (blocked-dev, no trading required)

1. ~~Design/implement **`ml_strategy_review`** (read-only)~~ — **done** (`src/diagnostics/ml_strategy_review.py`).
2. ~~Fix **data-plane path** for `strategy_improvement.json` → `data_dir()`~~ — **done** (persist under `data_dir()`; legacy load fallback). Optional bridge entry still open.
3. ~~Autopsy cadence script/cron note~~ — **done** `scripts/run_daily_loss_autopsy.sh` (+ optional `--with-review`).
4. Journal/ML stamp completeness ticket list from latest autopsy (HoldSec / MlScore gaps) — APP class. **Still needs live sample after unlock** (8/20 stamped closes proof).

### P1 — Strategy LOGIC (code/docs only until unlock; no loosen live)

5. ~~Clean-sample expectancy by exit reason / slot / epic~~ — **done** (`clean_expectancy` in `ml_strategy_review`).
6. Document LOGIC hypotheses only after APP rate drops (trail/soft_loss/short-hold) — one change class. **Deferred** (no parameter loosen).
7. ~~Revive or replace **gate funnel** reporting~~ — **done** (read-only snapshot + freshness status `ok|stale|empty|unavailable`; writer metadata pid/updated_at).

### P2 — ML (key, but after measurable stamps)

8. Score-bucket lift + calibration report inside `ml_strategy_review` — **done** (prior pass).
9. ~~Setup-memory / profit-policy veto regret analysis~~ — **done** durable `metrics/ml_veto_decisions.jsonl` + review consumption (counterfactual labels still nullable → `insufficient_data` until forward-filled).
10. ~~Auto-trainer: require review verdict ≠ `NOT_MEASURABLE`/`APP_BLOCKED` before improvement epoch~~ — **done** (`note_ml_model_trained(improvement_epoch=...)`).

### P3 — APP product (desk UI)

11. ~~Surface review verdict on Quantum Terminal~~ — **done** (`MlStrategyReviewChip` + `/api/desk/ml_strategy_review`).
12. ~~Supervisor: `APP_BLOCKED` → finding class `code`~~ — **done** (`gui_desk_supervisor`).

### APP runtime defects (2026-07-25 dual-deploy follow-up)

- ~~`/api/stop` truth mismatch / health cache~~ — **done**
- ~~SB `agent.pid` reconcile after listen~~ — **done** (`v32_runtime_start.sh launch_engine`)
- ~~Dual-lane pause write + `desk_dev_pause.sh` POST both ports~~ — **done**

**While locked: do ML measurement (P0/P2 tooling) and APP stamp fixes in code; do not claim strategy works from contaminated WR; do not auto-resume.**

---

## 5) 2026-07-25 P2 measurement + APP stamp follow-up

Code-only follow-up completed without deploy/restart:

- `ml_strategy_review` now excludes invalid `MlScoreAtEntry` values outside
  `[0,1]`, uses equal-frequency score buckets, and reports mean score, observed
  WR, calibration gap, Brier score, expected calibration error, and high-minus-low
  lift. Calibration remains `insufficient_data` below 12 valid scored closes.
- Veto regret is explicit in JSON/MD. It only measures structured veto rows with
  a labelled `counterfactual_pnl`, `shadow_pnl`, or `pnl_if_taken`; it never
  treats taken-trade PnL as the outcome of a skipped trade. The 2026-07-24 inputs
  contain no such persisted labels, so the correct status is `insufficient_data`.
- Close stamping now recovers `HoldSec` from the deal-keyed entry buffer after
  the micro track has gone, then from the learning DB using long/short IG deal-ID
  aliases. `MlScoreAtEntry` continues to recover from the same deal-keyed entry
  buffer before weaker live-snapshot fallback.

### Must be verified after the next deploy/session

No historical report can prove the new close path: the old journal rows are
immutable evidence from pre-fix runtime. After deployment, keep trading policy
locked and check:

1. After **8 new non-zero closes**, re-run the review and confirm both stamp
   percentages clear their gates and `clean_closes >= 8`. This is the earliest
   point a verdict can move away from `NOT_MEASURABLE`.
2. After **20 new stamped closes**, re-run again for the first operational APP
   verdict and minimally useful calibration/lift sample (the lift floor is 12).
3. Inspect broker-attached, ExitGate, and learning-sync close reasons separately;
   each must carry `HoldSec`, `MlScoreAtEntry`, account, origin, and non-zero cash
   PnL. Treat any missing stamp as APP, not ML/strategy evidence.
4. Confirm `MlScoreAtEntry` is finite and in `[0,1]`. Out-of-range values are now
   counted and excluded rather than silently presented as calibrated probability.
5. Veto regret remains blocked on **counterfactual labels** until a forward/shadow
   label process fills `counterfactual_pnl` / `shadow_pnl` / `pnl_if_taken` on
   `metrics/ml_veto_decisions.jsonl` rows. Decision logging is now live in
   `decision_engine`; labels stay null → evidence-safe `insufficient_data`.

### Shadow loss loop cadence

After autopsy classification, run the LOGIC-only shadow counterfactual (APP + UNKNOWN stay tickets; never mixed into ML edge claims):

```bash
./scripts/run_daily_loss_autopsy.sh YYYY-MM-DD --with-review --with-shadow
# or:
PYTHONPATH=src IG_AGENT_CONFIG=config/config_v31_demo_throughput.json \
  .venv/bin/python3 scripts/shadow_loss_loop.py --day YYYY-MM-DD
```

Report: `data_dir()/reports/shadow_loss_loop_YYYY-MM-DD.md` (+ `.json`).
Score mode is stamp vs current `profit_philosophy.min_ml_probability` (no orders / no A2 lift).

Exact operator re-runs:

```bash
./scripts/run_daily_loss_autopsy.sh YYYY-MM-DD --with-review --with-shadow
PYTHONPATH=src .venv/bin/python3 scripts/ml_strategy_review.py --day YYYY-MM-DD
PYTHONPATH=src .venv/bin/python3 -m pytest \
  tests/test_shadow_loss_loop.py \
  tests/test_ml_strategy_review.py \
  tests/test_ml_veto_and_trainer_gate.py \
  tests/test_api_stop_pause_truth.py -q
```

**Still operational after unlock (do not unlock from this report):** prove
HoldSec + MlScore stamps on **8** then **20** new closes before treating any
EDGE_* / APP_BLOCKED verdict as live strategy evidence.

---

## 5b) Stamp provenance — 2026-07-26 correction

The 2026-07-24 autopsy originally read **APP 80 / LOGIC 21 / UNKNOWN 2**. That
LOGIC bucket was an artifact. Two silent corruptions produced it, both now fixed
in `src/diagnostics/stamp_provenance.py`.

**Corruption 1 — ML score was a shared per-epic cache.**
`latest_sniper_ml_snapshot(epic=...)` returns the last row *for the epic*, so
several deals on one epic inherit one `p_success`. On 2026-07-24 that produced
26 losers stamped exactly `0.68` (= `SNIPER_THRESHOLD`), plus clusters of
11×`0.7773` and 9×`0.4378`. Two Gold rows were stamped `1.10074` — not a
probability at all. Shadow re-scoring was therefore scoring a constant and
reporting edge.

**Corruption 2 — HoldSec collapsed on broker-discovered closes.**
Broker-attached closes are found by transaction sync, so open and close
timestamps collapse and `hold_sec` lands at `0.0`. The classifier read that as a
zero-second scalp and charged 32 trades with "micro masquerade". Those holds are
*unmeasured*, not fast.

**Fixes:** every stamp now carries a source (`ml_score_source`,
`hold_sec_source`) and a trust flag; probabilities are clamped to `[0,1]` and
flagged; a value landing exactly on a gate threshold is downgraded to
`threshold_constant` whatever the caller claimed; hold buckets report
`unmeasured` rather than `<10s`.

**Reclassified result (same day, same data):**

| | Before | After |
|---|---|---|
| APP | 80 | **90** |
| LOGIC | 21 | **2** (£13.86) |
| UNKNOWN | 2 | 11 |
| Trustworthy hold stamps | not tracked | 22 / 103 |
| Trustworthy ML stamps | not tracked | 64 / 103 |

**New dimension — exit authority.** Who actually closed the position:

| Authority | Losers | PnL |
|---|---|---|
| `broker_attached_stop` | 78 | **−£370.28** |
| `agent_risk_stack` | 18 | −£87.58 |
| `unknown` | 7 | −£3.70 |

The broker's attached stop did **80% of the exiting**. Our GBP exit / virtual
stop / trail stack was decorative on those trades. A broker-closed loss is a
supervision defect, not a strategy decision, so it can no longer score as LOGIC.

**New breach code — `RISK_STACK_DID_NOT_CUT`.** Fires when the broker closed a
trade for more than `risk_per_trade_gbp × soft_loss_ratio × 1.5`. Our soft-loss
cut is £2.95 (≈5.9 pts on DOW at 0.5 £/pt) but the broker stop sits at 12 pts,
so a failure to cut roughly doubles the loss.

| APP defect | Losers | PnL |
|---|---|---|
| `RISK_STACK_DID_NOT_CUT` | 48 | −£323.22 |
| `CFD_ENTRY_WHILE_A2_PAUSED` | 41 | −£218.33 |
| `EXCLUDED_EPIC` | 8 | −£51.36 |
| `HOLD_LT_MACRO_INTENT` | 6 | −£7.48 |

**£181.62 of the £461.56 gross loss (39%) was avoidable** had the soft-loss cut
fired at £2.95 instead of letting the broker close later.

**Consequence for ML work:** there is no measurable strategy signal in
2026-07-24 — 2 LOGIC losses worth £13.86 out of £461.56. Do not tune the model
against this day. Fix the supervision gap first, then re-measure.

---

## 6) Operator one-liner

**Exists:** actuators (ML blend, setup memory, auto-train), measurement fragments, safety, evidence (loss autopsy), plus **`ml_strategy_review`** for a trusted day verdict.  
**Run:** `PYTHONPATH=src .venv/bin/python3 scripts/ml_strategy_review.py --day YYYY-MM-DD`  
**Rule:** treat sparse HoldSec / null-hold improvement files as **`NOT_MEASURABLE`**, not as proof the strategy is dead or alive.
