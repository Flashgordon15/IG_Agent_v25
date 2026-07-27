# Learning Loop Plan (operator-approved)

**Status:** Approved 2026-07-24. Authority for how we recover from the bleed halt and turn losses into one-step improvements.

**Hard constraints while this plan is active**

- Do **not** auto-resume trading under `operator_bleed_lock_*.json` (`do_not_auto_resume`).
- Do **not** remove bleed locks without explicit operator unlock (below).
- Do **not** treat GUI busy / green badges as success.
- Instant/micro stays **HARD OFF** until a later, separately approved step.

---

## Principles

1. **One change class at a time** — app bug *or* trading logic, not both in one cutover.
2. **Log before loosen** — every close must be autopsied; no “green badge = OK.”
3. **Supervisors catch bleed; humans (then ML) choose the fix.** Auto-pause on bleed; **never** auto-resume.
4. **Reopen is an experiment** with a kill-switch, not a hope.
5. **Losses = learn** — classify each loser as **APP** vs **LOGIC** vs **UNKNOWN** before changing anything.

---

## Related artifacts

| Artifact | Path |
|---|---|
| Operator halt (bleed) | [`src/data/v31-production/reports/OPERATOR_HALT_BLEED_2026-07-24.md`](../src/data/v31-production/reports/OPERATOR_HALT_BLEED_2026-07-24.md) |
| Loss autopsy (day report) | `src/data/v31-production/reports/loss_autopsy_YYYY-MM-DD.md` |
| ML / strategy review (day verdict) | `src/data/v31-production/reports/ml_strategy_review_YYYY-MM-DD.md` (+ `.json`) — [`docs/ML_STRATEGY_REVIEW_ASSESSMENT.md`](ML_STRATEGY_REVIEW_ASSESSMENT.md) · CLI `scripts/ml_strategy_review.py` |
| Daily cadence (locked-safe) | `./scripts/run_daily_loss_autopsy.sh [YYYY-MM-DD] [--with-review] [--with-shadow]` |
| Shadow loss loop | `scripts/shadow_loss_loop.py` → `reports/shadow_loss_loop_YYYY-MM-DD.md` (+ `.json`) |
| Veto decision log | `src/data/v31-production/metrics/ml_veto_decisions.jsonl` (counterfactuals nullable) |
| Per-deal autopsy files | `src/data/v31-production/autopsy/<deal_id>.json` |
| Autopsy stream | `src/data/v31-production/trade_autopsy.jsonl` |
| GUI supervisor latest | `src/data/v31-production/reports/gui_supervisor_latest.md` (+ `state/gui_supervisor_latest.json`) |
| Desk reopen checklist | [`docs/DESK_REOPEN_CHECKLIST.md`](DESK_REOPEN_CHECKLIST.md) |
| Bleed locks | `src/data/v31-production/state_cfd/operator_bleed_lock_YYYY-MM-DD.json` · `state_sb/operator_bleed_lock_YYYY-MM-DD.json` |
| Reopen witness | `src/data/v31-production/state/operator_reopen_witness.json` |

---

## Baseline (halt day)

| Layer | Status |
|---|---|
| Desk | Bleed-locked / halted until Steps 0–1 land and operator unlocks |
| In flight | Supervisor: bleed / micro-hold / GUI-lie / session kill · loss lifecycle autopsy |
| Learning SoT | Journal + `MlScoreAtEntry` / hold / exit reason (gaps = **APP** — fix first) |

---

## Loss classification

Every closed loser (mandatory deep dive). Winners: light log only.

| Class | Meaning | Action |
|---|---|---|
| **APP** | Stamps missing, wrong path, excluded epic traded, loops stuck, GUI lie / prefer vs WAIT contradiction | Ticket + test + deploy; supervisor should have caught the class |
| **LOGIC** | Gates/exits/R:R followed but expectancy bad | One parameter/rule change + witness window |
| **UNKNOWN** | Incomplete log | Treat as **APP** until stamped |

```text
Close (loss)
  → lifecycle autopsy
  → APP vs LOGIC vs UNKNOWN
  → if APP: ticket + test + deploy
  → if LOGIC: one parameter/rule change + witness window
  → journal row feeds ML
  → next session uses updated policy + model
```

---

## Steps 0–5

### Step 0 — Safety net

Finish supervisor expansions so halt cannot false-PASS:

- Alarms live for **BLEED**, **MICRO_HOLD**, **GUI_LIE**, **FLICKER**, **SESSION_KILL**, **POST_CUTOVER**, epic policy, **HALTED** chip.
- Auto-pause on bleed; durable `operator_bleed_lock_*.json` with `do_not_auto_resume: true`.
- Heal / silence paths must **not** `POST /api/start` under lock.
- Loss autopsy tooling available for Step 1.

**Exit:** Halt score ≠ false PASS under lock; autopsy readable.

### Step 1 — Evidence / autopsy pass (no new strategy)

- Run autopsy on losers → APP / LOGIC / UNKNOWN.
- Produce day report: `src/data/v31-production/reports/loss_autopsy_YYYY-MM-DD.md`.
- Include golden Path A vs losers where applicable.

**Exit:** Top 3 APP fixes and top 3 LOGIC hypotheses listed. No strategy loosen yet.

### Step 2 — Fix APP only + SB DOW-only reopen experiment

- One PR: APP class only (e.g. ML stamp at fill, Path A vs micro enforcement, SETUP hold in entry path, epic allowlist honesty).
- Tests + flat deploy only if required by anti-zombie protocol.
- Measured reopen (see knobs): **SB `:8081` only**, **DOW-focused**; CFD `:8080` stays paused (A2) unless autopsy explicitly requires otherwise.
- Instant/micro **HARD OFF**. Session kill / bleed alarms armed so auto-re-lock on fresh bleed.

**Exit:** N closes with full lifecycle stamps; supervisor quiet on APP class. Net/WR secondary.

### Step 3 — Fix LOGIC only

- Only after stamps trusted.
- One PR: e.g. min hold before trail, stop floor, Path A threshold — not bundled with APP work.
- Same controlled reopen pattern as Step 2.

**Exit:** Hold times look macro-like; bleed alarm does not fire on short-hold spam.

### Step 4 — Multi-market / ranked back on

- Turn ranked prefer / multi-market back on **only** after Steps 2–3 pass.
- Session kill (−£X, default −£150 via `IG_GUI_SUP_SESSION_KILL_NET_GBP`) stays on.

### Step 5 — ML / supervisor lead

- Closed, stamped trades → training / setup memory.
- Supervisor owns pause; Cursor handoff only for `needs_code`.
- Humans approve reopen-contract changes, not every tick.
- Day scorecard: `ml_strategy_review` must not be `NOT_MEASURABLE` / `APP_BLOCKED` before treating ML retrain as an improvement epoch (see [`ML_STRATEGY_REVIEW_ASSESSMENT.md`](ML_STRATEGY_REVIEW_ASSESSMENT.md)).

---

## Reopen experiment knobs

Use only after Steps 0–1 exit criteria, and only with explicit operator unlock.

| Knob | Value |
|---|---|
| Engines | Prefer **SB first** (`:8081`); CFD (`:8080`) only after SB sample OK |
| Epics | **DOW only** until Step 4 |
| Instant/micro | **OFF** |
| Kill | Supervisor BLEED / session −£X → durable lock (never auto-resume) |
| Witness | Stamp `operator_reopen_witness.json` **before** unlocking so pre-halt journal does not instantly re-lock |
| Success | Stamped closes + no MICRO_HOLD FAIL; net/WR secondary |

Operational detail: [`docs/DESK_REOPEN_CHECKLIST.md`](DESK_REOPEN_CHECKLIST.md).

---

## Unlock rules (bleed locks)

**Removing locks ≠ resume.** Resume is a separate per-port operator curl.

1. Read the halt report and latest supervisor score. Accept residual risk.
2. Stamp reopen witness (`state/operator_reopen_witness.json`) with `reopened_at_epoch` + `day_net_at_reopen_gbp`.
3. Remove **both** lock files (example for halt day):

```bash
rm src/data/v31-production/state_cfd/operator_bleed_lock_2026-07-24.json
rm src/data/v31-production/state_sb/operator_bleed_lock_2026-07-24.json
```

4. Operator curl **only** the intended port(s) — heal must not do this:

```bash
# Step 2 default: SB only
curl -sS -X POST http://127.0.0.1:8081/api/start

# CFD only if deliberately lifted later:
# curl -sS -X POST http://127.0.0.1:8080/api/start
```

5. Confirm `trading_paused=false` only where intended; books FLAT before size/feature changes.
6. If BLEED / SESSION_KILL / MICRO_HOLD FAIL returns: stop both, re-engage locks — **no “one more probe.”**

**Still forbidden after unlock unless separately decided:** Instant/micro ON, ranked loosen, unbounded dual-desk risk.

---

## Definition of “working”

Not “GUI busy.” Working means:

1. **Every loss explains itself** (autopsy + APP/LOGIC/UNKNOWN).
2. Supervisor would have caught today’s failure modes (bleed / micro-hold / GUI-lie / session kill).
3. The next change is **one logged step** the ML/supervisors can learn from.

---

## Shadow loss loop cadence

Day-to-day (safe while locked / `trading_paused`; never unlocks):

```text
Recent losers
  → lifecycle loss autopsy (APP / LOGIC / UNKNOWN)
  → APP tickets only (do NOT feed into ML edge claims)
  → LOGIC losers only → shadow re-score through sniper + long paths
  → report: would ML/gates have vetoed? counterfactual
  → if APP dominate: fix APP + re-run autopsy after deploy
  → if LOGIC dominate with clean stamps: one logic change OR ML veto learning
  → NEVER mix APP+LOGIC in one ML train as "edge"
```

UNKNOWN = treat as APP until stamped (excluded from ML shadow score set).

```bash
./scripts/run_daily_loss_autopsy.sh YYYY-MM-DD --with-review --with-shadow
# or separately:
PYTHONPATH=src IG_AGENT_CONFIG=config/config_v31_demo_throughput.json \
  .venv/bin/python3 scripts/shadow_loss_loop.py --day YYYY-MM-DD
```

Outputs: `src/data/v31-production/reports/shadow_loss_loop_YYYY-MM-DD.md` (+ `.json`).

---

## Immediate support order (current)

1. Finish Step 0 supervisor expansion (+ tests; run-once must not false PASS under halt).
2. Finish Step 1 loss autopsy + golden-path vs losers.
3. Only then Step 2 measured SB DOW-only reopen experiment.
4. Next single step from autopsy (APP or LOGIC) — not a mega-prompt.
