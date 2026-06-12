#!/usr/bin/env python3
"""Refresh setup_registry.json from agent-only learning_db closes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.paths import data_dir
from system.setup_registry_refresh import refresh_setup_registry_from_store


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Refresh setup registry from agent trades"
    )
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument(
        "--disable-gate",
        action="store_true",
        help="Write stats but keep registry gate disabled",
    )
    args = parser.parse_args()

    from data.learning_store import LearningStore

    store = LearningStore(data_dir() / "learning_db.sqlite3")
    payload = refresh_setup_registry_from_store(
        store,
        rolling_days=args.days,
        enabled=False if args.disable_gate else True,
    )
    print(json.dumps(payload, indent=2))
    print(f"\nBanned setups: {payload.get('banned_count')}")
    print(f"Registry gate enabled: {payload.get('enabled')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
