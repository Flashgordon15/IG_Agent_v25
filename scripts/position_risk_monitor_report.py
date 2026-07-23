#!/usr/bin/env python3
"""
Position risk monitor — audit open vs armed tracks and strategy health.

Usage:
  PYTHONPATH=src python3 scripts/position_risk_monitor_report.py
  PYTHONPATH=src python3 scripts/position_risk_monitor_report.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from api.position_risk_monitor import build_position_risk_report


def _print_text(report: dict[str, Any]) -> None:
    print("=" * 60)
    print("POSITION RISK MONITOR")
    print("=" * 60)
    print(f"Verdict:        {report.get('verdict')}")
    print(f"Broker open:    {report.get('broker_open')}")
    print(f"GBP armed:      {report.get('gbp_tracks')}")
    print(f"Virtual armed:  {report.get('virtual_tracks')}")
    print(f"Dynamic armed:  {report.get('dynamic_tracks')}")
    print(f"In profit >£1:  {report.get('winners')}")
    print(f"In loss <-£1:   {report.get('losers')}")
    print(f"Unmonitored:    {report.get('unmonitored')}")
    rec = report.get("reconcile") or {}
    if rec:
        print(f"Reconcile:      {rec}")
    print()
    print("Strategy stack:")
    for k, v in (report.get("strategy") or {}).items():
        print(f"  {k}: {v}")
    issues = report.get("issues") or []
    if issues:
        print()
        print("Issues:")
        for line in issues[:20]:
            print(f"  - {line}")
    print()
    print("Sample positions (deal, epic, pnl, armed G/V/D, peak):")
    for r in (report.get("positions") or [])[:12]:
        armed = (
            "G" if r.get("gbp_armed") else "-",
            "V" if r.get("virtual_armed") else "-",
            "D" if r.get("dynamic_armed") else "-",
        )
        print(
            f"  {r.get('deal_id','')[:12]} {str(r.get('epic',''))[-16:]:16} "
            f"pnl={r.get('pnl_gbp')} {''.join(armed)} peak={r.get('peak_profit_gbp')}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Position risk monitor report")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args()
    report = build_position_risk_report()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_text(report)
    return 0 if report.get("verdict") == "HEALTHY" else 1


if __name__ == "__main__":
    sys.exit(main())
