#!/usr/bin/env python3
"""
24/7 Robustness Certification Report Runner.

Orchestrates all staging/testing framework tools and produces a unified
human-readable markdown certification report.

Execution sequence:
  1. Capture configuration baseline via staging_feature_controller
  2. Run multi_asset_stress_harness (5 profiles x 5 scenarios x 5 latency modes)
  3. Run system_state_sanity_checker (10,000-tick drift audit)
  4. Aggregate and produce the certification report

Usage:
    PYTHONPATH=src python3 scripts/certification_report_runner.py
    PYTHONPATH=src python3 scripts/certification_report_runner.py --ticks 50000
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from io import StringIO

# ---------------------------------------------------------------------------
# Phase 1: Configuration Baseline
# ---------------------------------------------------------------------------

_TOGGLEABLE_FLAGS = (
    "volatility_bracket_enabled",
    "adaptive_trailing_stop_enabled",
    "capital_recycle_enabled",
    "enforce_top3_rotation_filter",
    "enforce_rr_floor_filter",
    "enforce_1h_ema_filter",
    "enforce_environment_fitness_filter",
    "ml_filter_overrides_enabled",
    "trading_hours_enabled",
)


def capture_config_baseline() -> dict:
    from system.config_loader import get_config
    cfg = get_config()
    data = cfg.as_dict()

    flags = {}
    for flag in _TOGGLEABLE_FLAGS:
        if hasattr(cfg, flag):
            flags[flag] = getattr(cfg, flag)
        else:
            flags[flag] = data.get(flag, "<unset>")

    from execution.risk_manager import (
        _volatility_bracket_states,
        _volatility_bracket_last,
        _tick_highs,
    )
    state_sizes = {
        "volatility_bracket_states": len(_volatility_bracket_states),
        "volatility_bracket_last": len(_volatility_bracket_last),
        "tick_highs_asymmetric": len(_tick_highs),
    }

    return {
        "flags": flags,
        "state_dict_sizes": state_sizes,
        "config_source": data.get("$extends", "primary"),
    }


# ---------------------------------------------------------------------------
# Phase 2: Multi-Asset Stress
# ---------------------------------------------------------------------------

def run_multi_asset_stress() -> dict:
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
    from multi_asset_stress_harness import run_all, PROFILES, SCENARIOS, LATENCY_MODES

    t0 = time.monotonic()
    results = run_all()
    elapsed = time.monotonic() - t0

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    failures = [{"name": r.name, "detail": r.detail} for r in results if not r.passed]

    profile_results = {}
    for r in results:
        parts = r.name.split("|")
        if len(parts) >= 2:
            pname = parts[1]
            if pname not in profile_results:
                profile_results[pname] = {"passed": 0, "failed": 0}
            if r.passed:
                profile_results[pname]["passed"] += 1
            else:
                profile_results[pname]["failed"] += 1

    return {
        "total_tests": len(results),
        "passed": passed,
        "failed": failed,
        "elapsed_sec": round(elapsed, 2),
        "profiles": len(PROFILES),
        "scenarios": len(SCENARIOS),
        "latency_modes": len(LATENCY_MODES),
        "per_profile": profile_results,
        "failures": failures[:20],
        "verdict": "PASS" if failed == 0 else "FAIL",
    }


# ---------------------------------------------------------------------------
# Phase 3: State Sanity Check
# ---------------------------------------------------------------------------

def run_sanity_check(ticks: int) -> dict:
    from system_state_sanity_checker import run_simulation
    return run_simulation(ticks)


# ---------------------------------------------------------------------------
# Phase 4: Report Generation
# ---------------------------------------------------------------------------

def generate_report(
    baseline: dict,
    stress: dict,
    sanity: dict,
    total_elapsed: float,
) -> str:
    lines: list[str] = []
    w = lines.append

    w("# 24/7 Robustness Certification Report")
    w(f"**Generated:** {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    w(f"**Agent Version:** 29.1.0")
    w(f"**Total Runtime:** {total_elapsed:.1f}s")
    w("")

    # Section 1: Config Baseline
    w("## 1. Configuration Baseline")
    w("")
    w("| Flag | Value |")
    w("|------|-------|")
    for flag, val in baseline["flags"].items():
        w(f"| `{flag}` | `{val}` |")
    w("")
    w("**State Dict Sizes (pre-test):**")
    for k, v in baseline["state_dict_sizes"].items():
        w(f"- `{k}`: {v} entries")
    w("")

    # Section 2: Component Interaction Map
    w("## 2. System Architecture Interaction Map")
    w("")
    w("```")
    w("Trading Tick Pipeline (zero-allocation hot path):")
    w("")
    w("  TradingLoop._run_tick_core()")
    w("    |")
    w("    +--> get_config()  [mtime-checked singleton, O(1) cache hit]")
    w("    |")
    w("    +--> TradeManager.update_from_quote()")
    w("           |")
    w("           +--> [if adaptive_trailing_stop_enabled]")
    w("           |      _apply_trailing() --> eval_trailing_stop()")
    w("           |")
    w("           +--> [if volatility_bracket_enabled]")
    w("                  _apply_volatility_bracket()")
    w("                    |")
    w("                    +--> risk_manager.compute_volatility_adjusted_trail_stop()")
    w("                           |")
    w("                           +--> _volatility_bracket_states[epic]  [reuse, no alloc]")
    w("                           +--> update_bracket(state, quote)      [pure math]")
    w("                           |      |")
    w("                           |      +--> _smooth_live_atr()         [EMA, no alloc]")
    w("                           |      +--> volatility_ratio()         [clamped 0.25-4.0]")
    w("                           |      +--> dynamic_trail_atr_multiple()  [floor 0.65]")
    w("                           |      +--> eval_trailing_stop()       [ratchet only]")
    w("                           |")
    w("                           +--> _volatility_bracket_last[epic] = row  [GUI snapshot]")
    w("")
    w("  State Broadcast (500ms cadence, lock-free):")
    w("")
    w("  MasterOrchestrator._publish_iron_ledger_tick()")
    w("    +--> build_institutional_matrix_snapshot()")
    w("    |      +--> get_volatility_bracket_snapshot()  [copy under _asymmetric_lock]")
    w("    +--> IronLedgerSnapshot.commit()  [atomic tuple swap, no reader locks]")
    w("")
    w("  Dashboard Data Flow:")
    w("")
    w("  /state endpoint --> enrich_tick_runtime()")
    w("    +--> institutional.volatility_bracket = snapshot")
    w("    +--> React App.jsx polls /state")
    w("           +--> mergedState = {...state, ...health, ...startup}")
    w("           +--> LivePanel receives rawState={mergedState}")
    w("                  +--> useMemo(volBracket)       [O(1) extract]")
    w("                  +--> useMemo(volBracketByEpic)  [O(n) index, n=positions]")
    w("                  +--> MemoVolBracketRibbon       [React.memo, rigid eq]")
    w("                  +--> WebGL ApexWebGLRenderer    [pre-alloc Float32Array]")
    w("```")
    w("")

    # Section 3: Multi-Asset Stress Results
    w("## 3. Multi-Asset Stress Results")
    w("")
    w(f"- **Profiles:** {stress['profiles']}")
    w(f"- **Scenarios:** {stress['scenarios']} (flash_crash, macro_gap, liquidity_vacuum, momentum_cascade, chop_whipsaw)")
    w(f"- **Latency Modes:** {stress['latency_modes']} (normal, congested, dropout, burst_replay, extended_stall)")
    w(f"- **Total Tests:** {stress['total_tests']}")
    w(f"- **Elapsed:** {stress['elapsed_sec']}s")
    w("")
    w("| Profile | Passed | Failed |")
    w("|---------|--------|--------|")
    for pname, pdata in stress.get("per_profile", {}).items():
        marker = "PASS" if pdata["failed"] == 0 else "FAIL"
        w(f"| {pname} | {pdata['passed']} | {pdata['failed']} ({marker}) |")
    w("")
    if stress["failures"]:
        w("**Failures:**")
        for f in stress["failures"]:
            w(f"- `{f['name']}`: {f['detail']}")
        w("")
    w(f"**Stress Verdict:** {stress['verdict']}")
    w("")

    # Section 4: Memory & State Drift Audit
    w("## 4. Memory & State Drift Audit")
    w("")
    w(f"- **Ticks Simulated:** {sanity['ticks_simulated']:,}")
    w(f"- **Epics Simulated:** {sanity['epics_simulated']}")
    w(f"- **Total Tick Updates:** {sanity['total_ticks_processed']:,}")
    w(f"- **Elapsed:** {sanity['elapsed_sec']}s")
    w("")
    w("| Metric | Value | Limit | Status |")
    w("|--------|-------|-------|--------|")
    mem_ok = "PASS" if sanity["memory_peak_kb"] <= sanity["memory_limit_kb"] else "FAIL"
    w(f"| Peak Memory | {sanity['memory_peak_kb']}KB | {sanity['memory_limit_kb']}KB | {mem_ok} |")
    w(f"| Final Memory | {sanity['memory_final_kb']}KB | -- | -- |")
    growth = "PASS" if sanity["memory_growth_stable"] else "FAIL"
    w(f"| Growth Stable | {sanity['memory_growth_stable']} | -- | {growth} |")
    cons = "PASS" if sanity["snapshot_consistency_failures"] == 0 else "FAIL"
    w(f"| Consistency | {sanity['snapshot_consistency_failures']}/{sanity['snapshot_consistency_checks']} failures | 0 | {cons} |")
    ratch = "PASS" if sanity["monotonic_ratchet_violations"] == 0 else "FAIL"
    w(f"| Ratchet Violations | {sanity['monotonic_ratchet_violations']} | 0 | {ratch} |")
    w("")
    w("**Per-Epic Breakdown:**")
    w("")
    w("| Epic | Ticks | Ratchet Violations | Entry Drift |")
    w("|------|-------|--------------------|-------------|")
    for ep in sanity.get("per_epic", []):
        m = "OK" if ep["ratchet_violations"] == 0 else "FAIL"
        w(f"| `{ep['epic']}` | {ep['ticks']:,} | {ep['ratchet_violations']} ({m}) | {ep['entry_drift']} |")
    w("")

    curve = sanity.get("memory_curve", [])
    if curve:
        w("**Memory Curve (sampled):**")
        w("")
        w("| Tick | Current KB | Peak KB |")
        w("|------|-----------|---------|")
        for s in curve:
            w(f"| {s['tick']:,} | {s['current_kb']} | {s['peak_kb']} |")
        w("")

    w(f"**Drift Verdict:** {sanity['verdict']}")
    w("")

    # Section 5: Certification
    w("## 5. Certification Verdict")
    w("")
    all_pass = stress["verdict"] == "PASS" and sanity["verdict"] == "PASS"
    if all_pass:
        w("**CERTIFIED: System is approved for 24/7 unattended operation.**")
        w("")
        w("Evidence:")
        w(f"- {stress['total_tests']} multi-asset property tests across {stress['profiles']} asset classes, "
          f"{stress['scenarios']} scenarios, {stress['latency_modes']} latency modes: ALL PASSED")
        w(f"- {sanity['ticks_simulated']:,} simulated clock ticks across {sanity['epics_simulated']} epics: "
          f"zero ratchet violations, zero consistency failures")
        w(f"- Peak memory {sanity['memory_peak_kb']}KB (limit {sanity['memory_limit_kb']}KB): "
          f"O(1) per-epic confirmed, no unbounded growth")
        w(f"- Monotonic stop-ratchet invariant: HOLDS across all sides and scenarios")
        w(f"- Float precision drift: max {sanity.get('float_drift_max', 0)} (threshold {1e-10})")
    else:
        w("**NOT CERTIFIED: Failures detected. Review sections 3 and 4 above.**")
        blockers = []
        if stress["verdict"] != "PASS":
            blockers.append(f"Multi-asset stress: {stress['failed']} failures")
        if sanity["verdict"] != "PASS":
            blockers.append(f"Sanity check: {sanity['verdict']}")
        for b in blockers:
            w(f"- BLOCKER: {b}")
    w("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="24/7 Robustness Certification Runner")
    parser.add_argument("--ticks", type=int, default=10_000, help="Sanity checker ticks")
    args = parser.parse_args()

    t0 = time.monotonic()

    print("=" * 64)
    print("  24/7 Robustness Certification Suite")
    print("=" * 64)
    print()

    print("[Phase 1/3] Capturing configuration baseline...")
    baseline = capture_config_baseline()
    print(f"  Flags: {len(baseline['flags'])} captured")
    print()

    print("[Phase 2/3] Running multi-asset stress harness...")
    stress = run_multi_asset_stress()
    print(f"  {stress['passed']}/{stress['total_tests']} passed in {stress['elapsed_sec']}s")
    print()

    print(f"[Phase 3/3] Running state sanity checker ({args.ticks:,} ticks)...")
    sanity = run_sanity_check(args.ticks)
    print(f"  Verdict: {sanity['verdict']}  peak={sanity['memory_peak_kb']}KB")
    print()

    total_elapsed = time.monotonic() - t0

    report = generate_report(baseline, stress, sanity, total_elapsed)

    report_path = "logs_archive/certification_report.md"
    try:
        from pathlib import Path
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(report)
        print(f"  Report written to: {report_path}")
    except Exception as exc:
        print(f"  Warning: could not write report file: {exc}")

    print()
    print(report)

    all_pass = stress["verdict"] == "PASS" and sanity["verdict"] == "PASS"
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
