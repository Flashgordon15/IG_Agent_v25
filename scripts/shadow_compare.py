#!/usr/bin/env python3
"""
Compare v25 feeder outcomes vs v26 shadow intents for a day.

  PYTHONPATH=src:v26 python3 scripts/shadow_compare.py
  PYTHONPATH=src:v26 python3 scripts/shadow_compare.py --day 2026-06-08 --process
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "v26"))

from collections import defaultdict

from expectancy.engine import (
    collect_fills,
    compute_setup_stats,
    portfolio_summary,
    write_snapshot,
)
from expectancy.shadow_attribution import (
    attribute_fills,
    load_fill_closes,
    load_shadow_would_trades,
    summarize_strategy_pnl,
    write_strategy_pnl_snapshot,
)
from ingest.lake_reader import iter_events, summarize_day
from shadow.runner import process_day_events, shadow_dir


def _strategy_breakdown(shadows: list[dict]) -> dict[str, dict[str, int]]:
    by: dict[str, dict[str, int]] = defaultdict(
        lambda: {"intents": 0, "would_trade": 0}
    )
    for s in shadows:
        sid = str(s.get("strategy_id") or "unknown")
        payload = s.get("payload") or {}
        by[sid]["intents"] += 1
        if payload.get("would_trade"):
            by[sid]["would_trade"] += 1
    return dict(by)


def _load_shadow_intents(day: str) -> list[dict]:
    path = shadow_dir() / f"{day}.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="v25 vs v26 shadow compare")
    parser.add_argument("--day", default="", help="UTC day YYYY-MM-DD")
    parser.add_argument(
        "--process",
        action="store_true",
        help="Run v26 shadow processor on feeder events before compare",
    )
    parser.add_argument(
        "--expectancy", action="store_true", help="Write expectancy_snapshot.json"
    )
    parser.add_argument(
        "--days", type=int, default=14, help="Rolling days for expectancy"
    )
    args = parser.parse_args()

    from datetime import datetime, timezone

    day = args.day.strip() or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if args.process:
        events = list(iter_events(day=day))
        n = process_day_events(events, day=day, clear_seen=True)
        print(f"Processed {n} v26 shadow intents from {len(events)} feeder events")

    v25 = summarize_day(day)
    shadows = _load_shadow_intents(day)
    shadow_trades = sum(
        1 for s in shadows if (s.get("payload") or {}).get("would_trade")
    )

    print(f"\n=== Shadow compare — {day} ===")
    print("v25 feeder:")
    print(f"  signal_eval would_fire: {v25.would_fire}")
    print(f"  order_intents:         {v25.order_intents}")
    print(f"  fill_closes:           {v25.fill_closes}")
    print(f"  fill_pnl_gbp:          {v25.fill_pnl_gbp:+.2f}")
    print("v26 shadow (all strategies):")
    print(f"  shadow_intents:        {len(shadows)}")
    print(f"  would_trade:           {shadow_trades}")

    by_strat = _strategy_breakdown(shadows)
    if by_strat:
        print("\n  by strategy:")
        for sid in sorted(by_strat):
            row = by_strat[sid]
            print(
                f"    {sid:20} intents={row['intents']:6}  would_trade={row['would_trade']:5}"
            )

    if v25.would_fire > 0:
        parity = shadow_trades / v25.would_fire * 100.0
        print(f"  parity vs would_fire:  {parity:.1f}%")
    if v25.order_intents > 0:
        match = min(v25.order_intents, shadow_trades) / v25.order_intents * 100.0
        print(f"  vs order_intents:      {match:.1f}% (capped)")

    fills = collect_fills(days=args.days)
    pf = portfolio_summary(fills)
    print(f"\nRolling {args.days}d portfolio (fill_close):")
    print(
        f"  trades: {pf['n']}  WR: {pf['wr']:.1%}  E£: {pf['e_gbp']:+.2f}  total: {pf['total_pnl_gbp']:+.2f}"
    )

    setups = compute_setup_stats(fills)
    if setups:
        print("\nTop setups by P&L:")
        for s in setups[:5]:
            print(
                f"  {s.setup_key[:48]:48} n={s.n:3} E£={s.e_gbp:+6.2f} "
                f"WR={s.wr:.0%} [{s.status}]"
            )

    shadows_trade = load_shadow_would_trades(days=args.days)
    fills_all = load_fill_closes(days=args.days)
    attributed = attribute_fills(fills_all, shadows_trade)
    if fills_all:
        print(
            f"\nShadow P&L attribution ({args.days}d, {len(attributed)}/{len(fills_all)} fills matched):"
        )
        strat_pnl = summarize_strategy_pnl(attributed)
        if strat_pnl:
            for sid in sorted(strat_pnl):
                row = strat_pnl[sid]
                print(
                    f"  {sid:20} n={int(row['n']):3}  WR={row['wr']:.0%}  "
                    f"E£={row['e_gbp']:+.2f}  total={row['total_pnl_gbp']:+.2f}"
                )
        else:
            print("  (no fills matched to shadow would_trade intents)")

    if args.expectancy:
        path = write_snapshot(days=args.days)
        print(f"\nWrote {path}")
        from system.setup_registry import registry_path

        rp = registry_path()
        if rp.is_file():
            print(f"Wrote {rp}")
        sp_path = write_strategy_pnl_snapshot(days=args.days)
        print(f"Wrote {sp_path}")

    return 0 if v25.total_events > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
