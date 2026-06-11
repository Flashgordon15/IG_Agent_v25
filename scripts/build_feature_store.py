#!/usr/bin/env python3
"""Build v26 feature CSVs from v25 feeder lake for one or more days."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "v26"))

from research.feature_store import build_day  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build data_lake/features from feeder events"
    )
    parser.add_argument("--day", default="", help="YYYY-MM-DD (default today UTC)")
    parser.add_argument("--days", type=int, default=1, help="Build last N UTC days")
    args = parser.parse_args()

    if args.day:
        days = [args.day.strip()]
    else:
        today = datetime.now(timezone.utc).date()
        days = [
            (today - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(max(1, args.days))
        ]

    for day in days:
        written = build_day(day)
        print(f"{day}:")
        for name, path in written.items():
            print(f"  {name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
