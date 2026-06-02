from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from system import replay_scheduler_runner as replay
from system.replay_scheduler_state import STATE_PATH, load_replay_scheduler_state

LONDON = ZoneInfo("Europe/London")
SAMPLE_REPORT = (
    "=== JAPAN 225 SIGNAL REPLAY ANALYSIS ===\n"
    "RECOMMENDED CONFIG:\n"
    "  signal_threshold: 75  (best profit factor)\n"
)


def _london(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=LONDON)


def _patch_data_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_path = tmp_path / "replay_scheduler_state.json"
    from system import replay_scheduler_state as state_mod

    monkeypatch.setattr(state_mod, "STATE_PATH", state_path)
    monkeypatch.setattr(state_mod, "_LEGACY_STATE_PATH", tmp_path / "replay_state.json")
    monkeypatch.setattr(replay, "ANALYSIS_PATH", tmp_path / "replay_analysis.txt")
    monkeypatch.setattr(replay, "CACHE_PATH", tmp_path / "nikkei_5m.jsonl")
    monkeypatch.setattr(replay, "RESULTS_PATH", tmp_path / "replay_results.jsonl")


def _mock_successful_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    analysis_before: str = "",
    analysis_after: str = SAMPLE_REPORT,
    bar_lines: int = 3,
) -> None:
    cache = replay.CACHE_PATH
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("\n".join('{"t":"x"}' for _ in range(bar_lines)) + "\n")

    def fake_run(name: str) -> int:
        if name == "analyse_replay.py":
            replay.ANALYSIS_PATH.parent.mkdir(parents=True, exist_ok=True)
            replay.ANALYSIS_PATH.write_text(analysis_after, encoding="utf-8")
        return 0

    monkeypatch.setattr(replay, "_run_script", fake_run)
    if analysis_before:
        replay.ANALYSIS_PATH.parent.mkdir(parents=True, exist_ok=True)
        replay.ANALYSIS_PATH.write_text(analysis_before, encoding="utf-8")


def test_analysis_appends_across_two_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_data_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(
        replay,
        "_now_london",
        lambda: _london(2026, 5, 28, 12, 0),
    )
    run1 = "=== RUN ONE ===\n"
    run2 = "=== RUN TWO ===\n"
    _mock_successful_pipeline(monkeypatch, analysis_before="", analysis_after=run1)
    assert replay.run_replay_pipeline(scheduled=False) == 0
    after_first = replay.ANALYSIS_PATH.read_text(encoding="utf-8")
    assert run1 in after_first

    _mock_successful_pipeline(
        monkeypatch, analysis_before=after_first, analysis_after=run2
    )
    assert replay.run_replay_pipeline(scheduled=False) == 0
    merged = replay.ANALYSIS_PATH.read_text(encoding="utf-8")
    assert run1 in merged
    assert run2 in merged
    assert "=== REPLAY RUN" in merged


def test_updates_replay_scheduler_state_after_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_data_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(
        replay,
        "_now_london",
        lambda: _london(2026, 5, 28, 10, 15),
    )
    _mock_successful_pipeline(monkeypatch, bar_lines=5)

    assert replay.run_replay_pipeline(scheduled=False) == 0
    assert STATE_PATH.is_file()
    state = load_replay_scheduler_state()
    assert state["last_run_time"] == _london(2026, 5, 28, 10, 15).isoformat()
    assert state["bars_processed"] == 5
    assert state["calibration_factor"] == 0.75
    assert state["status"] == "idle"


def test_exits_cleanly_at_or_after_2230(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_data_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(
        replay,
        "_now_london",
        lambda: _london(2026, 5, 28, 22, 30),
    )
    run_script = MagicMock(return_value=0)
    monkeypatch.setattr(replay, "_run_script", run_script)

    assert replay.run_replay_pipeline(scheduled=False) == 0
    run_script.assert_not_called()


def test_scheduled_run_allowed_at_0615(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_data_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(
        replay,
        "_now_london",
        lambda: _london(2026, 5, 28, 6, 15),
    )
    run_script = MagicMock(return_value=0)

    def fake_run(name: str) -> int:
        if name == "analyse_replay.py":
            replay.ANALYSIS_PATH.parent.mkdir(parents=True, exist_ok=True)
            replay.ANALYSIS_PATH.write_text(SAMPLE_REPORT, encoding="utf-8")
        return run_script(name)

    monkeypatch.setattr(replay, "_run_script", fake_run)
    replay.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    replay.CACHE_PATH.write_text('{"t":"x"}\n')

    assert replay.run_replay_pipeline(scheduled=True) == 0
    assert run_script.call_count >= 1


def test_migrates_legacy_replay_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_data_paths(monkeypatch, tmp_path)
    legacy = tmp_path / "replay_state.json"
    legacy.write_text(
        json.dumps(
            {
                "last_replay_timestamp": "2026-01-01T08:00:00+00:00",
                "bar_count": 99,
                "calibration_factor": 0.8,
            }
        ),
        encoding="utf-8",
    )
    from system import replay_scheduler_state as state_mod

    monkeypatch.setattr(state_mod, "_LEGACY_STATE_PATH", legacy)
    state = load_replay_scheduler_state()
    assert state["last_run_time"] == "2026-01-01T08:00:00+00:00"
    assert state["bars_processed"] == 99
