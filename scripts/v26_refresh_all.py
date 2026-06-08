#!/usr/bin/env python3
"""Refresh all v26 snapshots (quiet trading days — no v25 restart needed)."""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(cmd: list[str], *, label: str) -> int:
    print(f"\n--- {label} ---")
    print(" ".join(cmd))
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        env={**dict(__import__("os").environ), "PYTHONPATH": "src:v26"},
    ).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh v26 data lake snapshots")
    parser.add_argument("--day", default="", help="UTC day YYYY-MM-DD")
    parser.add_argument(
        "--days", type=int, default=14, help="Rolling days for expectancy"
    )
    parser.add_argument("--weekly", action="store_true", help="Also write weekly pack")
    args = parser.parse_args()

    day = args.day.strip() or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    py = sys.executable

    steps: list[tuple[str, list[str]]] = [
        (
            "Feature store",
            [py, str(ROOT / "scripts" / "build_feature_store.py"), "--day", day],
        ),
        (
            "Shadow compare + expectancy",
            [
                py,
                str(ROOT / "scripts" / "shadow_compare.py"),
                "--day",
                day,
                "--process",
                "--expectancy",
                "--days",
                str(args.days),
            ],
        ),
        (
            "Trade learning snapshot",
            [
                py,
                "-c",
                "from research.trade_learning import write_trade_learning_snapshot; "
                "write_trade_learning_snapshot()",
            ],
        ),
        (
            "Daily progress",
            [py, str(ROOT / "scripts" / "v26_progress.py"), "--day", day, "--write"],
        ),
    ]
    if args.weekly:
        steps.append(
            (
                "Weekly pack",
                [py, str(ROOT / "scripts" / "v26_weekly_pack.py"), "--days", "7"],
            )
        )

    print("v26 REFRESH ALL")
    print("=" * 40)
    for label, cmd in steps:
        if _run(cmd, label=label) != 0:
            print(f"\nFailed at: {label}")
            return 1
    print(
        "\nDone — PROFIT tab reads snapshots on next agent restart (or existing /api/v26/profit)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
