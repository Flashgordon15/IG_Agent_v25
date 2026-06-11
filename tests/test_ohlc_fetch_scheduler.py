from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "ohlc_fetch_scheduler.py"
SPEC = importlib.util.spec_from_file_location("ohlc_fetch_scheduler", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
sched = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sched)

LONDON = ZoneInfo("Europe/London")


def _london(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=LONDON)


def test_once_skips_when_fetch_window_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sched, "is_fetch_window_open", lambda now=None: False)
    monkeypatch.setattr(sched, "_load_pull_status", lambda: {})
    run_fetch = MagicMock(return_value=0)
    monkeypatch.setattr(sched, "_run_fetch", run_fetch)

    assert sched.tick() == 0
    run_fetch.assert_not_called()


def test_once_skips_when_next_retry_in_future(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    status_path = tmp_path / "state" / "ohlc_pull_status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps({"next_retry_time": "2099-01-01T12:00:00"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(sched, "STATUS_PATH", status_path)
    monkeypatch.setattr(sched, "is_fetch_window_open", lambda now=None: True)
    run_fetch = MagicMock(return_value=0)
    monkeypatch.setattr(sched, "_run_fetch", run_fetch)
    monkeypatch.setattr(
        sched,
        "_now_london",
        lambda: _london(2026, 5, 28, 12, 0),
    )

    assert sched.tick() == 0
    run_fetch.assert_not_called()


def test_once_runs_fetch_when_window_open_and_retry_passed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    status_path = tmp_path / "state" / "ohlc_pull_status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps({"next_retry_time": "2020-01-01T00:00:00"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(sched, "STATUS_PATH", status_path)
    monkeypatch.setattr(sched, "is_fetch_window_open", lambda now=None: True)
    run_fetch = MagicMock(return_value=0)
    monkeypatch.setattr(sched, "_run_fetch", run_fetch)
    monkeypatch.setattr(
        sched,
        "_now_london",
        lambda: _london(2026, 5, 28, 12, 0),
    )

    assert sched.tick() == 0
    run_fetch.assert_called_once()


def test_pull_status_round_trip_via_scheduler_loader(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    status_path = tmp_path / "state" / "ohlc_pull_status.json"
    payload = {
        "status": "ok",
        "run_status": "complete",
        "next_retry_time": None,
        "block_reason": None,
    }
    status_path.parent.mkdir(parents=True)
    status_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    monkeypatch.setattr(sched, "STATUS_PATH", status_path)

    loaded = sched._load_pull_status()
    assert loaded == payload

    payload["next_retry_time"] = "2026-05-28T15:00:00"
    status_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    assert sched._load_pull_status()["next_retry_time"] == "2026-05-28T15:00:00"


def test_main_once_delegates_to_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    tick = MagicMock(return_value=0)
    monkeypatch.setattr(sched, "tick", tick)
    assert sched.main(["--once"]) == 0
    tick.assert_called_once()
