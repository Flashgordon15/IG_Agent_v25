#!/usr/bin/env python3
"""
Periodic historical OHLC backfill — only 07:00–22:30 Europe/London.

Manual (foreground loop, default 15 min between attempts):
  PYTHONPATH=src python3 scripts/ohlc_fetch_scheduler.py

Single shot (cron / launchd — still enforces window + retry):
  PYTHONPATH=src python3 scripts/ohlc_fetch_scheduler.py --once

Background:
  nohup env PYTHONPATH=src python3 scripts/ohlc_fetch_scheduler.py \\
    >> logs/ohlc_fetch_scheduler.log 2>&1 &

Cron example (every 15 minutes; skips outside window automatically):
  */15 * * * * cd /path/to/IG_Agent_v25 && PYTHONPATH=src \\
    /usr/bin/python3 scripts/ohlc_fetch_scheduler.py --once \\
    >> logs/ohlc_fetch_scheduler.log 2>&1

Launchd example (ProgramArguments = python3 + script path, --once; StartInterval 900).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.engine_log import log_engine
from system.ohlc_fetch_window import is_fetch_window_allowed
from system.paths import data_dir

LONDON = ZoneInfo("Europe/London")
STATUS_PATH = data_dir() / "state" / "ohlc_pull_status.json"
DEFAULT_INTERVAL_SEC = 900
FETCH_SCRIPT = ROOT / "scripts" / "fetch_historical_ohlc.py"


def _now_london() -> datetime:
    return datetime.now(LONDON)


def _iso_london(dt: datetime | None = None) -> str:
    dt = dt or _now_london()
    if dt.tzinfo is not None:
        dt = dt.astimezone(LONDON).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _load_pull_status() -> dict:
    if not STATUS_PATH.is_file():
        return {}
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _pull_retry_blocked(pull_status: dict, now_iso: str) -> tuple[bool, str]:
    blocked_until = str(pull_status.get("next_retry_time") or "")
    block_reason = str(pull_status.get("block_reason") or "")
    if not blocked_until or not block_reason:
        return False, ""
    try:
        if datetime.fromisoformat(now_iso) < datetime.fromisoformat(blocked_until):
            return True, (
                f"block_reason={block_reason} next_retry={blocked_until}"
            )
    except ValueError:
        pass
    return False, ""


def _run_fetch() -> int:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + (
        f":{env['PYTHONPATH']}" if env.get("PYTHONPATH") else ""
    )
    proc = subprocess.run(
        [sys.executable, str(FETCH_SCRIPT)],
        cwd=str(ROOT),
        env=env,
    )
    return int(proc.returncode)


def tick() -> int:
    now = _now_london()
    now_iso = _iso_london(now)

    if not is_fetch_window_allowed(now):
        log_engine(
            "ohlc_fetch_scheduler: SKIP — outside fetch window "
            "(allowed 07:00–22:30 Europe/London; quiet 22:30–07:00)"
        )
        return 0

    pull_status = _load_pull_status()
    blocked, detail = _pull_retry_blocked(pull_status, now_iso)
    if blocked:
        log_engine(f"ohlc_fetch_scheduler: SKIP — pull retry not due ({detail})")
        return 0

    log_engine("ohlc_fetch_scheduler: invoking fetch_historical_ohlc.py")
    rc = _run_fetch()
    if rc != 0:
        log_engine(f"ohlc_fetch_scheduler: fetch exited {rc}")
    return rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OHLC historical fetch scheduler")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one tick and exit (for cron/launchd)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SEC,
        metavar="SEC",
        help=f"Seconds between ticks in loop mode (default {DEFAULT_INTERVAL_SEC})",
    )
    args = parser.parse_args(argv)

    if args.once:
        return tick()

    interval = max(60, int(args.interval))
    log_engine(
        f"ohlc_fetch_scheduler: loop started interval={interval}s "
        "(window 07:00–22:30 Europe/London)"
    )
    while True:
        try:
            tick()
        except KeyboardInterrupt:
            log_engine("ohlc_fetch_scheduler: stopped")
            return 0
        except Exception as exc:
            log_engine(f"ohlc_fetch_scheduler: error {type(exc).__name__}: {exc}")
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
