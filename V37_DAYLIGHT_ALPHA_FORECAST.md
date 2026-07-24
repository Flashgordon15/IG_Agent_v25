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
