#!/usr/bin/env python3
"""
Periodic historical OHLC backfill scheduler (07:00–22:30 Europe/London).

Safe alongside the live trading agent: this process only reads
``ohlc_pull_status.json``, respects ``next_retry_time`` and the fetch window,
and invokes ``fetch_historical_ohlc.py`` in a separate subprocess. It does not
hold the agent instance lock or mutate runtime trading state.

Usage:
  PYTHONPATH=src python3 scripts/ohlc_fetch_scheduler.py          # poll every 5 min
  PYTHONPATH=src python3 scripts/ohlc_fetch_scheduler.py --once   # one check, exit

Cron / launchd (single shot every 5+ minutes):
  */5 * * * * cd /path/to/IG_Agent_v25 && PYTHONPATH=src \\
    python3 scripts/ohlc_fetch_scheduler.py --once >> logs/ohlc_fetch_scheduler.log 2>&1
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
from system.ohlc_fetch_window import is_fetch_window_open
from system.paths import data_dir

LONDON = ZoneInfo("Europe/London")
STATUS_PATH = data_dir() / "state" / "ohlc_pull_status.json"
DEFAULT_POLL_INTERVAL_SEC = 300  # 5 minutes
FETCH_SCRIPT = ROOT / "scripts" / "fetch_historical_ohlc.py"


def _now_london() -> datetime:
    return datetime.now(LONDON)


def _load_pull_status() -> dict:
    if not STATUS_PATH.is_file():
        return {}
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _next_retry_in_future(pull_status: dict, now: datetime) -> tuple[bool, str]:
    raw = pull_status.get("next_retry_time")
    if not raw:
        return False, ""
    try:
        retry_dt = datetime.fromisoformat(str(raw))
        now_cmp = now.astimezone(LONDON).replace(tzinfo=None) if now.tzinfo else now
        if retry_dt.tzinfo is not None:
            retry_dt = retry_dt.astimezone(LONDON).replace(tzinfo=None)
        if now_cmp < retry_dt:
            return True, str(raw)
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
    pull_status = _load_pull_status()
    now = _now_london()

    blocked, next_retry = _next_retry_in_future(pull_status, now)
    if blocked:
        log_engine(
            f"ohlc_fetch_scheduler: SKIP — next_retry_time in future ({next_retry})"
        )
        return 0

    if not is_fetch_window_open(now):
        log_engine(
            "ohlc_fetch_scheduler: SKIP — fetch window closed "
            "(allowed 07:00–22:30 Europe/London)"
        )
        return 0

    log_engine("ohlc_fetch_scheduler: RUN start — fetch_historical_ohlc.py")
    rc = _run_fetch()
    if rc == 0:
        log_engine("ohlc_fetch_scheduler: RUN complete — fetch_historical_ohlc.py")
    else:
        log_engine(
            f"ohlc_fetch_scheduler: RUN complete — fetch_historical_ohlc.py exited {rc}"
        )
    return rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="OHLC historical fetch scheduler (5 min poll or --once)"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one check then exit",
    )
    args = parser.parse_args(argv)

    if args.once:
        return tick()

    log_engine(
        f"ohlc_fetch_scheduler: loop started poll_interval={DEFAULT_POLL_INTERVAL_SEC}s"
    )
    while True:
        try:
            tick()
        except KeyboardInterrupt:
            log_engine("ohlc_fetch_scheduler: stopped")
            return 0
        except Exception as exc:
            log_engine(f"ohlc_fetch_scheduler: error {type(exc).__name__}: {exc}")
        time.sleep(DEFAULT_POLL_INTERVAL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
