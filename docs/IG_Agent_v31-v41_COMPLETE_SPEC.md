# IG Agent v31–v41 Complete Specification

**Status:** Shipped (as implemented on `main`)  
**Scope:** Strategy intelligence stack, GUI observability plane, execution guard binding, macOS launcher  
**Authority:** This document supersedes informal phase notes for v32–v41. v31 app-mode detail remains in `docs/V31_APP_MODE_CONTRACT.md`.

---

## 1. Executive summary

IG Agent v31–v41 adds a **layered strategy intelligence stack** on top of the existing v29.1/v30 trading core. The stack is split into two planes:

| Plane | Role | Affects live orders? |
|-------|------|----------------------|
| **Advisory** | Computes recommendations, biases, routes, and governance adjustments | **No** — exposed on `/api/gui_status` only |
| **Execution binding** | Guards dispatch on Path A, Micro, and Path B | **Yes** — blocks or allows dispatch only |

Phases v33–v41 are **advisory-only**. Phases v31 controller, v32 hard enforcement, v31 soft enforcement, and v40 unified routing provide **execution guards** that gate dispatch without modifying signals, sizing math, REST payloads, or `LiveExecutor` internals.

**Boot invariant (P0):** Unified execution route cache is warmed at post-ready via `warm_unified_execution_route_cache()` so v40 guards are active before the first dispatch cycle, without requiring a prior `/api/gui_status` poll.

---

## 2. Design principles

1. **Advisory by default** — New layers read upstream state and emit JSON; they do not write config or mutate risk limits.
2. **Explicit execution binding** — Only `strategy_controller`, `strategy_enforcement` (soft), `hard_enforcement`, and `unified_execution` guards may block dispatch.
3. **Fail-open on guard errors (Path A)** — `execution_engine.py` wraps each guard in `try/except`; exceptions allow dispatch. Micro and Path B differ slightly (see §8).
4. **Fail-open on cold unified cache (pre-P0 only)** — Without boot warm-up, `_ROUTE_CACHE` empty → unified guards allow all paths. **P0 warm-up is mandatory for production launch.**
5. **GUI is the integration hub** — `build_gui_status()` orchestrates the full advisory DAG and populates unified route cache.
6. **No SQLite history mutation** — Advisory layers and launcher DEMO reset do not delete learning history tables.
7. **One session per account scope** — `session_lock` + `APP_MODE` contract (see `docs/V31_APP_MODE_CONTRACT.md`).

---

## 3. Phase map (v31–v41)

| Version | Phase | Module(s) | Binding |
|---------|-------|-----------|---------|
| **v31** | Strategy profiles | `strategy_profile.py` | Advisory (metadata) |
| **v31** | Strategy selector | `strategy_selector.py` | Advisory |
| **v31** | Strategy controller | `strategy_controller.py` | **Execution guard** |
| **v31** | Strategy transition | `strategy_transition.py` | Advisory |
| **v31** | Soft enforcement | `strategy_enforcement.py` | **Execution guard** (skipped when hard active) |
| **v31** | Session review | `session_review.py` | Advisory bundle |
| **v31** | Pipeline health / governance | `pipeline_health.py`, `pipeline_governance.py` | Advisory |
| **v31** | App mode / session lock | `app_mode.py`, `session_lock.py`, `session_identity.py` | Infrastructure |
| **v31** | macOS launcher | `macos/launcher/*` | Operator tooling |
| **v32** | Hard enforcement | `hard_enforcement.py` | **Execution guard** (overrides soft) |
| **v33** | Adaptive thresholds | `adaptive_thresholds.py` | Advisory |
| **v34** | Strategy performance memory | `strategy_performance_memory.py` | Advisory (+ in-process EMA) |
| **v35** | Regime detection | `regime_detection.py` | Advisory |
| **v36** | Regime-aware selector | `regime_aware_selector.py` | Advisory |
| **v37** | Regime risk envelope | `regime_risk_envelope.py` | Advisory |
| **v38** | Regime sizing | `regime_sizing.py` | Advisory |
| **v39** | Daily P&L targeting | `daily_pnl_targeting.py` | Advisory (partial route influence via v40) |
| **v40** | Unified execution | `unified_execution.py` | **Execution guard** (cache-dependent) |
| **v41** | Strategy governance | `strategy_governance.py` | Advisory (terminal layer) |

---

## 4. Advisory chain (`/api/gui_status`)

### 4.1 Endpoint

- **Route:** `GET /api/gui_status` (`src/api/routes.py`)
- **Auth:** Unauthenticated (listed in `auth_middleware.py` allowlist)
- **Builder:** `build_gui_status()` in `src/api/gui_status.py`
- **Boot warm-up:** `warm_unified_execution_route_cache()` — calls `build_gui_status()`, returns route count

### 4.2 Build order (dependency DAG)

```
session identity + pipeline_health + pipeline_governance
  → strategy_selector_advice
  → strategy_controller_decisions
  → strategy_transition_advice
  → strategy_enforcement_decisions (soft)
  → hard_enforcement_decisions
  → session_review_bundle (session_review, loosening_advice, self_reflection)
  → adaptive_thresholds
  → strategy_performance_bundle (memory + weighting)
  → regime_detection_bundle (detection + alignment)
  → regime_aware_strategy_selector
  → regime_risk_envelope
  → regime_sizing_advice
  → daily_pnl_targeting
  → unified_execution_route  ← populates _ROUTE_CACHE
  → strategy_governance
```

**Note:** `daily_pnl_targeting` must build before `unified_execution_route` and `strategy_governance` (progress history). Response JSON spreads `session_review_bundle` last, so `session_review` keys appear after `strategy_governance` in wire order.

### 4.3 Required GUI fields

| Field | Type | Phase |
|-------|------|-------|
| `strategy_selector_advice` | `list[dict]` per epic | v31 |
| `strategy_controller_decisions` | `list[dict]` | v31 |
| `strategy_transition_advice` | `list[dict]` | v31 |
| `strategy_enforcement_decisions` | `list[dict]` | v31 soft |
| `hard_enforcement_decisions` | `list[dict]` | v32 |
| `session_review` | `dict` | v31 |
| `loosening_advice` | `dict` | v31 |
| `self_reflection` | `dict` | v31 |
| `adaptive_thresholds` | `dict` | v33 |
| `strategy_performance_memory` | `dict` | v34 |
| `strategy_weighting_advice` | `dict` | v34 |
| `regime_detection` | `list[dict]` | v35 |
| `regime_strategy_alignment` | `list[dict]` | v35 |
| `regime_aware_strategy_selector` | `list[dict]` | v36 |
| `regime_risk_envelope` | `list[dict]` | v37 |
| `regime_sizing_advice` | `list[dict]` | v38 |
| `daily_pnl_targeting` | `dict` (session-level) | v39 |
| `unified_execution_route` | `list[dict]` | v40 |
| `strategy_governance` | `dict` | v41 |

**Infrastructure fields** (also required by macOS launcher): `trade_pipeline_health`, `pipeline_governance`, `api_feed_health`, session identity fields.

### 4.4 Common output shape pattern

Each per-epic layer typically exposes:

```json
{
  "epic": "CS.D.EURUSD.CFD.IP",
  "<domain>_flags": ["FLAG_NAME"],
  "<domain>_confidence": 0,
  "<domain>_reason": "human-readable",
  "contributing_factors": {}
}
```

`contributing_factors` is **mandatory** on v36–v41; earlier layers use flags + scalar confidence only.

---

## 5. Execution guard stack

### 5.1 Guard order (all three dispatch paths)

```
1. strategy_controller.guard_* 
2. hard_enforcement.hard_guard_* 
3. strategy_enforcement.soft_guard_*  (only if hard inactive)
4. unified_execution.unified_guard_*
```

| Path | File | Rejection reason |
|------|------|------------------|
| PATH_A | `src/execution/execution_engine.py` | `blocked_by_strategy_controller`, `hard_blocked_by_strategy_enforcement`, `soft_blocked_by_strategy_enforcement`, `blocked_by_unified_execution_route` |
| MICRO | `src/runtime/trade_manager.py` | same pattern |
| PATH_B | `src/runtime/dual_core_execution.py` | same pattern |

### 5.2 Cache refresh behaviour

| Layer | Auto-rebuild on guard miss? | TTL |
|-------|----------------------------|-----|
| Controller | Yes | 1s |
| Hard | Yes | 1s |
| Soft | Yes | 1s |
| Unified | **No** (cache set only by `build_unified_execution_routes`) | `_ROUTE_CACHE_TTL_SEC` defined (1s) but not enforced on read |

### 5.3 P0 boot warm-up

**When:** First action in `start_post_ready_services()` after G5 READY (skipped in `IG_TEST_HARNESS=1`).

**What:** `warm_unified_execution_route_cache()` → full `build_gui_status()` → `build_unified_execution_routes()` → `_ROUTE_CACHE`.

**Log line:** `post-ready: unified execution route cache warmed (N route(s))`

**Failure:** Fail-open — logs skip, does not block boot.

---

## 6. Phase specifications

### 6.1 v31 — Strategy controller (execution binding)

- **Module:** `src/runtime/strategy_controller.py`
- **Ownership:** SCALP, MOMENTUM, SWING, ROTATION, STAND_DOWN per epic
- **Guards:** `guard_path_a_execution`, `guard_micro_dispatch`, `guard_path_b_handoff`
- **Output:** `strategy_controller_decisions` with `ownership`, `allowed_paths`, `blocked_paths`, `enforcement_flags`, `confidence`

### 6.2 v31 — Soft enforcement (Phase 1 binding)

- **Module:** `src/runtime/strategy_enforcement.py`
- **Behaviour:** Soft path gating when hard enforcement inactive
- **Skipped when:** `is_hard_enforcement_active(epic)` is true

### 6.3 v32 — Hard enforcement (execution binding)

- **Module:** `src/runtime/hard_enforcement.py`
- **Behaviour:** Hard block/allow path sets per epic; `active` flag
- **Overrides:** Soft enforcement when active
- **Auto-build:** `build_hard_enforcement_decisions()` on cache miss (1s TTL)

### 6.4 v33 — Adaptive thresholds (advisory)

- **Module:** `src/runtime/adaptive_thresholds.py`
- **Output:** `threshold_adjustments`, `adjustment_flags`, `adjustment_confidence`
- **Baseline:** `BASELINE_THRESHOLDS` constant
- **Not applied to:** Live gate math in execution paths

### 6.5 v34 — Strategy performance memory (advisory)

- **Module:** `src/runtime/strategy_performance_memory.py`
- **Output:** `strategy_performance_memory` (win rates, regime performance), `strategy_weighting_advice`
- **State:** In-process EMA; `reset_*_for_tests()` hooks

### 6.6 v35 — Regime detection (advisory)

- **Module:** `src/runtime/regime_detection.py`
- **Regimes:** `TREND`, `CHOP`, `REVERSAL`, `LOW_VOL`, `EXTREME_VOL`, `LIQUIDITY_DROP`, `UNKNOWN`
- **Output:** `regime_detection`, `regime_strategy_alignment`

### 6.7 v36 — Regime-aware selector (advisory)

- **Module:** `src/runtime/regime_aware_selector.py`
- **Output:** `recommended_profile` per epic — **canonical profile for v40 routing**
- **Includes:** `contributing_factors`, `selector_flags`, `selector_confidence`

### 6.8 v37 — Regime risk envelope (advisory)

- **Module:** `src/runtime/regime_risk_envelope.py`
- **Profiles:** `TIGHT`, `MEDIUM`, `WIDE`, `STRUCTURAL`
- **Output:** `risk_profile`, `risk_flags`, `risk_confidence`, `contributing_factors`

### 6.9 v38 — Regime sizing (advisory)

- **Module:** `src/runtime/regime_sizing.py`
- **Output:** `recommended_size_factor` (0.0–1.0), `sizing_flags`, `sizing_confidence`
- **Not wired to:** `risk_manager` or lot sizing (observational only)

### 6.10 v39 — Daily P&L targeting (advisory)

- **Module:** `src/runtime/daily_pnl_targeting.py`
- **Default target:** 1000 points (`DAILY_PNL_TARGET_POINTS` env override)
- **Output:** `progress_ratio`, `recommended_bias` (`stand_down_bias`, sizing/risk biases), `bias_flags`, `bias_confidence`
- **Route influence:** `stand_down_bias ≥ 0.35` → unified route `NONE` when cache warm

### 6.11 v40 — Unified execution (routing + guard)

- **Module:** `src/runtime/unified_execution.py`

**Profile → primary path:**

| Profile | Primary path |
|---------|--------------|
| SCALP | MICRO |
| MOMENTUM | PATH_A |
| SWING | PATH_A |
| ROTATION | PATH_B_SWEEP |
| STAND_DOWN | NONE |

**Modifiers:**
- Hard/soft path blocking with profile-specific fallbacks
- Regime suppression: `EXTREME_VOL`, `LIQUIDITY_DROP` → NONE
- Daily `stand_down_bias ≥ 0.35` → NONE
- Feed degraded → PATH_B_SWEEP downgraded to MICRO
- Confidence blend: selector 30%, regime 25%, risk 20%, sizing 15%

**Guards:** `unified_guard_path_a_execution`, `unified_guard_micro_dispatch`, `unified_guard_path_b_handoff`

**Cold cache:** `_path_allowed_by_route` returns `(True, "")` when no cache row — **fail-open**.

### 6.12 v41 — Strategy governance (advisory, terminal)

- **Module:** `src/runtime/strategy_governance.py`
- **State:** Cross-session `_STATE` (regime observations, progress ratios, enforcement samples, drawdown samples)
- **Not consumed by:** Execution or adaptive thresholds in same session (no feedback loop)

**Output:**

```json
{
  "governance_adjustments": {
    "strategy_bias_adjustments": { "SCALP": 0.0, "MOMENTUM": 0.0, "SWING": 0.0, "ROTATION": 0.0 },
    "threshold_adjustments": {},
    "risk_bias_adjustments": { "tighten": 0.0, "loosen": 0.0 },
    "sizing_bias_adjustments": { "increase": 0.0, "decrease": 0.0 },
    "regime_sensitivity_adjustments": {},
    "stand_down_sensitivity_adjustments": 0.0
  },
  "governance_confidence": 0,
  "governance_reason": "",
  "governance_flags": [],
  "contributing_factors": {}
}
```

**Governance rules:**

| Rule | Condition | Actions | Flag |
|------|-----------|---------|------|
| Long-term SCALP bias | SCALP win rate strongest ≥ 58% | +SCALP bias, lower SCALP thresholds | `LONG_TERM_SCALP_BIAS` |
| Long-term MOMENTUM bias | MOMENTUM strongest ≥ 58% | +MOMENTUM bias, lower MOMENTUM thresholds | `LONG_TERM_MOMENTUM_BIAS` |
| Long-term SWING bias | SWING strongest ≥ 58% | +SWING bias | `LONG_TERM_SWING_BIAS` |
| Long-term ROTATION bias | ROTATION strongest ≥ 58% | +ROTATION bias | `LONG_TERM_ROTATION_BIAS` |
| TREND persistence | ≥3 in tail | +MOMENTUM, −SCALP | `REGIME_PERSISTENCE_TREND` |
| CHOP persistence | ≥3 in tail | +SCALP, −MOMENTUM | `REGIME_PERSISTENCE_CHOP` |
| REVERSAL persistence | ≥3 in tail | +ROTATION, −SWING | `REGIME_PERSISTENCE_REVERSAL` |
| LOW_VOL persistence | ≥3 in tail | +SWING, −SCALP | `REGIME_PERSISTENCE_LOW_VOL` |
| Drawdown protection | avg drawdown ≥ 4% | tighten risk, +stand-down, −sizing, raise thresholds | `DRAWDOWN_CYCLE_PROTECTION` |
| Target ahead | avg progress ≥ 0.75 | reduce aggressiveness, tighten risk | `TARGET_HISTORY_AHEAD` |
| Target behind | avg progress < 0.30 | increase aggressiveness, loosen risk, −stand-down | `TARGET_HISTORY_BEHIND` |
| Enforcement conflict | blocked profile frequency | −profile bias, raise thresholds, +stand-down | `ENFORCEMENT_CONFLICT_HISTORY` |
| Session instability | stability < 55 or risk ≥ 55 | tighten thresholds/risk, −sizing, +stand-down | `SESSION_INSTABILITY_TIGHTEN` |

**Confidence model (weighted, clamped 0–100):**

| Component | Weight |
|-----------|--------|
| `long_term_performance_confidence` | 35% |
| `regime_persistence_confidence` | 25% |
| `drawdown_cycle_confidence` | 20% |
| `daily_target_history_confidence` | 10% |
| `enforcement_history_confidence` | 10% |

Exposed in `contributing_factors.governance_confidence_components`.

---

## 7. macOS launcher contract

**Entrypoint:** `macos/launcher/launch_agent.sh` (not `launch_agent.py`)

| Phase | Action |
|-------|--------|
| STOP | `mark_manual_stop` → `scripts/stop.sh` → verify :8080 free |
| CLEAN | Purge `__pycache__`, remove stale locks |
| RESET | DEMO: strategy cache + daily P&L baseline reset |
| START | `scripts/start.sh --mode DEMO` (297-test pytest gate + supervisor) |
| VERIFY | Poll `/api/health` (G5) + `/api/gui_status` (all `REQUIRED_GUI_FIELDS`) |
| GUI | Open dashboard |

**Test gate:** 28 test files including all v31–v41 phase tests (`scripts/start.sh`).

---

## 8. Boot sequence

```
Gate 1–4 (preflight, credentials, hub, loops materialize)
  → Gate 5 READY (loops accepting ticks)
  → start_post_ready_services()
      1. warm_unified_execution_route_cache()     ← P0
      2. KernelInterceptor
      3. Schedulers / monitors / alpha matrix / etc.
      4. DualCoreCoordinator + Path B arming        ← after warm-up
```

**Anti-zombie protocol** (operator restart):

```bash
PYTHONPATH=src .venv/bin/python3 -c "from system.shutdown_cleanup import mark_manual_stop; mark_manual_stop(source='operator_restart')"
kill -TERM <main_pid>   # wait ≤30s, verify lsof -iTCP:8080
find src -type d -name __pycache__ -prune -exec rm -rf {} +
rm -f src/data/.ig_agent_v29.lock
```

---

## 9. Test coverage

| Test file | Phase |
|-----------|-------|
| `tests/test_strategy_profile.py` | v31 |
| `tests/test_strategy_selector.py` | v31 |
| `tests/test_strategy_controller.py` | v31 |
| `tests/test_strategy_transition.py` | v31 |
| `tests/test_strategy_enforcement.py` | v31 soft |
| `tests/test_session_review.py` | v31 |
| `tests/test_hard_enforcement.py` | v32 |
| `tests/test_adaptive_thresholds.py` | v33 |
| `tests/test_strategy_performance_memory.py` | v34 |
| `tests/test_regime_detection.py` | v35 |
| `tests/test_regime_aware_selector.py` | v36 |
| `tests/test_regime_risk_envelope.py` | v37 |
| `tests/test_regime_sizing.py` | v38 |
| `tests/test_daily_pnl_targeting.py` | v39 |
| `tests/test_unified_execution.py` | v40 |
| `tests/test_unified_routing_boot_warmup.py` | P0 |
| `tests/test_strategy_governance.py` | v41 |
| `tests/test_full_system_stress.py` | Cross-phase chain |

**P1 fix:** Soft-enforcement engine test resets hard enforcement and unified cache in fixture to prevent auto-build interference.

---

## 10. Known gaps and v42+ roadmap

| Gap | Severity | Recommendation |
|-----|----------|----------------|
| Unified cache no TTL on guard reads | Low | Enforce `_ROUTE_CACHE_TTL_SEC` or auto-rebuild like hard/soft |
| Governance not fed back into thresholds | Medium | v42 closed-loop with operator gate |
| Sizing advice not bound to `risk_manager` | Medium | v43 opt-in bounded multiplier |
| Profile divergence (controller vs regime selector) | Low | Document canonical source per layer |
| Pytest teardown hang (~10 min suite) | High (ops) | Investigate `multiprocessing.resource_tracker` / feed hub shutdown |
| `scripts/watchdog.sh` can restart agent during launcher pytest | High (ops) | Ensure `manual_stop` blocks watchdog during launcher |
| v39 points proxy (GBP×10) | Low | Wire real points ledger |
| No formal v32–v41 docs until this file | — | **Resolved by this spec** |

---

## 11. Safety constraints (immutable)

The following must **not** be modified by v31–v41 layers without spec update:

- Hard risk limits (`max_daily_loss_gbp`, REST budget, instance lock semantics)
- Signal generation in `signal_engine.py` / `trading_loop.py`
- `LiveExecutor` internals and REST client wire format
- `risk_manager` sizing math (advisory sizing is observational only)
- Path A/B micro-dispatch plumbing (guards may only allow/block)

---

## 12. Related documents

| Document | Role |
|----------|------|
| `docs/V31_APP_MODE_CONTRACT.md` | APP_MODE, session lock, data roots |
| `docs/V31_RUNTIME_MODE_MAP.md` | Internal plane derivation |
| `docs/V31_E2E_ARCHITECTURE_AND_GAPS.md` | Live canary E2E gaps |
| `IG_Agent_v29.1_COMPLETE_SPEC.md` | Core trading pipeline (v29.1 authority) |
| `macos/README.md` | Launcher operator guide |

---

*Last updated: 2026-06-29 — reflects P0 unified boot warm-up, P1 test isolation, and v41 governance rules as shipped.*
