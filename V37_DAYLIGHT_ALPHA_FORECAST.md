# V37 Daylight Alpha Forecast — Repair-then-Recalibrate

**Session:** London morning DEMO dual desk (code-only turnaround)  
**Generated:** 2026-07-24T09:53:00+01:00  
**Config:** `APP_MODE=DEMO` · `IG_AGENT_CONFIG=config/config_v31_demo_throughput.json`  
**Desks:** `:8080` / `:8081` left running (no restart / no flatten / no orders)  
**Branch:** `cursor/quantum-router-audit-blueprint`

**Source:** `V36_DAYLIGHT_ALPHA_FORECAST.md` + daylight probe JSON (OBI=0/112, P≥0.78 blocks 100%, WR~23%, −£1.80/trade, journal ml_score null)

---

## PRE-DEV AUDIT (this run)

| Check | Result |
|---|---|
| Market sessions closed? | **No** — London cash hours. **Code-only**; books FLAT |
| Watchdog hold active? | N/A — no process tear-down |
| Active PIDs cleaned? | **No** — :8080/:8081 left up by operator rule |
| Safe to mutate live gates in-process? | **No restart** — ship code+tests; flat deploy next |

---

## What shipped (Phases 1–3)

### Phase 1 — OBI / feature plane (P0)
- `compute_obi_ratio_available` + `extract_order_book_depth` — distinguish missing book vs balanced OBI≈0
- `resolve_obi_signal` (book → microkernel → quote proxy) returns `(ratio, source, available)`
- `evaluate_live_sniper_probability` **fail-closes** with `obi_unavailable` — **removed** `features_unavailable_fail_open` thr-stamp approvals
- Optional **10-tick rolling OBI** after raw OBI is available
- Unit tests: synthetic book → non-zero OBI; missing book → no blind accept

### Phase 2 — Journal attribution (P0)
- `record_trade_close` recovers `ml_score` / `hold_sec` / `MarketRegime` when callers omit them
- `ensure_broker_attached_exit_journaled` passes through ml/regime
- Tests prove non-null `MlScoreAtEntry` on DIAAAA close path

### Phase 3 — ElasticGate (P1)
- Volatility-Adaptive ElasticGate in `overnight_entry_policy`
  - Healthy (tight spread + expanding depth + informative \|OBI\|) → P ∈ **[0.68, 0.72]** (never &lt;0.68)
  - Wide spread / thin OBI → P ≥ **0.78–0.82**
  - OBI unavailable → **reject**
  - Always require \|OBI\| floor when available
- Enabled in `config_v31_demo_throughput.json` (`elastic_gate.enabled` + `selectivity_gates.elastic_gate_enabled`)
- `tests/test_v37_elastic_gate.py` + Nightmare overnight regression **green**

---

## Re-probe (Phase 4) — BEFORE vs AFTER

| Metric | V36 (before) | V37 re-probe (after code, **live process unreloaded**) |
|---|---:|---:|
| Candidates | 112 | 112 |
| Finite p (API path) | 112 (100%) | 112 (100%) |
| Sniper approved | 59 | 41 |
| Sniper rejected | 53 | 71 |
| `obi_velocity_zero` | **112/112** | **112/112** |
| `features_unavailable_fail_open` | 44 | 32 (still from **old** live API) |
| Degenerate rate | 82.1% | 71.4% |
| Journal MlScore fill (daytime) | 0% | **0%** (live closes still old path) |
| Daytime expectancy | −£1.80/trade | unchanged (no new closes in window) |
| CRO verdict | WEAK | **WEAK** |

### Honest interpretation

1. **Live `:8080`/`:8081` still execute the pre-fix binary.** Probe funnel / fail-open counts come from `/api/desk/sniper_ml` on those PIDs → dead OBI and fail-open stamps persist until **flat deploy**.
2. **In-process new code** (this checkout) correctly returns `obi_unavailable` / `approved=False` when book+proxy are absent in a cold probe process — fail-open is gone in code.
3. Mini `rest_poll` still often lacks L2 depth; after deploy, **quote mid-drift proxy** (when hub mid history exists inside the agent) is the expected non-zero OBI path until L2 is wired.
4. **Do not claim £1k/day.** Daytime expectancy remains negative on the soak sample; selectivity/ElasticGate cannot manufacture alpha from a dead live feature plane.

### OBI histogram (re-probe API candidates)

All 112 candidates: `obi_velocity = 0.0` (live old process).  
Code-path unit sims: buy-heavy synthetic book → OBI &gt; 0.5; missing book → `obi_unavailable`.

---

## Sample limits (honest)

- 112 candidates / ~8 polls / ~39s — same methodology as V36
- Candidate ≠ every quote tick; latency = in-process scorer, not full tick→order e2e
- Daytime journal n remains small; ML fill on live CSV still 0 until deploy loads attribution fix

---

## Phase 5 — Self-evaluation

### 1. Fixed vs remains

| Fixed in code | Remains |
|---|---|
| Fail-open thr stamps removed | Live PIDs still old code until flat deploy |
| Availability-aware OBI resolution + rolling buffer | Live hub often has no L2 → proxy depends on mid history post-deploy |
| Journal close attribution recovery | Live CSV still null until new process journals closes |
| ElasticGate (0.68–0.72 / 0.78–0.82) | Not proven live; enable is config-ready after deploy |
| Nightmare overnight tests green | Finnhub 429 / LTR engagement / exit vandalism untouched |

### 2. Confidence OBI blindness cured

- **In code / unit tests:** **High** — synthetic book non-zero; missing → reject; no fail-open.
- **Live production proof:** **Low until flat deploy** — re-probe still shows OBI=0/112 from unreloaded processes; cold in-process hub also `obi_unavailable` without mid history.

### 3. ElasticGate safe to enable live?

**Conditionally yes after flat deploy**, with eyes open:
- Will **tighten** when OBI unavailable (reject) — correct vs blind accept
- Will **not** loosen below 0.68
- Do **not** enable if operator wants entries while feature plane is still blind — ElasticGate will starve rather than stamp fake conviction
- Config already sets `elastic_gate.enabled: true` for next load

### 4. Ranked next steps

1. **Flat deploy** (sessions FLAT + anti-zombie protocol) to load OBI fail-closed + journal attribution + ElasticGate
2. **Re-probe** immediately post-deploy — expect `obi_unavailable` or non-zero proxy OBI; fail-open count → 0
3. **Feature work:** ensure hub mid history / optional L2 on rest_poll so proxy/book can be non-zero in agent
4. **Exit vandalism / LTR:** daytime exits still broker_attached / soft_loss; zero LTR — separate hunt
5. **Threshold tune** only after OBI hist is non-degenerate for a full London session

### 5. Residual risks

- Post-deploy entry starvation if proxy mid history thin at open
- ElasticGate + rigid overnight 0.78 interaction on stressed band (intentional)
- Attribution recovery uses last sniper snapshot / autopsy — may mis-attribute if epic rotates before close
- Negative daytime expectancy unchanged — gates alone ≠ alpha

---

## Console summary

```
CRO VERDICT: WEAK (live unreloaded) / CODE PATH: FAIL-CLOSED FIXED
TOP ACTIONS:
  1. Flat deploy when books stay FLAT
  2. Re-probe — confirm fail-open=0 and OBI available or explicit reject
  3. Do not loosen P to 0.64
  4. £1k/day NOT proven
```

---

## POST-DEPLOY RE-PROBE (2026-07-24 ~09:58 BST)

**Deployed HEAD:** `72b72dd` (`Fix daylight OBI fail-open and journal attribution before ElasticGate.`)  
**Live PIDs (verified before + after probe):** CFD `:8080` **88437** · SB `:8081` **88654**  
**Books:** both desks **FLAT** · `trade_ready=true` · no restart / no orders during probe  
**Command:** `daylight_alpha_probe.py --ports 8080,8081 --candidates 100 --max-minutes 20`  
**Result artifact:** `src/data/v31-production/reports/daylight_alpha_probe_2026-07-24.json`  
**Window:** 2026-07-24T09:58:15 → 09:58:51 (+01:00) · 8 polls · 113 candidates · 35.2s

### PRE-DEV AUDIT (re-probe)

| Check | Result |
|---|---|
| Market sessions closed? | **No** — London cash open (7/7 quotes fresh). **Read-only probe only** |
| Watchdog hold active? | **No** |
| Active PIDs cleaned? | N/A — expected live PIDs confirmed (88437 / 88654) |
| Safe for code/deploy? | Probe only — no entry/exit edits, no kill, no port restart |

### Before vs after (live API on new PIDs)

| Metric | Prior (V36 / pre-deploy live) | Post-deploy re-probe (`72b72dd` PIDs) |
|---|---:|---:|
| Candidates | 112 | **113** |
| Finite p (API path) | 112 (100%) | 112 finite + 1 null (99.1%) |
| Sniper **approved** | 59 (V36) / mixed fail-open | **0** |
| Sniper **rejected** | 53 | **113** |
| Reject reasons | chop / selectivity / mix | **`obi_unavailable` 112** + `regime_veto_obi_unavailable` 1 |
| `obi_velocity = 0` | **112/112 (100%)** | **112/112 (100%)** |
| `obi_unavailable` flag | absent / masked by fail-open | **112/112 (100%)** |
| `features_unavailable_fail_open` | **44–76** (blind thr stamps) | **0** |
| Degenerate rate | ~82% / 71% unreloaded | **99.1%** (honest: unavailable ≠ balanced) |
| p when blocked | often 0.68–0.777 stamped | **p=0.10** fail-closed stamp |
| Selectivity P≥0.78 | block-all on mid-band p | still blocks (p=0.1≪0.78) — secondary to OBI reject |
| ElasticGate live band | not loaded | thr still epic base **0.68 / 0.70 / 0.74**; **no [0.78–0.82] stress band** (never reached healthy ElasticGate path — OBI reject first) |
| Journal MlScore daytime | 0% fill | still **0/13** (no new closes in probe window; CSV cohort pre-deploy) |
| Daytime expectancy | −£1.80/trade | unchanged (−£1.80 / n=13) |
| Probe `cro_verdict` | WEAK | **WEAK** |
| £1k/day | NOT proven | **NOT proven** |

### OBI histogram (post-deploy API candidates)

| Bucket | Count | % |
|---|---:|---:|
| `0` (and `obi_unavailable`) | 112 | 100% |
| `(0, 0.05)` … `≥0.5` | 0 | 0% |

**% zero OBI:** 100% · **% unavailable rejects:** 112/113 = **99.1%** (plus 1 regime veto on same plane)

### What changed vs what did not

1. **Fail-open thr stamps are gone** — live PIDs no longer emit `sniper_ml_features_unavailable_fail_open` / mid-threshold blind approvals. Fix **verified on new processes**.
2. **Feature plane still BLIND on Mini rest_poll** — no L2 book and no usable mid-history proxy inside the agent → every epic returns `obi_unavailable` → hard reject.
3. **Zero approved is correct fail-close, not a new P=0.78 bug** — prior “P0.78 block-all” was selectivity on fake mid-band p; now sniper never approves because OBI is unavailable.
4. **ElasticGate not exercised live** — healthy [0.68–0.72] loosen path needs informative \|OBI\|; stress [0.78–0.82] also never engaged. Observed thresholds are epic base floors only.
5. **Do not claim £1k/day** — daytime expectancy still negative on the soak journal sample.

### CRO verdict (operator)

**BLIND** (live OBI/feature plane) with **correct fail-close** → daytime funnel **OVER-FILTERING by design** until quote-proxy / L2 feeds OBI.  
Probe label remains **WEAK**. Not FUNCTIONING. Not DATA UNRELIABLE for gate semantics (rejects are explicit and consistent).

### Ranked next steps

1. **Wire live OBI inputs on rest_poll Mini** — hub mid-history proxy and/or L2 depth so `obi_unavailable` rate drops and ElasticGate can actually run.
2. **Re-probe after OBI source lands** — expect non-zero OBI hist, some approvals in healthy band, fail-open still 0.
3. **Do not loosen P to 0.64** / do not re-enable fail-open to “get trades.”
4. **Journal MlScore** — confirm fill on the *next* live closes under `72b72dd` (attribution code loaded; no new closes in this window).
5. **Exit / LTR hunt** remains separate (broker_attached dominant; LTR=0) — not unblocked by this deploy.

```
CRO VERDICT: BLIND (fail-closed) / WEAK probe label
FAIL-OPEN STAMPS: GONE (0)
FUNNEL: approved=0 / rejected=113 (obi_unavailable)
OBI: 0/112 available · 100% zero · ElasticGate not reached
£1k/day: NOT proven
TOP ACTION: restore live OBI (proxy/L2) then re-probe
```

---

## POST-OBI-RESTORE (2026-07-24 ~10:15 BST)

**Deployed HEAD:** `deb8d4b` (`Restore daytime OBI on Mini rest_poll via rolling mid quote-proxy.`)  
**Live PIDs after anti-zombie dual reload:** CFD `:8080` **8290** · SB `:8081` **8554**  
**Books:** both desks **FLAT** during recycle · no `kill -9`  
**Root cause (confirmed):** `QuoteSnapshot` had bid/offer only — no rolling mids. True L2 is absent on Mini `rest_poll`, so `_obi_proxy_from_quote_available` always returned unavailable → 100% `obi_unavailable`.  
**Fix path:** hub now appends mid on every publish; `compute_proxy_obi_from_mids` builds signed up/down-move imbalance (**quote-proxy, not L2**); flat/missing series still fail-closed; fail-open stays deleted.

**Command:** `daylight_alpha_probe.py --ports 8080,8081 --candidates 100 --max-minutes 20`  
**Result artifact:** `src/data/v31-production/reports/daylight_alpha_probe_2026-07-24.json` (overwritten; `git_head=deb8d4b…`)  
**Window:** 2026-07-24T10:15:01 → ~10:15:39 (+01:00) · 8 polls · **112** candidates · **38.5s**

### PRE-DEV AUDIT (reload + probe)

| Check | Result |
|---|---|
| Market sessions closed? | **No** — London open; recycle allowed because books **FLAT** |
| Watchdog hold active? | Engaged via `mark_manual_stop` for recycle; cleared after boot |
| Active PIDs cleaned? | Old 88437/88654 exited (TERM→INT); ports rebound 8290/8554 |
| Safe for code/deploy? | Yes (flat) — dual anti-zombie reload completed |

### Before vs after (post-OBI-restore)

| Metric | Post-deploy re-probe (`72b72dd`) | Post-OBI-restore (`deb8d4b`) |
|---|---:|---:|
| Candidates | 113 | **112** |
| Sniper **approved** | **0** | **49** (43.8%) |
| Sniper **rejected** | 113 | **63** |
| Reject dominant | `obi_unavailable` 112 | `sniper_ml_chop_isolation` (P below class thr) |
| `obi_source` | `obi_unavailable` | **`quote_proxy` 112/112** |
| OBI available | 0/112 | **112/112** |
| % zero \|OBI\| | **100%** | **0.9%** (1/112) |
| `features_unavailable_fail_open` | **0** | **0** |
| Degenerate rate | 99.1% | **0.9%** |
| Probe `cro_verdict` | WEAK / BLIND | **ALPHA FUNCTIONING** |
| £1k/day | NOT proven | **NOT proven** (daytime expectancy still −£1.80 / n=13 journal) |

### OBI histogram (|raw|, post-restore API candidates)

| Bucket | Count | % |
|---|---:|---:|
| `0` | 1 | 0.9% |
| `(0, 0.05)` | 24 | 21.4% |
| `[0.05, 0.15)` | 37 | 33.0% |
| `[0.15, 0.25)` | 20 | 17.9% |
| `[0.25, 0.5)` | 16 | 14.3% |
| `≥0.5` | 14 | 12.5% |

### What this proves / does not prove

1. **Live OBI plane is no longer blind on Mini rest_poll** — rolling mid quote-proxy feeds non-zero \|OBI\| when the market moves; ElasticGate/sniper can score instead of hard-rejecting unavailable.
2. **Fail-open remains gone** (0 stamps) — restore did **not** re-enable blind thr approvals.
3. **Approvals returned** (49/112) with class thresholds 0.68 / 0.70 / 0.74 still in force; chop isolation still rejects weak P.
4. **This is proxy OBI, not true L2** — do not treat as institutional book stacking.
5. **£1k/day NOT proven** — journal daytime expectancy still negative on the soak sample.

### CRO verdict (operator)

**ALPHA FUNCTIONING** on the sniper feature plane (probe label). Daytime expectancy / £1k promotion still **not** supported.

### Ranked next steps

1. Confirm journal `MlScoreAtEntry` fills on the *next* live closes under `deb8d4b`.
2. Watch ElasticGate healthy vs stressed bands over a full London session (selectivity still shows P≥0.78 fails on some paths).
3. Do **not** loosen P below 0.68 / do **not** re-enable fail-open.
4. Exit / LTR hunt remains separate.
5. Optional later: true L2 if IG ever exposes depth on this desk — keep proxy as Mini fallback.

```
CRO VERDICT: ALPHA FUNCTIONING (proxy OBI live)
FAIL-OPEN STAMPS: 0
FUNNEL: approved=49 / rejected=63
OBI: quote_proxy 112/112 · % zero ≈0.9% · fail-open=0
£1k/day: NOT proven
TOP ACTION: journal MlScore fill + session-length ElasticGate observation
```
