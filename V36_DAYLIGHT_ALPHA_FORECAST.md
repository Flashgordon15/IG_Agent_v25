# V36 Daylight Alpha Forecast — 2026-07-24

**Session:** London morning DEMO dual desk  
**Generated:** 2026-07-24T09:35:35+01:00  
**Probe window:** 2026-07-24T09:34:57 → 09:35:35 Europe/London (~37s; target 100 candidates hit early)  
**Git HEAD:** `0796cd47174b00b2060efc2bc8ecf8ed1aad5635` (Nightmare Night + Profit Barrage gates)  
**Config:** `APP_MODE=DEMO` · `IG_AGENT_CONFIG=config/config_v31_demo_throughput.json`  
**Desks:** `:8080` PID **54455** Z6BAH4 QUANT_SNIPER (CFD) · `:8081` PID **54848** Z6BAH3 MACRO_SENTINEL (SB)  
**Book:** FLAT both desks · `trade_ready=true` · no restart / no flatten / no orders  

**Artifacts**
- Probe: `src/diagnostics/daylight_alpha_probe.py`
- JSONL: `src/data/v31-production/logs/daylight_alpha_probe_2026-07-24.jsonl`
- Summary JSON: `src/data/v31-production/reports/daylight_alpha_probe_2026-07-24.json`
- Overnight FAIL context: `src/data/v31-production/reports/trading_report_2026-07-24_0800.md`

---

## PRE-DEV AUDIT (read-only)

| Check | Result |
|---|---|
| Market sessions closed? | **No** — London cash hours open (~09:35 BST). Diagnostics only. |
| Watchdog hold active? | **No** |
| Active PIDs cleaned? | **Yes** — single listener each on 8080/8081; FLAT |
| Safe to mutate live gates? | **No / not requested** — report only |

---

## CRO verdict

# **WEAK**

ML is **not blind** (finite `p_success` on 112/112 live candidates), but the **feature plane is degenerate**, **selectivity P≥0.78 rejects essentially every sniper “approval”**, and daytime expectancy is **negative**. £1k/day is **not proven**.

| Enum option | Fit |
|---|---|
| ALPHA FUNCTIONING | No — degenerate features + negative daytime expectancy |
| **WEAK** | **Primary** — scores exist but alpha signal quality is thin |
| BLIND | No — `ml_finite_rate=1.0` on probe sample |
| GATES OVER-FILTERING | Secondary — selectivity 0.78 + 15m BEARISH BUY block starve hot path |
| DATA UNRELIABLE | Partial — Yahoo fresh; Finnhub 429 degraded (secondary) |

---

## Mission answers (evidence)

### 1. Is ML scoring daytime entry candidates with non-null probabilities?

**Yes.** Live `/api/desk/sniper_ml` snapshots for both desks returned finite `p_success` on all 7 epics × 8 polls.

| Metric | Value |
|---|---:|
| Candidates | 112 |
| Finite p | 112 (100%) |
| Null p | 0 |
| Sniper approved (p≥class thr) | 59 |
| Sniper rejected (chop isolation) | 53 |

Caveat: journal `MlScoreAtEntry` on daytime closes is still **0/13 filled** (attribution leak on close path). `ml_trade_outcomes.jsonl` does carry scores (17/17 since 07:00).

### 2. Are features (OBI, 15m trend, spread elasticity) live and non-degenerate?

**Partially live, largely degenerate.**

| Feature | Observation |
|---|---|
| OBI / `obi_velocity` | **0.0 on 112/112** candidates |
| `spread_elasticity` | **Stuck ≈1.0 on 80/112** |
| `atr_velocity` | **0.0 on 68/112** |
| `features_unavailable_fail_open` | **44/112** — stamped at thr mid-point (0.68) and marked approved |
| 15m macro trend | **Live BEARISH** both desks (`/api/v31/gate-stack` Gate 4 BLOCKING for BUY) |

`feature_health.degenerate_rate = 82.1%` → treat sniper P as **weakly informative**, not production-grade alpha.

### 3. Tick→score latency p50/p95/p99 vs &lt;20ms

| Stat | ms |
|---|---:|
| n | 112 |
| p50 | **0.006** |
| p95 | **0.49** |
| p99 | **1.22** |
| max | 320.8 (cold/import outlier) |

**Method (honest):** in-process `evaluate_live_sniper_probability` via `alpha.micro_sniper_ml` timed with `time.perf_counter`. **Not** full tick→order e2e. `/api/unified/performance` `e2e_latency_ns` is **zeroed** (`last/p50/p99 = 0`) — **do not claim** production path &lt;20ms compliance from that API.

In-process scorer p99 &lt;20ms: **yes (measured)**. End-to-end path: **insufficient instrumentation**.

### 4. Gate funnel reject counts by reason

From live sniper snapshots (probe funnel):

| Reason | Count |
|---|---:|
| APPROVED (sniper thr pass) | 59 |
| `sniper_ml_chop_isolation` (various p&lt;thr) | 53 |

Selectivity overlay (`selectivity_gates` P≥0.78 / \|OBI\|≥0.25 / 15m agree) on the **same** candidates:

| Selectivity reason | Count |
|---|---:|
| `selectivity_p_fail` (all variants) | **112** (every candidate fails P first) |
| of which sniper-approved then selectivity-blocked | **59** |

Log matrix blocks since post-deploy (CFD+SB current logs): predominantly `sniper_ml_chop_isolation` on DOW; plus CFD `alpha_decay_kill`, `limit_chase_max_ticks_exceeded`.

Config thresholds in force:
- `sniper_ml` class thr: INDEX **0.68** / FX **0.70** / GOLD **0.74**
- `selectivity_gates.min_ml_p_success`: **0.78**
- `selectivity_gates.min_abs_obi`: **0.25**
- Overnight lockdown **enabled** 21:00–07:00 (outside window now → `outside_overnight_window`)
- `ml_unblind.enabled`: **true**

### 5. Which rejects look correct vs false negatives?

| Class | n | Notes |
|---|---:|---|
| correct | 79 | Chop isolation when P truly mid/low **or** pass-through approvals without fail-open |
| false_negative | 33 | Chop isolation **with** degenerate OBI/elasticity=1 plane — may discard recoverable edge; also fail-open thr stamps masquerading as conviction |

**Correct (keep):**
- Hard chop when live P≪0.68 on DOW with no feature support
- Overnight CFD ban / SB micro ban (out of window now — not binding)
- 15m BEARISH blocking **BUY** (Gate 4) while allowing SELL
- REST budget / mutex / streak when actually tripped

**False-negative / over-filter suspects:**
- Selectivity **0.78** after sniper already gates at **0.68–0.74** → double veto; Gold `p=0.777` misses 0.78 by knife-edge
- \|OBI\|≥0.25 cannot pass while `obi_velocity` stuck at 0 on rest_poll Mini
- `features_unavailable_fail_open` → synthetic P=thr “approvals” (Nikkei/DAX) that then fail selectivity anyway
- `alpha_decay_kill` / `limit_chase_max_ticks_exceeded` on CFD — may be exit/entry vandalism vs alpha reject

### 6. Daytime expectancy since London open / last N hours

**Journal** (`daily_journal.csv`, today hour≥07 London):

| | |
|---|---:|
| n | 13 |
| WR | **23.1%** (3W / 10L) |
| Net | **£-23.35** |
| Expectancy | **£-1.80 / trade** |
| Hold sec | unavailable (null) |
| ML journal fill-rate | **0%** (0/13) |
| Exits | broker_attached 12 · soft_loss breach 1 |
| Style | supervised_exit 12 · macro 1 |
| Account | **all Z6BAH3 (SB)** |

**ml_trade_outcomes** (ts≥07:00):

| | |
|---|---:|
| n | 17 |
| WR | 23.5% |
| Net | **£-31.74** |
| ML score fill | **100%** (scores present here, not in journal CSV) |
| CFD (Z6BAH4) | 1 close · £-0.60 |
| SB (Z6BAH3) | 16 closes · £-31.14 |

Overnight contrast (FAIL): n=66 · WR 13.6% · net **£-215.93** · autopsy ml_score null.

### 7. CFD vs SB; scalp vs long_trade_runner

| Desk | Daytime closes | Net | Scalp / LTR evidence |
|---|---:|---:|---|
| CFD QUANT_SNIPER | 1 (ml outcomes) | -0.60 | ORDER_SUBMITTED seen; **0** `long_trade_runner` log hits |
| SB MACRO_SENTINEL | 13–16 | ~-23 to -31 | Style=supervised_exit/macro; **0** LTR log hits |

Post-deploy logs: CFD MicroScalper/submit activity exists; **no** LTR arming observed on either desk this morning.

### 8. Data-plane: feed ages, 429s, Yahoo/IG freshness

| Source | Health | Notes |
|---|---|---|
| Yahoo | **ok / alive** | Quote ages typically **&lt;3s** (DOW ~0–2s) |
| Twelve Data | ok / alive | High retry_count (~144) |
| Finnhub | **degraded / dead** | HTTP **429**; probe counted **4** new 429 hits in ~37s |
| IG execution | rest_poll path | `trade_ready`; books FLAT |
| REST pressure | OK at probe end | Earlier morning saw HIGH / path-down (stability API) — intermittent |

Primary signal path usable; secondary Finnhub noise continues overnight pattern (274× overnight).

### 9. Ranked remediation (do **not** apply without operator OK)

1. **P0 — Feature plane:** Fix rest_poll OBI / elasticity / ATR velocity so sniper is not scoring a constant plane; disable or quarantine `features_unavailable_fail_open` mid-thr approvals.  
2. **P0/P1 — Threshold stack:** Reconcile sniper class thr (0.68–0.74) vs selectivity **0.78**; with OBI≡0, \|OBI\|≥0.25 is a hard starve. Decide one authoritative gate.  
3. **P1 — Journal attribution:** Stamp `ml_score_at_entry` into `daily_journal.csv` / autopsy on fill (`ml_unblind` path incomplete on close).  
4. **P1 — 15m Gate 4:** Confirm SELL path is actually exercised under BEARISH (BUY correctly blocked); avoid silent BUY-only starvation.  
5. **P2 — Finnhub 429:** Back off reconnects; keep Yahoo primary.  
6. **P2 — long_trade_runner:** Verify SB LTR arming (3m / 4R / 40% giveback) — zero daytime hits.  
7. **P3 — E2E latency meters:** Wire non-zero tick→score→submit timers; unified `e2e_latency_ns` currently useless.

---

## Phase C — Deep hunt

### Over-filtering — **P0/P1**
- Evidence: 59 sniper approvals → **59** selectivity blocks; DOW mean p≈0.54; Gold 0.777 fails 0.78.  
- Fix: single calibrated thr; temporarily allow diagnostic shadow mode for 0.70–0.78 band (observe only).

### Under-filtering — **P2**
- Evidence: fail-open approvals (44) at synthetic 0.68; overnight CFD still printed losers pre-lockdown deploy.  
- Daytime under-filter less dominant than over-filter post-0796cd4.

### ML blindness — **Not primary; attribution weak — P1**
- Live scoring non-null; journal CSV blind on closes; features degenerate → **effective** blindness.

### Exit vandalism — **P2**
- Daytime exits dominated by `broker_attached` / soft_loss; overnight `micro_gbp_exit` scalp scratches.  
- No LTR giveback path observed — runners not engaged.

### Data / latency — **P2**
- Yahoo fresh; Finnhub 429; in-process score fast; e2e meters dark.

---

## PnL tables

### Overnight FAIL (context)

| Account | Engine | n | WR | Net £ |
|---|---|---:|---:|---:|
| Z6BAH4 | QUANT_SNIPER | 22 | 0% | -92.57 |
| Z6BAH3 | MACRO_SENTINEL | 44 | 20.5% | -123.36 |
| **Total** | | **66** | **13.6%** | **-215.93** |

### Daytime since ~07:00 London (journal)

| Account | n | WR | Net £ | ML fill |
|---|---:|---:|---:|---:|
| Z6BAH3 SB | 13 | 23.1% | -23.35 | 0% |
| Z6BAH4 CFD | 0 in journal | — | — | — |

### Probe funnel (live)

| Desk | Candidates | Approved | Rejected |
|---|---:|---:|---:|
| CFD | 56 | ~half mixed | chop + selectivity |
| SB | 56 | ~half mixed | chop + selectivity |

---

## £1k / day sanity bound

| | |
|---|---|
| Claim | **NOT proven** |
| Daytime expectancy | **£-1.80 / trade** |
| Implied trades for +£1k | **undefined** (need positive expectancy first) |
| Note | Negative expectancy cannot scale to £1k/day. Overnight −£216 on 66 closes reinforces soak ≠ promotion. |

---

## Sample limits (honest)

- Hit **112** candidates in **8 polls / ~37s** (live `/api/desk/sniper_ml` epic snapshots × 2 ports).  
- Did **not** need 10-minute scarce-candidate extension or log backfill.  
- Candidate ≠ every quote tick; latency ≠ full tick→order path.  
- Daytime PnL n=13 is small — treat as directional soak, not significance.

---

## Console summary

```
CRO VERDICT: WEAK
TOP 5 LEAKS:
  1. [P0] Degenerate ML features (OBI=0, elasticity≈1, fail-open thr stamps)
  2. [P0/P1] Selectivity P≥0.78 blocks 100% of candidates incl. all sniper approvals
  3. [P1] Journal MlScoreAtEntry null (0/13) despite live finite scores
  4. [P1] 15m BEARISH Gate 4 BUY block + weak DOW p≈0.54 starve hot path
  5. [P2] Finnhub 429 + zero long_trade_runner engagement
```

**No live gate patches applied.** Probe + tests only.
