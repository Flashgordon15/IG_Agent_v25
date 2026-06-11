#!/usr/bin/env python3
"""
IG Agent v25 — pre-flight checks against a running agent.

Static checks (always):
  - Session summary anti-mock / integrity

Live agent checks (--live, agent must be running):
  - Gate evaluation within 60s
  - Live market data fresh
  - Startup stream_ready gate logged

Usage:
  PYTHONPATH=src python3 scripts/pre_flight_check.py
  PYTHONPATH=src python3 scripts/pre_flight_check.py --live
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.pre_flight_checks import pre_flight_summary, run_all_pre_flight_checks


def main() -> int:
    parser = argparse.ArgumentParser(description="IG Agent v25 pre-flight checks")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Require live gate activity and fresh market data",
    )
    parser.add_argument(
        "--max-gate-age",
        type=float,
        default=60.0,
        help="Max seconds since last gate evaluation (default 60)",
    )
    args = parser.parse_args()

    results = run_all_pre_flight_checks(
        require_live_agent=args.live,
        max_gate_age_sec=args.max_gate_age,
    )
    summary = pre_flight_summary(results)

    print()
    print("IG Agent v25 — PRE-FLIGHT CHECK")
    print("=" * 44)
    for row in summary["results"]:
        mark = "PASS" if row["passed"] else "FAIL"
        line = f"[{row['id']}] {mark} — {row['description']}"
        if row["reason"]:
            line += f" ({row['reason']})"
        print(line)
    print("=" * 44)
    status = "OK" if summary["ok"] else "FAIL"
    print(f"Result: {summary['passed']}/{summary['total']} {status}")
    print()
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
