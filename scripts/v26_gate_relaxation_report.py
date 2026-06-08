#!/usr/bin/env python3
"""Rank gate blockers over N days and print relaxation recommendations."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "v26"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--write", action="store_true", help="Write state JSON")
    args = parser.parse_args()

    from research.gate_relaxation_report import (
        recommend_relaxations,
        rollup_gate_blockers,
        write_gate_relaxation_report,
    )

    if args.write:
        path = write_gate_relaxation_report(days=args.days)
        print(f"Wrote {path}")

    rollup = rollup_gate_blockers(days=args.days)
    recs = recommend_relaxations(rollup)

    print(f"\n=== Gate blocker rollup ({args.days}d) ===")
    totals = rollup.get("totals") or {}
    print(
        f"Would fire: {totals.get('would_fire', 0)} | fills: {totals.get('fill_closes', 0)} | "
        f"near-miss: {totals.get('near_miss_evals', 0)} | "
        f"shadow match: {totals.get('shadow_would_trade', 0)} | "
        f"est E£: {totals.get('estimated_counterfactual_e_gbp', 0)}"
    )
    print("\nRanked blockers:")
    for row in rollup.get("ranked_blockers") or []:
        print(f"  {row['gate']:24} {row['fail_count']:6}  ({row['weight']:.0%})")

    print("\nRecommendations:")
    for r in recs:
        flag = "SAFE" if r.get("safe") else "REVIEW"
        print(f"  [{flag}] {r.get('action')}")
        if r.get("evidence"):
            print(f"         {r['evidence']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
