"""
Generate a robust multi-day historical tick archive for HARDENED_TESTBED replay.

Produces merge-sorted JSONL ticks across EUR/USD, Wall St, and Gold with regime
transitions (trend, chop, volatility spikes) suitable for strategy evaluation.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

EPICS = (
    ("CS.D.EURUSD.CFD.IP", 1.08500, 0.00010, 5),
    ("IX.D.DOW.IFM.IP", 38500.0, 1.0, 1),
    ("CS.D.CFPGOLD.CFP.IP", 2350.0, 0.20, 2),
)

REGIMES = ("trend_up", "trend_down", "chop", "vol_spike")


def _session_vol_scale(ts: datetime) -> float:
    hour = ts.hour
    if 13 <= hour < 17:
        return 1.4
    if 8 <= hour < 13:
        return 1.2
    if 17 <= hour < 21:
        return 1.1
    return 0.7


def _next_mid(
    mid: float,
    regime: str,
    *,
    vol: float,
    decimals: int,
    rng: random.Random,
) -> float:
    shock = rng.gauss(0, vol)
    if regime == "trend_up":
        shock += vol * 0.35
    elif regime == "trend_down":
        shock -= vol * 0.35
    elif regime == "chop":
        shock = rng.gauss(0, vol * 1.6)
    elif regime == "vol_spike":
        shock = rng.gauss(0, vol * 3.5)
    return round(mid + shock, decimals)


def generate_ticks(
    *,
    days: float = 5.0,
    interval_sec: float = 30.0,
    start: datetime | None = None,
    seed: int = 42,
) -> list[dict]:
    rng = random.Random(seed)
    t0 = start or datetime(2026, 6, 10, 0, 0, tzinfo=timezone.utc)
    end = t0 + timedelta(days=days)
    states = {
        epic: {"mid": base, "half_spread": spread, "decimals": dec}
        for epic, base, spread, dec in EPICS
    }
    regime = "trend_up"
    regime_ticks_left = int(timedelta(hours=6).total_seconds() / interval_sec)
    rows: list[dict] = []
    ts = t0
    while ts < end:
        if regime_ticks_left <= 0:
            regime = rng.choice(REGIMES)
            regime_ticks_left = int(
                rng.uniform(2, 8) * 3600 / interval_sec
            )
        scale = _session_vol_scale(ts)
        for epic, base, half_spread, decimals in EPICS:
            st = states[epic]
            vol = half_spread * 2.5 * scale
            if epic == "CS.D.EURUSD.CFD.IP":
                vol = max(0.00005, vol)
            elif epic == "IX.D.DOW.IFM.IP":
                vol = max(1.5, vol)
            else:
                vol = max(0.08, vol)
            st["mid"] = _next_mid(
                float(st["mid"]),
                regime,
                vol=vol,
                decimals=decimals,
                rng=rng,
            )
            bid = round(st["mid"] - half_spread, decimals)
            offer = round(st["mid"] + half_spread, decimals)
            rows.append(
                {
                    "type": "tick",
                    "epic": epic,
                    "bid": bid,
                    "offer": offer,
                    "timestamp": ts.isoformat(),
                }
            )
        regime_ticks_left -= 1
        ts += timedelta(seconds=interval_sec)
    rows.sort(key=lambda r: r["timestamp"])
    return rows


def write_archive(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate production replay archive")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("src/simulation/data/production_5day_archive.jsonl"),
    )
    parser.add_argument("--days", type=float, default=5.0)
    parser.add_argument("--interval-sec", type=float, default=30.0)
    parser.add_argument("--min-ticks", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    rows = generate_ticks(
        days=args.days,
        interval_sec=args.interval_sec,
        seed=args.seed,
    )
    if len(rows) < args.min_ticks:
        extra_days = args.days * (args.min_ticks / max(len(rows), 1))
        rows = generate_ticks(
            days=extra_days,
            interval_sec=args.interval_sec,
            seed=args.seed,
        )
    write_archive(args.output, rows)
    print(
        f"Wrote {len(rows)} ticks spanning {args.days}d → {args.output} "
        f"({args.output.stat().st_size // 1024} KB)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
