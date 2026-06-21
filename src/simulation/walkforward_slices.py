"""Walk-forward 80/20 day slicing for production tick archives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from simulation.historical_replayer import ReplayTick, load_ticks


@dataclass(frozen=True)
class WalkForwardSlices:
    in_sample: list[ReplayTick]
    out_of_sample: list[ReplayTick]
    in_sample_days: tuple[str, ...]
    out_of_sample_days: tuple[str, ...]
    split_ratio: float = 0.8


def _day_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def partition_by_day(ticks: list[ReplayTick]) -> dict[str, list[ReplayTick]]:
    buckets: dict[str, list[ReplayTick]] = {}
    for tick in ticks:
        buckets.setdefault(_day_key(tick.timestamp), []).append(tick)
    for day in buckets:
        buckets[day].sort(key=lambda t: t.timestamp)
    return dict(sorted(buckets.items()))


def walkforward_80_20(
    path: Path,
    *,
    train_day_count: int | None = None,
) -> WalkForwardSlices:
    """
    Partition archive into Days 1–4 (in-sample) and Day 5 (out-of-sample).

    Uses calendar-day buckets; with a 5-day archive this is exactly 4+1 days.
    """
    ticks = load_ticks(path)
    by_day = partition_by_day(ticks)
    days = list(by_day.keys())
    if len(days) < 2:
        split = max(1, int(len(days) * 0.8))
        is_days = days[:split]
        oos_days = days[split:]
    elif train_day_count is not None:
        split = min(train_day_count, len(days) - 1)
        is_days = days[:split]
        oos_days = days[split:]
    else:
        split = max(1, len(days) - 1)
        is_days = days[:split]
        oos_days = days[split:]

    in_sample: list[ReplayTick] = []
    out_of_sample: list[ReplayTick] = []
    for d in is_days:
        in_sample.extend(by_day[d])
    for d in oos_days:
        out_of_sample.extend(by_day[d])
    in_sample.sort(key=lambda t: t.timestamp)
    out_of_sample.sort(key=lambda t: t.timestamp)
    return WalkForwardSlices(
        in_sample=in_sample,
        out_of_sample=out_of_sample,
        in_sample_days=tuple(is_days),
        out_of_sample_days=tuple(oos_days),
    )


def write_slice_jsonl(ticks: list[ReplayTick], path: Path) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for tick in ticks:
            row = {
                "type": "tick",
                "epic": tick.epic,
                "bid": tick.bid,
                "offer": tick.offer,
                "timestamp": datetime.fromtimestamp(
                    tick.timestamp, tz=timezone.utc
                ).isoformat(),
            }
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")


def slice_summary(slices: WalkForwardSlices) -> dict[str, Any]:
    return {
        "in_sample_days": list(slices.in_sample_days),
        "out_of_sample_days": list(slices.out_of_sample_days),
        "in_sample_ticks": len(slices.in_sample),
        "out_of_sample_ticks": len(slices.out_of_sample),
        "split_ratio": slices.split_ratio,
    }
