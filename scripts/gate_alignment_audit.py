#!/usr/bin/env python3
"""Audit config/gate alignment — run before live sessions and after config changes."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.learning_store import LearningStore
from system.config_loader import ConfigLoader
from system.gate_coherence import (
    audit_trading_readiness,
    format_report,
    run_scheduled_coherence_check,
)
from system.paths import data_dir
from trading.points_engine import PointsEngine


def main() -> int:
    parser = argparse.ArgumentParser(description="Gate alignment audit")
    parser.add_argument(
        "--repair-db",
        action="store_true",
        help="Repair corrupt trade rows (empty epic, stop=0)",
    )
    parser.add_argument(
        "--no-repair",
        action="store_true",
        help="Audit only — do not modify SQLite",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write data_lake/state/gate_coherence_snapshot.json",
    )
    args = parser.parse_args()

    if args.write:
        report = run_scheduled_coherence_check(
            repair_db=args.repair_db and not args.no_repair,
            alert_on_critical=False,
        )
    else:
        cfg = ConfigLoader().load()
        store = LearningStore(str(data_dir() / "learning_db.sqlite3"))
        store.connect()
        points = PointsEngine(store)
        report = audit_trading_readiness(
            cfg,
            store,
            points_state=points.get_state(),
            repair_db=args.repair_db or not args.no_repair,
            per_market=True,
        )
        store.close()
    print(format_report(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
