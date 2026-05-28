from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "replay_scheduler.py"
SPEC = importlib.util.spec_from_file_location("replay_scheduler", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay)

LONDON = ZoneInfo("Europe/London")
SAMPLE_REPORT = (
    "=== JAPAN 225 SIGNAL REPLAY ANALYSIS ===\n"
    "RECOMMENDED CONFIG:\n"
    "  signal_threshold: 75  (best profit factor)\n"
)


def _london(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=LONDON)


def _patch_data_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(replay, "STATE_PATH", tmp_path / "replay_state.json")
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
    assert replay.main() == 0
    after_first = replay.ANALYSIS_PATH.read_text(encoding="utf-8")
    assert run1 in after_first

    _mock_successful_pipeline(
        monkeypatch, analysis_before=after_first, analysis_after=run2
    )
    assert replay.main() == 0
    merged = replay.ANALYSIS_PATH.read_text(encoding="utf-8")
    assert run1 in merged
    assert run2 in merged
    assert "=== REPLAY RUN" in merged


def test_updates_replay_state_after_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_data_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(
        replay,
        "_now_london",
        lambda: _london(2026, 5, 28, 10, 15),
    )
    _mock_successful_pipeline(monkeypatch, bar_lines=5)

    assert replay.main() == 0
    assert replay.STATE_PATH.is_file()
    state = json.loads(replay.STATE_PATH.read_text(encoding="utf-8"))
    assert state["last_replay_timestamp"] == _london(2026, 5, 28, 10, 15).isoformat()
    assert state["bar_count"] == 5
    assert state["calibration_factor"] == 0.75


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

    assert replay.main() == 0
    run_script.assert_not_called()


def test_handles_missing_replay_state_on_first_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_data_paths(monkeypatch, tmp_path)
    assert not replay.STATE_PATH.exists()
    monkeypatch.setattr(
        replay,
        "_now_london",
        lambda: _london(2026, 5, 28, 12, 0),
    )
    _mock_successful_pipeline(monkeypatch)

    assert replay.main() == 0
    state = json.loads(replay.STATE_PATH.read_text(encoding="utf-8"))
    assert "last_replay_timestamp" in state
    assert "bar_count" in state
    assert "calibration_factor" in state
