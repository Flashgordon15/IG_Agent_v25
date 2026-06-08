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
from ingest.lake_reader import (
    event_utc_day,
    events_dir,
    iter_events,
    summarize_day,
    utc_today,
)
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


def _shadow_tail_loop() -> None:
    from ingest.tail import tail_events
    from shadow.runner import process_event

    print("v26 S1+S2+S3 shadow tail — following UTC day (feeder → shadow_v26/)")
    for event in tail_events(follow_utc_rollover=True):
        process_event(event, day=event_utc_day(event))


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
        from research.l4_forward import format_forward_status, write_forward_cert

        path = write_forward_cert()
        print("v26 demo forward certification (v25 executes — this tracks P&L only)")
        print(f"  snapshot: {path}")
        print(f"  {format_forward_status()}")
        if args.watch:
            print("  watch: refresh every 5m (Ctrl+C to stop)")
            try:
                while True:
                    time.sleep(300)
                    write_forward_cert()
                    print("---")
                    print(format_forward_status())
            except KeyboardInterrupt:
                return 0
        return 0
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
        t = threading.Thread(target=_shadow_tail_loop, daemon=True)
        t.start()
        print("v26 watch+tail — UTC day rolls automatically at midnight")
        try:
            while True:
                print("---")
                _print_summary(utc_today())
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
