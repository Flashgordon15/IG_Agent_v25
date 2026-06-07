#!/usr/bin/env python3
"""
IG Agent v26 — shadow / research entry (no IG orders in shadow mode).

  PYTHONPATH=src:v26 python3 v26/main.py --mode shadow
  PYTHONPATH=src:v26 python3 v26/main.py --mode shadow --watch
  PYTHONPATH=src:v26 python3 v26/main.py --mode shadow --process-day
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from datetime import datetime, timezone

from expectancy.engine import write_snapshot
from ingest.lake_reader import events_dir, iter_events, summarize_day
from shadow.runner import process_day_events


def _print_summary(day: str) -> int:
    s = summarize_day(day)
    print(f"v26 shadow ingest — {s.day}")
    print(f"  events file: {events_dir() / (s.day + '.jsonl')}")
    print(f"  total events: {s.total_events}")
    if s.total_events == 0:
        print("  (no events — is v25 running with IG_AGENT_FEEDER=1?)")
        return 1
    print(f"  epics: {', '.join(sorted(s.epics)) or '—'}")
    print(f"  signal_evals: {s.signal_evals}  would_fire: {s.would_fire}")
    print(f"  order_intents: {s.order_intents}")
    print(f"  fill_closes: {s.fill_closes}  fill_pnl_gbp: {s.fill_pnl_gbp:+.2f}")
    if s.by_type:
        print("  by_type:")
        for k, v in s.by_type.most_common():
            print(f"    {k}: {v}")
    return 0


def _shadow_tail_loop(day: str) -> None:
    from ingest.tail import tail_events
    from shadow.runner import process_event

    print(f"v26 S1 shadow tail — {day} (feeder → shadow_v26/)")
    for event in tail_events(day):
        process_event(event, day=day)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="IG Agent v26")
    parser.add_argument(
        "--mode",
        choices=("shadow", "trade", "research"),
        default="shadow",
        help="shadow=ingest + optional S1 shadow; trade not wired",
    )
    parser.add_argument("--day", default="", help="UTC day YYYY-MM-DD (default today)")
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Summary every 30s; with --tail also process new events",
    )
    parser.add_argument(
        "--tail",
        action="store_true",
        help="Tail feeder lake and write S1 shadow intents (use with --watch)",
    )
    parser.add_argument(
        "--process-day",
        action="store_true",
        help="One-shot: process all feeder events for day → shadow_v26",
    )
    parser.add_argument(
        "--expectancy",
        action="store_true",
        help="Write data_lake/state/expectancy_snapshot.json",
    )
    args = parser.parse_args(argv)

    if args.mode == "trade":
        print("v26 --mode trade not wired yet; use v25 feeder until L5 certification.")
        return 2
    if args.mode == "research":
        print("Run: python3 scripts/build_feature_store.py --days 7")
        return 0

    day = args.day.strip() or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if args.process_day:
        events = list(iter_events(day=day))
        n = process_day_events(events, day=day, clear_seen=True)
        print(f"Processed {n} shadow intents from {len(events)} events ({day})")
        _print_summary(day)
        if args.expectancy:
            p = write_snapshot()
            print(f"Expectancy snapshot: {p}")
        return 0

    if args.watch and args.tail:
        t = threading.Thread(target=_shadow_tail_loop, args=(day,), daemon=True)
        t.start()
        print(f"v26 watch+tail — day={day}")
        try:
            while True:
                print("---")
                _print_summary(day)
                time.sleep(30)
        except KeyboardInterrupt:
            return 0

    if args.watch:
        print(f"v26 shadow watch — day={day} (add --tail for live S1 processing)")
        try:
            while True:
                print("---")
                _print_summary(day)
                time.sleep(30)
        except KeyboardInterrupt:
            return 0

    rc = _print_summary(day)
    if args.expectancy:
        p = write_snapshot()
        print(f"Expectancy snapshot: {p}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
