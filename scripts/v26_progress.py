#!/usr/bin/env python3
"""v26 daily progress — learning on no-trade days (gates, shadow, L1 replay)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "v26"))

from ingest.lake_reader import summarize_day, utc_today  # noqa: E402
from research.gate_blockers import (  # noqa: E402
    build_gate_blocker_report,
    report_to_dict,
)
from research.l1_replay import replay_day_signals  # noqa: E402
from research.shadow_expectancy import (  # noqa: E402
    analyze_near_miss,
    near_miss_to_dict,
)
from shadow.runner import shadow_dir  # noqa: E402


def _shadow_counts(day: str) -> dict[str, int]:
    path = shadow_dir() / f"{day}.jsonl"
    if not path.is_file():
        return {}
    from collections import Counter

    intents: Counter[str] = Counter()
    would: Counter[str] = Counter()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("event_type") != "shadow_intent":
            continue
        sid = str(row.get("strategy_id") or "?")
        intents[sid] += 1
        if (row.get("payload") or {}).get("would_trade"):
            would[sid] += 1
    return {f"{sid}_intents": n for sid, n in intents.items()} | {
        f"{sid}_would_trade": would[sid] for sid in would
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="v26 progress report for quiet trading days"
    )
    parser.add_argument("--day", default="", help="UTC day YYYY-MM-DD (default today)")
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write data_lake/state/v26_daily_progress.json",
    )
    args = parser.parse_args()

    day = args.day.strip() or utc_today()
    lake = summarize_day(day)
    gates = report_to_dict(build_gate_blocker_report(day=day))
    replay = replay_day_signals(day)
    shadow = _shadow_counts(day)
    near_miss = near_miss_to_dict(analyze_near_miss(day=day))

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "day": day,
        "lake": {
            "total_events": lake.total_events,
            "signal_evals": lake.signal_evals,
            "would_fire": lake.would_fire,
            "order_intents": lake.order_intents,
            "fill_closes": lake.fill_closes,
        },
        "gate_blockers": gates,
        "l1_replay": replay,
        "shadow": shadow,
        "shadow_expectancy": near_miss,
    }

    print(f"\n=== v26 progress — {day} ===")
    print(
        f"Feeder: {lake.total_events} events | evals {lake.signal_evals} | would_fire {lake.would_fire}"
    )
    print(f"Live:   intents {lake.order_intents} | fills {lake.fill_closes}")

    if gates.get("confidence_buckets"):
        print("\nConfidence distribution (adjusted %):")
        for bucket, n in gates["confidence_buckets"].items():
            print(f"  {bucket:8} {n:6}")

    nm = gates.get("near_miss") or {}
    if nm:
        print(
            f"\nNear-miss: 70-74%={nm.get('70_74_pct', 0)}  75-79%={nm.get('75_79_pct', 0)}"
        )

    if gates.get("failed_gates"):
        print("\nTop gate blockers (fail count):")
        for gate, n in list(gates["failed_gates"].items())[:8]:
            print(f"  {gate:24} {n:6}")

    if replay.get("ok"):
        print(
            f"\nL1 replay: median conf {replay.get('median_confidence')}% | mean {replay.get('mean_confidence')}%"
        )
        print("  Hypothetical fires by threshold:")
        for k, v in (replay.get("by_threshold") or {}).items():
            print(f"    {k}: {v}")

    if near_miss.get("near_miss_evals"):
        print(
            f"\nNear-miss counterfactual: {near_miss.get('near_miss_evals')} evals (70-79%) | "
            f"shadow match {near_miss.get('shadow_would_trade_same_epic')} | "
            f"est E£ proxy {near_miss.get('estimated_counterfactual_e_gbp')}"
        )

    if shadow:
        print("\nShadow strategies:")
        strategies = sorted(
            {k.rsplit("_", 1)[0] for k in shadow if k.endswith("_intents")}
        )
        for sid in strategies:
            print(
                f"  {sid:16} intents={shadow.get(f'{sid}_intents', 0):6}  "
                f"would_trade={shadow.get(f'{sid}_would_trade', 0):4}"
            )

    if args.write:
        out = ROOT / "data_lake" / "state" / "v26_daily_progress.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nWrote {out}")

    print()
    return 0 if lake.total_events > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
