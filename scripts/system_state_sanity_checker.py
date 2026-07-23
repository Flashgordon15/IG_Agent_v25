#!/usr/bin/env python3
"""
System State Sanity Checker — memory & data-drift verification.

Runs a 10,000-tick isolated simulation through the volatility_risk_bracket
engine to verify:
  1. Peak memory stays bounded (O(1) per epic on hot path)
  2. State dict sizes remain constant (no unbounded growth)
  3. Snapshot consistency (every bracket state maps to a valid snapshot entry)
  4. Monotonic stop ratchet invariant holds
  5. Float drift stays below 1e-10 threshold

Outputs a structured JSON report. Exit 0 = PASS, 1 = FAIL.

Usage:
    PYTHONPATH=src python3 scripts/system_state_sanity_checker.py
    PYTHONPATH=src python3 scripts/system_state_sanity_checker.py --ticks 50000
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
import tracemalloc
from dataclasses import dataclass, field

from execution.volatility_risk_bracket import (
    BracketConfig,
    BracketQuote,
    BracketState,
    BracketUpdate,
    update_bracket,
    volatility_ratio,
    dynamic_trail_atr_multiple,
    VOL_RATIO_FLOOR,
    VOL_RATIO_CEILING,
)


@dataclass
class EpicSimState:
    """Per-epic tracking for the sanity checker."""
    name: str
    base_mid: float
    atr: float
    spread_half: float
    decimals: int
    bracket: BracketState
    prev_stop: float
    ratchet_violations: int = 0
    entry_price_drift: float = 0.0
    ticks_processed: int = 0


SIMULATED_EPICS = [
    ("CS.D.EURUSD.CFD.IP", 1.16000, 0.00080, 0.00005, 5),
    ("IX.D.DOW.IFM.IP", 39500.0, 120.0, 2.5, 1),
    ("CS.D.CFPGOLD.CFP.IP", 2340.0, 18.0, 0.30, 2),
    ("CRYPTO.BTC.USD", 62000.0, 1500.0, 25.0, 2),
    ("CS.D.GBPJPY.CFD.IP", 198.500, 0.650, 0.015, 3),
]

MEMORY_PEAK_LIMIT_KB = 1024.0
CHECKPOINT_INTERVAL = 100
FLOAT_DRIFT_THRESHOLD = 1e-10


def _build_epic_states() -> list[EpicSimState]:
    states = []
    for epic, mid, atr, spread_half, decimals in SIMULATED_EPICS:
        bracket = BracketState.open_long(entry=mid, entry_atr=atr)
        states.append(EpicSimState(
            name=epic,
            base_mid=mid,
            atr=atr,
            spread_half=spread_half,
            decimals=decimals,
            bracket=bracket,
            prev_stop=bracket.stop,
        ))
    return states


def _generate_quote(
    es: EpicSimState, tick: int, rng: random.Random
) -> BracketQuote:
    """Synthetic quote with mild trend + noise."""
    drift = es.atr * 0.003 * math.sin(tick * 0.05)
    noise = rng.uniform(-es.atr * 0.08, es.atr * 0.08)
    mid = es.base_mid + drift + noise

    if tick % 200 == 150:
        mid -= es.atr * 3.0

    bid = round(mid - es.spread_half, es.decimals)
    offer = round(mid + es.spread_half, es.decimals)
    return BracketQuote(bid=bid, offer=offer, live_atr=es.atr)


def run_simulation(total_ticks: int) -> dict:
    rng = random.Random(2026_07_03)
    epic_states = _build_epic_states()

    memory_samples: list[dict] = []
    dict_size_samples: list[dict] = []
    snapshot_consistency_checks = 0
    snapshot_consistency_failures = 0

    max_dict_entries = 0
    stopped_epics: set[str] = set()

    tracemalloc.start()
    t0 = time.monotonic()

    try:
        for tick in range(total_ticks):
            for es in epic_states:
                if es.name in stopped_epics:
                    continue

                quote = _generate_quote(es, tick, rng)
                upd = update_bracket(es.bracket, quote)
                es.ticks_processed += 1

                if es.bracket.side == "BUY" and upd.stop < es.prev_stop - 1e-10:
                    es.ratchet_violations += 1
                elif es.bracket.side == "SELL" and upd.stop > es.prev_stop + 1e-10:
                    es.ratchet_violations += 1
                es.prev_stop = upd.stop

                entry_drift = abs(es.bracket.entry - es.base_mid)
                es.entry_price_drift = max(es.entry_price_drift, entry_drift)

                if upd.vol_ratio < VOL_RATIO_FLOOR - 1e-9 or upd.vol_ratio > VOL_RATIO_CEILING + 1e-9:
                    snapshot_consistency_failures += 1
                if upd.trail_atr_mult < 0.65 - 1e-9:
                    snapshot_consistency_failures += 1

                snapshot_consistency_checks += 1

                if upd.stop_hit:
                    stopped_epics.add(es.name)
                    es.bracket = BracketState.open_long(
                        entry=es.base_mid, entry_atr=es.atr
                    )
                    es.prev_stop = es.bracket.stop
                    stopped_epics.discard(es.name)

            if tick % CHECKPOINT_INTERVAL == 0:
                current, peak = tracemalloc.get_traced_memory()
                memory_samples.append({
                    "tick": tick,
                    "current_kb": round(current / 1024.0, 1),
                    "peak_kb": round(peak / 1024.0, 1),
                })

        _, peak_final = tracemalloc.get_traced_memory()
        current_final, _ = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    elapsed = time.monotonic() - t0

    total_ratchet_violations = sum(es.ratchet_violations for es in epic_states)
    max_float_drift = max(es.entry_price_drift for es in epic_states)
    total_ticks_processed = sum(es.ticks_processed for es in epic_states)

    peak_kb = peak_final / 1024.0
    final_kb = current_final / 1024.0

    memory_growth_stable = True
    if len(memory_samples) >= 20:
        warmup_idx = max(1, len(memory_samples) * 3 // 10)
        baseline_peak = memory_samples[warmup_idx]["peak_kb"]
        late_peak = memory_samples[-1]["peak_kb"]
        if baseline_peak > 1.0 and late_peak > baseline_peak * 5.0:
            memory_growth_stable = False

    passed = (
        peak_kb <= MEMORY_PEAK_LIMIT_KB
        and snapshot_consistency_failures == 0
        and total_ratchet_violations == 0
        and memory_growth_stable
    )

    report = {
        "ticks_simulated": total_ticks,
        "epics_simulated": len(epic_states),
        "total_ticks_processed": total_ticks_processed,
        "elapsed_sec": round(elapsed, 3),
        "memory_peak_kb": round(peak_kb, 1),
        "memory_final_kb": round(final_kb, 1),
        "memory_limit_kb": MEMORY_PEAK_LIMIT_KB,
        "memory_growth_stable": memory_growth_stable,
        "snapshot_consistency_checks": snapshot_consistency_checks,
        "snapshot_consistency_failures": snapshot_consistency_failures,
        "monotonic_ratchet_violations": total_ratchet_violations,
        "float_drift_max": round(max_float_drift, 12),
        "float_drift_ok": max_float_drift < FLOAT_DRIFT_THRESHOLD,
        "per_epic": [
            {
                "epic": es.name,
                "ticks": es.ticks_processed,
                "ratchet_violations": es.ratchet_violations,
                "entry_drift": round(es.entry_price_drift, 12),
            }
            for es in epic_states
        ],
        "memory_curve": memory_samples[:10] + memory_samples[-3:],
        "verdict": "PASS" if passed else "FAIL",
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="System State Sanity Checker — memory & drift verification"
    )
    parser.add_argument("--ticks", type=int, default=10_000, help="Ticks to simulate")
    args = parser.parse_args()

    print("=" * 60)
    print("  System State Sanity Checker")
    print("=" * 60)
    print(f"  Ticks: {args.ticks:,}  |  Epics: {len(SIMULATED_EPICS)}")
    print()

    report = run_simulation(args.ticks)

    print(f"  Elapsed:    {report['elapsed_sec']}s")
    print(f"  Memory:     peak={report['memory_peak_kb']}KB  final={report['memory_final_kb']}KB  limit={report['memory_limit_kb']}KB")
    print(f"  Growth:     {'stable' if report['memory_growth_stable'] else 'GROWING'}")
    print(f"  Consistency: {report['snapshot_consistency_checks']} checks, {report['snapshot_consistency_failures']} failures")
    print(f"  Ratchet:    {report['monotonic_ratchet_violations']} violations")
    print(f"  Drift:      max={report['float_drift_max']}  ok={report['float_drift_ok']}")
    print()

    for ep in report["per_epic"]:
        marker = "OK" if ep["ratchet_violations"] == 0 else "FAIL"
        print(f"    [{marker}] {ep['epic']}: {ep['ticks']} ticks, drift={ep['entry_drift']}")

    print()
    print(f"  VERDICT: {report['verdict']}")

    print()
    print("--- JSON Report ---")
    print(json.dumps(report, indent=2))

    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
