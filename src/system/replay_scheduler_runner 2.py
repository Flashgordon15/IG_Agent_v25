"""Nightly replay pipeline — fetch OHLC, replay signals, analyse."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from system.engine_log import log_engine
from system.paths import data_dir, project_root
from system.replay_scheduler_state import load_replay_scheduler_state, save_replay_scheduler_state

LONDON = ZoneInfo("Europe/London")
CACHE_PATH = data_dir() / "ohlc_cache" / "nikkei_5m.jsonl"
RESULTS_PATH = data_dir() / "replay_results.jsonl"
ANALYSIS_PATH = data_dir() / "replay_analysis.txt"
HARD_STOP_HOUR = 22
HARD_STOP_MIN = 30
_CALIBRATION_RE = re.compile(r"signal_threshold:\s*(\d+)")


def _now_london() -> datetime:
    return datetime.now(LONDON)


def in_replay_quiet_window(now: datetime | None = None) -> bool:
    """22:30–07:00 London — manual/API runs blocked; scheduled 06:15 bypasses."""
    now = now or _now_london()
    minutes = now.hour * 60 + now.minute
    return minutes >= HARD_STOP_HOUR * 60 + HARD_STOP_MIN or minutes < 7 * 60


def in_replay_api_window(now: datetime | None = None) -> bool:
    """07:00–22:30 London — allowed for POST /api/replay/run."""
    return not in_replay_quiet_window(now)


def _calibration_factor_from_report(report: str) -> float:
    match = _CALIBRATION_RE.search(report)
    if not match:
        return 1.0
    return round(int(match.group(1)) / 100.0, 2)


def _merge_analysis_append(previous: str, new_report: str, run_ts: str) -> str:
    prev = previous.strip()
    new = new_report.strip()
    if not prev:
        return new + ("\n" if not new.endswith("\n") else "")
    if prev == new:
        return prev + ("\n" if not prev.endswith("\n") else "")
    sep = f"\n\n=== REPLAY RUN {run_ts} ===\n\n"
    merged = prev + sep + new
    return merged + ("\n" if not merged.endswith("\n") else "")


def _write_analysis(text: str) -> None:
    ANALYSIS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = ANALYSIS_PATH.with_suffix(".txt.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(ANALYSIS_PATH)


def _run_script(name: str) -> int:
    script = project_root() / "scripts" / name
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root() / "src") + (
        f":{env['PYTHONPATH']}" if env.get("PYTHONPATH") else ""
    )
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(project_root()),
        env=env,
    )
    return int(proc.returncode)


def _count_cache_bars() -> int:
    if not CACHE_PATH.is_file():
        return 0
    return sum(1 for _ in CACHE_PATH.read_text(encoding="utf-8").splitlines() if _.strip())


def _count_results_rows() -> int:
    if not RESULTS_PATH.is_file():
        return 0
    return sum(1 for _ in RESULTS_PATH.read_text(encoding="utf-8").splitlines() if _.strip())


def _set_run_status(status: str, *, error: str | None = None) -> None:
    state = load_replay_scheduler_state()
    state["status"] = status
    if error:
        state["last_error"] = error
    elif status == "idle":
        state.pop("last_error", None)
    save_replay_scheduler_state(state)


def run_replay_pipeline(*, scheduled: bool = False) -> int:
    """
    Run fetch → replay_signals → analyse_replay.

    scheduled=True: daily 06:15 job (may run during pre-07:00 quiet window).
    scheduled=False: manual/API — blocked 22:30–07:00 London.
    """
    now = _now_london()
    if not scheduled and in_replay_quiet_window(now):
        log_engine("replay_scheduler: SKIP — quiet window 22:30–07:00 BST")
        return 0
    if not scheduled and now.hour == HARD_STOP_HOUR and now.minute >= HARD_STOP_MIN:
        log_engine("replay_scheduler: SKIP — hard cutoff 22:30 BST")
        return 0

    _set_run_status("running")
    bars_before = _count_cache_bars()

    try:
        rc = _run_script("fetch_historical_ohlc.py")
        if rc != 0:
            log_engine(f"replay_scheduler: fetch exited {rc}")
            _set_run_status("failed", error=f"fetch exited {rc}")
            return rc

        bars_after_fetch = _count_cache_bars()
        new_fetch = max(0, bars_after_fetch - bars_before)

        rc = _run_script("replay_signals.py")
        if rc != 0:
            _set_run_status("failed", error=f"replay_signals exited {rc}")
            return rc

        if not scheduled and in_replay_quiet_window(_now_london()):
            log_engine("replay_scheduler: stopped before analysis — quiet window")
            _set_run_status("idle")
            return 0

        previous_analysis = (
            ANALYSIS_PATH.read_text(encoding="utf-8") if ANALYSIS_PATH.is_file() else ""
        )
        rc = _run_script("analyse_replay.py")
        if rc != 0:
            _set_run_status("failed", error=f"analyse_replay exited {rc}")
            return rc

        new_report = (
            ANALYSIS_PATH.read_text(encoding="utf-8") if ANALYSIS_PATH.is_file() else ""
        )
        merged = _merge_analysis_append(previous_analysis, new_report, now.isoformat())
        _write_analysis(merged)

        save_replay_scheduler_state(
            {
                "last_run_time": now.isoformat(),
                "bars_processed": bars_after_fetch,
                "bars_cache": bars_after_fetch,
                "results_rows": _count_results_rows(),
                "calibration_factor": _calibration_factor_from_report(new_report),
                "status": "idle",
            }
        )
        log_engine(f"Nightly replay complete: {new_fetch} new bars")
        return 0
    except Exception as exc:
        log_engine(f"replay_scheduler: failed {type(exc).__name__}: {exc}")
        _set_run_status("failed", error=str(exc))
        return 1
