#!/usr/bin/env python3
"""Scheduled gate coherence check — per-market rules alignment (launchd 4×/day)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.gate_coherence import format_report, run_scheduled_coherence_check


def main() -> int:
    parser = argparse.ArgumentParser(description="Scheduled gate coherence check")
    parser.add_argument(
        "--repair-db",
        action="store_true",
        help="Repair corrupt trade rows (empty epic, stop=0)",
    )
    parser.add_argument(
        "--no-repair",
        action="store_true",
        help="Audit only — do not modify SQLite (default for launchd)",
    )
    parser.add_argument(
        "--no-alert",
        action="store_true",
        help="Skip Telegram alert on CRITICAL",
    )
    args = parser.parse_args()
    repair = args.repair_db and not args.no_repair
    report = run_scheduled_coherence_check(
        repair_db=repair,
        alert_on_critical=not args.no_alert,
    )
    print(format_report(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
