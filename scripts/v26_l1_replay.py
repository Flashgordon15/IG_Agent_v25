#!/usr/bin/env python3
"""Build feature store + L1 replay + certification report for all feeder days."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "v26"))

from ingest.lake_reader import events_dir
from research.l1_replay import replay_days
from research.learning_engine import build_learning_snapshot, write_learning_snapshot


def _event_days(max_days: int) -> list[str]:
    root = events_dir()
    if not root.is_dir():
        return []
    return sorted(
        (p.stem for p in root.glob("*.jsonl") if p.is_file()),
        reverse=True,
    )[:max_days]


def main() -> int:
    parser = argparse.ArgumentParser(description="v26 L1 replay + learning snapshot")
    parser.add_argument("--max-days", type=int, default=14)
    parser.add_argument("--skip-features", action="store_true")
    parser.add_argument(
        "--write", action="store_true", help="Write v26_learning_snapshot.json"
    )
    args = parser.parse_args()

    days = _event_days(args.max_days)
    if not days:
        print("No feeder event days found in data_lake/events/")
        return 1

    py = sys.executable
    if not args.skip_features:
        for day in days:
            print(f"Building features {day}...")
            subprocess.run(
                [py, str(ROOT / "scripts" / "build_feature_store.py"), "--day", day],
                cwd=str(ROOT),
                env={**dict(__import__("os").environ), "PYTHONPATH": "src:v26"},
                check=False,
            )

    replay = replay_days(days)
    print(f"\n=== L1 replay ({replay.get('days_ok')} days) ===")
    print(f"Total evals: {replay.get('total_evals')}")
    print(f"Would fire at ≥75%: {replay.get('total_would_fire_at_75')}")
    for row in replay.get("daily") or []:
        if not row.get("ok"):
            print(f"  {row.get('day')}: SKIP ({row.get('error')})")
            continue
        print(
            f"  {row['day']}: evals={row['evals']} "
            f"median={row.get('median_confidence')}% "
            f"fire@75={row.get('by_threshold', {}).get('>=75', 0)}"
        )

    snap = build_learning_snapshot(days=days)
    l1 = snap.get("l1_certification") or {}
    print("\n=== L1 certification ===")
    print(
        f"Status: {l1.get('status')} ({l1.get('days_available')}/{l1.get('days_required')} days)"
    )
    for issue in l1.get("issues") or []:
        print(f"  - {issue}")
    m = l1.get("metrics") or {}
    print(
        f"  P&L total £{m.get('total_pnl_gbp')} | "
        f"median/day £{m.get('median_daily_gbp')} | "
        f"days≥£1k: {m.get('days_ge_1000_gbp')}"
    )

    bars = snap.get("bar_analysis_latest") or {}
    print(f"\n=== Bar analysis ({bars.get('day')}) ===")
    print(
        f"  bars={bars.get('total_bars')} "
        f"S2 eligible={bars.get('s2_eligible')} would_trade={bars.get('s2_would_trade')} "
        f"S3 would_trade={bars.get('s3_would_trade')}"
    )
    print(f"  hint: {bars.get('s2_tune_hint')}")

    shadow = snap.get("shadow_summary") or {}
    print(f"\n=== Shadow strategies ({shadow.get('days_ok')} days) ===")
    for sid, row in (shadow.get("by_strategy") or {}).items():
        print(
            f"  {sid}: intents={row.get('intents')} would_trade={row.get('would_trade')}"
        )

    print("\nLearning focus:")
    for tip in snap.get("learning_focus") or []:
        print(f"  • {tip}")

    if args.write:
        path = write_learning_snapshot(days=days)
        print(f"\nWrote {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
