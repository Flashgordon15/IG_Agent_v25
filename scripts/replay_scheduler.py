#!/usr/bin/env python3
"""
Nightly replay pipeline — 06:15 Europe/London, never during 22:30–07:00.

  PYTHONPATH=src python3 scripts/replay_scheduler.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.engine_log import log_engine
from system.paths import data_dir

LONDON = ZoneInfo("Europe/London")
CACHE_PATH = data_dir() / "ohlc_cache" / "nikkei_5m.jsonl"
RESULTS_PATH = data_dir() / "replay_results.jsonl"
STATE_PATH = data_dir() / "replay_scheduler_state.json"
HARD_STOP_HOUR = 22
HARD_STOP_MIN = 30


def _now_london() -> datetime:
    return datetime.now(LONDON)


def _in_quiet_window(now: datetime | None = None) -> bool:
    now = now or _now_london()
    minutes = now.hour * 60 + now.minute
    return minutes >= HARD_STOP_HOUR * 60 + HARD_STOP_MIN or minutes < 7 * 60


def _load_state() -> dict:
    if not STATE_PATH.is_file():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _run_script(name: str) -> int:
    script = ROOT / "scripts" / name
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + (
        f":{env['PYTHONPATH']}" if env.get("PYTHONPATH") else ""
    )
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(ROOT),
        env=env,
    )
    return int(proc.returncode)


def main() -> int:
    now = _now_london()
    if _in_quiet_window(now):
        log_engine("replay_scheduler: SKIP — quiet window 22:30–07:00 BST")
        return 0
    if now.hour == HARD_STOP_HOUR and now.minute >= HARD_STOP_MIN:
        log_engine("replay_scheduler: SKIP — hard cutoff 22:30 BST")
        return 0

    state = _load_state()
    bars_before = 0
    if CACHE_PATH.is_file():
        bars_before = sum(1 for _ in CACHE_PATH.read_text(encoding="utf-8").splitlines() if _.strip())

    rc = _run_script("fetch_historical_ohlc.py")
    if rc not in (0,):
        log_engine(f"replay_scheduler: fetch exited {rc}")
        return rc

    bars_after_fetch = sum(1 for _ in CACHE_PATH.read_text(encoding="utf-8").splitlines() if _.strip()) if CACHE_PATH.is_file() else 0
    new_fetch = max(0, bars_after_fetch - bars_before)

    rc = _run_script("replay_signals.py")
    if rc != 0:
        return rc

    if _in_quiet_window(_now_london()):
        log_engine("replay_scheduler: stopped before analysis — quiet window")
        return 0

    rc = _run_script("analyse_replay.py")
    if rc != 0:
        return rc

    results_after = 0
    if RESULTS_PATH.is_file():
        results_after = sum(1 for _ in RESULTS_PATH.read_text(encoding="utf-8").splitlines() if _.strip())

    state["last_run"] = now.isoformat()
    state["bars_cache"] = bars_after_fetch
    state["results_rows"] = results_after
    _save_state(state)
    log_engine(f"Nightly replay complete: {new_fetch} new bars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
