# V37 Dual-Regime Ownership Map

**Status:** master architecture for independent mathematical regimes  
**Desks:** `:8080` / `Z6BAH4` / `QUANT_SNIPER` (CFD) · `:8081` / `Z6BAH3` / `MACRO_SENTINEL` (SB)  
**Constraint:** code + tests + docs only this turn — **no live restart / deploy**  
**WR goal:** sustainable **>60% WR baseline on BOTH** accounts — **not claimed by this PR alone**

---

## Phase 0 — Witness baseline (2026-07-24)

### Read-only audit (this session)

| Check | Result |
|---|---|
| Market sessions closed? | **No** (London daylight). **Code-only** — live runtime **not** modified |
| Watchdog hold / PID cleanup? | N/A — no process control |
| Daylight session witness | **Still running** (`--until 16:00`, PID alive) |

### Verdict: **INCONCLUSIVE** (primary) + overnight **FAIL** (contrast)

| Source | Verdict | WR | Net £ | ML stamp fill | OBI / Elastic | Cascades | CFD vs SB |
|---|---|---:|---:|---:|---|---:|---|
| `daylight_witness_*.jsonl` (partial, ~poll 5) | **INCONCLUSIVE** | 0% (n=1) | −6.66 | 100% on that 1 close | fail_open=0; elastic evidence=0 | 0 | CFD 1 loss; SB 0 closes |
| `daylight_alpha_probe_2026-07-24.json` (10:15) | ALPHA FUNCTIONING (feature plane) | Day journal **23.1%** (n=13) | −23.35 | Journal **0%** / outcomes path 100% | OBI plane restored earlier; ElasticGate bands **not** live-proven | 0 | Probe 56/56 candidates; daytime closes mostly Z6BAH3 |
| `trading_report_2026-07-24_0800.md` | **FAIL** overnight | **13.6%** (n=66) | **−215.93** | Autopsy ml_score **null** | 429 noise; LTR hits=0 | Cap breach 0; desk_down=4 | CFD 0/22 (−92.57 scalp); SB 9/44 (−123.36 long) |

**Implication for gates:** do **not** hardcode aggressive ElasticGate 0.72/0.82 calibration from this sample. Keep ElasticGate as **config knobs**; CFD owns the knobs; SB must not depend on HF OBI-velocity scalp triggers.

---

## Shared vs CFD-only vs SB-only

### Shared (read-mostly / infra)

| Module | Path | Role |
|---|---|---|
| Engine lane IDs | `src/system/engine_lane.py` | `QUANT_SNIPER` / `MACRO_SENTINEL`, journal metadata, CFD hard-cap 1 |
| Dual-port CLI / state dirs | `src/system/engine_cli.py`, `src/system/paths.py` | `state_cfd/` vs `state_sb/`; forbid cross-write |
| Session / locks | `src/runtime/session_lock.py`, `src/runtime/session_registry.py` | Per-account locks |
| Desk deploy / anti-zombie | `scripts/desk_deploy.sh`, `.cursorrules` | Flat-books only; never `kill -9` main |
| Journal schema | `src/diagnostics/performance_journal.py` | Must stamp **AccountID**, **EngineOrigin**, **MlScoreAtEntry**, **MarketRegime**, **HoldSec** |
| ML outcomes sidecar | `src/diagnostics/ml_trade_outcomes.py` | `ml_score_at_entry`, `market_regime`, `hold_duration_seconds` |
| Risk hard caps | `src/execution/order_in_flight_mutex.py`, Rest budget | Preserve hard-cap 1 CFD; REST budget |
| Overnight window helper | `src/runtime/overnight_entry_policy.py` (`overnight_entry_lockdown`) | Shared clock; **policy differs by lane** |
| Dual-regime isolation API | `src/system/dual_regime.py` | Engine-scoped stores + exit matrix helpers |

**Forbidden:** shared **mutable** gate / ML override arrays that one engine can overwrite for the other (`_ml_dynamic_overrides` must be engine-keyed; macro sentiment must not be clobbered by CFD scalp fills).

### CFD-only — `QUANT_SNIPER` / `:8080` / `Z6BAH4`

| Concern | Owner modules | Notes |
|---|---|---|
| Microstructure entry | `src/alpha/micro_sniper_ml.py`, `src/execution/entry_gate_hardening.py`, `src/intelligence/order_book_imbalance.py` | OBI velocity / touch / short-horizon momentum; **quote-proxy OBI**; **no fail-open** |
| ElasticGate knobs | `src/runtime/overnight_entry_policy.py` (`elastic_gate`, `evaluate_elastic_gate`) + `config.elastic_gate` | **CFD-owned**; config bands only — do not hardcode 0.72/0.82 until Phase 0 SUCCESS |
| Instant / micro scalp | `src/runtime/dual_core_execution.py` (micro paths), `micro_scalp_instant` config | HF scalp lane |
| 12pt DOW stop floor | `src/execution/live_broker_order_router.py` (`desk_entry_stop_floor_pts`), `config.micro_risk.dow_broker_stop_floor_pts` | Preserve **12** (never 6/4) |
| Rapid scalp trail / banks | `src/runtime/micro_gbp_exit.py`, tiered banks in `micro_risk` | CFD keeps scratch banks |
| Overnight CFD entry ban | `overnight_entry_policy.evaluate_overnight_entry` → `overnight_cfd_new_entries_blocked` | **Preserve** — no overnight CFD entries |
| Cascade guard | `engine_position_caps.cfd_sniper: 1` | Hard-cap 1 |

### SB-only — `MACRO_SENTINEL` / `:8081` / `Z6BAH3`

| Concern | Owner modules | Notes |
|---|---|---|
| Macro / directional entry | MTF / sentiment / breakout·S-R via trading loop + env scorer | **No HF OBI-velocity scalp triggers** |
| Prefer passive asymmetric limits | `asymmetric_ioc_routing`, DynamicLimit skip until armed | Prefer limits over hyper-trail scrapes |
| Trend-Retention | `src/runtime/profit_run_policy.py` + `src/runtime/long_trade_runner.py` + `dual_regime.evaluate_exit_matrix` | UPL≥£15 → kill micro trails → **BE+1** floor → LTR giveback **~20% peak** so winners breathe |
| Overnight SB path | Instant/micro blocked; long_trade_runner only when gates pass | Preserve existing lockdown split |
| **Daytime SB Instant/Core-B ban** | `dual_regime.sb_disable_instant_micro` + `sb_disable_core_b_micro` + `sb_macro_ltr_entries_only` via `evaluate_engine_entry_path_policy` | **Hard-disable** Instant + `ENGINE_B_MICRO_SCALPER` on `:8081` even outside overnight window |
| Skip scalp banks | `long_trade_runner.sb_prefer_long_hold` | CFD chop gates must not short-circuit SB |

---

## V38 cutover note — 2026-07-24 (flat books)

**Problem:** Daytime SB still entered via Instant / `ENGINE_B_MICRO_SCALPER` because overnight lockdown returns `outside_overnight_window` → allow. Soft-loss ~£3 scrapes prevented LTR / Trend-Retention from ever arming (~19% WR bleed).

**Fix (on disk + reload):**
1. Config: `dual_regime.sb_disable_instant_micro=true`, `sb_disable_core_b_micro=true`, `sb_macro_ltr_entries_only=true`
2. Code gates: `allow_engine_micro_scalp_path` + `evaluate_engine_entry_path_policy` in Instant tick lane, ParallelStrategySweep, DualCoreCoordinator dispatch; SB `core_b_micro_active` forced off in mid ingest
3. REST: `IGRestClient.confirm_deal` already fail-fast refuses synthetic `MICRO-*` dealReferences (loads on restart)
4. CFD Instant/micro path **unchanged** (available when `:8080` unpaused); A2 pause re-applied after cutover

**Safest post-cutover posture:** both desks `POST /api/stop` until SB macro path proven in logs.

**Enable SB-only macro probe later (CFD stays paused):**
```bash
# Prove Instant-micro disabled in effective config first, then:
curl -sS -X POST http://127.0.0.1:8081/api/start
# Keep CFD paused:
curl -sS http://127.0.0.1:8080/api/health | python3 -c "import sys,json; print(json.load(sys.stdin).get('trading_paused'))"
```

---

## Exact files to change (this master split)

| File | Change |
|---|---|
| `docs/V37_DUAL_REGIME_OWNERSHIP.md` | This map + Phase 0 |
| `src/system/dual_regime.py` | **New** — engine-scoped ML/gate/sentiment stores + exit matrix |
| `src/runtime/overnight_entry_policy.py` | ElasticGate applies to CFD owner only; SB skips HF ElasticGate path |
| `src/runtime/dual_core_execution.py` | Engine-keyed ML cognitive overrides (no cross-account clobber) |
| `src/runtime/long_trade_runner.py` | SB Trend-Retention giveback ~0.20 when profit_run active |
| `src/runtime/profit_run_policy.py` | Engine-aware Trend-Retention decision helper hooks |
| `config/config_v31_demo_throughput.json` | `dual_regime` ownership block (knobs; no aggressive P hardcode) |
| `tests/test_v37_dual_regime_isolation.py` | **New** — isolation + journal stamps + exit matrices |
| `tests/test_v37_journal_attribution.py` | Extend AccountID / EngineOrigin assertions |

**Do not touch:** SQLite history, live PIDs, OBI fail-open re-enable, overnight CFD ban removal, UI/4K aesthetic.

---

## Preserve (non-negotiable)

1. Hard-cap **1** open on CFD (`Z6BAH4`)
2. DOW stop floor **12**
3. Overnight **CFD** new-entry ban
4. Quote-proxy OBI + **fail-closed** when unavailable
5. ElasticGate as **config knobs** (never loosen below 0.68 in code clamp)
6. Anti-zombie deploy rules (flat books + `mark_manual_stop` + TERM)

---

## What NOT to build (defer)

Do **not** ship a 60s CFD→SB anti-correlation mutex from dual-regime ownership work — independent engines may both SELL the same index; that is portfolio risk, not a process lock bug (see `docs/v38_correlation_post_mortem.md`). REST stays on the existing shared budget (`RestApiBudget` / shared_rest_budget); do not add a second limiter. GUI dual-port cash must **dedupe** shared journal aggregates rather than sum identical day totals.

---

## Flat-books deploy checklist (operator — do **not** execute here)

See end of delivery note / §Deploy checklist in the parent handoff. Requires FLAT on both ports, anti-zombie tear-down, then reload both engines with this tree.
