#!/usr/bin/env python3
"""Full v26 learning pack — refresh data + L1 + progress + learning snapshot."""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(cmd: list[str], label: str) -> int:
    print(f"\n--- {label} ---")
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        env={**dict(__import__("os").environ), "PYTHONPATH": "src:v26"},
    ).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="v26 full learning pack")
    parser.add_argument("--max-days", type=int, default=14)
    parser.add_argument("--weekly", action="store_true")
    parser.add_argument(
        "--skip-ohlc",
        action="store_true",
        help="Skip slow OHLC historical replay step",
    )
    args = parser.parse_args()

    py = sys.executable
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    steps = [
        (
            "Reprocess shadow (S1+S2+S3)",
            [
                py,
                str(ROOT / "v26" / "main.py"),
                "--mode",
                "shadow",
                "--process-day",
                "--day",
                day,
            ],
        ),
        (
            "Refresh snapshots",
            [
                py,
                str(ROOT / "scripts" / "v26_refresh_all.py"),
                "--day",
                day,
                "--days",
                str(min(args.max_days, 14)),
            ],
        ),
    ]
    if not args.skip_ohlc:
        steps.extend(
            [
                (
                    "OHLC historical replay",
                    [
                        py,
                        str(ROOT / "scripts" / "v26_ohlc_replay.py"),
                        "--write",
                    ],
                ),
                (
                    "S2 per-epic threshold tune",
                    [py, str(ROOT / "scripts" / "v26_s2_tune.py")],
                ),
                (
                    "Trail MFE/MAE tune (Japan + Gold)",
                    [py, str(ROOT / "scripts" / "v26_trail_tune.py")],
                ),
                (
                    "S4 offline retrain",
                    [py, str(ROOT / "scripts" / "v26_s4_retrain.py")],
                ),
            ]
        )
    steps.extend(
        [
            (
                "L1 replay + learning engine",
                [
                    py,
                    str(ROOT / "scripts" / "v26_l1_replay.py"),
                    "--max-days",
                    str(args.max_days),
                    "--write",
                ],
            ),
            (
                "Daily progress",
                [
                    py,
                    str(ROOT / "scripts" / "v26_progress.py"),
                    "--day",
                    day,
                    "--write",
                ],
            ),
        ]
    )
    if args.weekly:
        steps.append(
            (
                "Weekly pack",
                [py, str(ROOT / "scripts" / "v26_weekly_pack.py"), "--days", "7"],
            )
        )

    print("v26 LEARNING PACK")
    print("=" * 44)
    for label, cmd in steps:
        if _run(cmd, label) != 0:
            print(f"\nWarning: {label} returned non-zero (continuing)")

    print("\n" + "=" * 44)
    print("LEARNING PACK COMPLETE")
    print("Snapshots: data_lake/state/v26_learning_snapshot.json")
    print("           data_lake/state/v26_daily_progress.json")
    print("PROFIT tab: restart agent to load latest API payload")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
