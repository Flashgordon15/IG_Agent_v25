"""Tests for v25→v26 feeder event bus."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from feeder.event_bus import (
    emit,
    emit_fill_close,
    emit_signal_eval,
    set_enabled_for_tests,
)
from system.paths import feeder_events_dir, project_root


@pytest.fixture
def feeder_events_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    lake = tmp_path / "data_lake" / "events"
    lake.mkdir(parents=True)
    monkeypatch.setattr("system.paths.project_root", lambda: tmp_path)
    monkeypatch.setattr("feeder.event_bus.feeder_events_dir", lambda: lake)
    set_enabled_for_tests(True)
    yield lake
    set_enabled_for_tests(None)


def test_emit_writes_jsonl(feeder_events_tmp: Path) -> None:
    emit(
        "gate_result",
        epic="IX.D.NASDAQ.IFM.IP",
        market="US Tech 100",
        session="us_afternoon",
        payload={"gate_name": "session_open", "passed": True},
    )
    files = list(feeder_events_tmp.glob("*.jsonl"))
    assert len(files) == 1
    row = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert row["contract_version"] == "1.0"
    assert row["event_type"] == "gate_result"
    assert row["epic"] == "IX.D.NASDAQ.IFM.IP"
    assert row["payload"]["passed"] is True


def test_emit_disabled_by_default_in_pytest() -> None:
    set_enabled_for_tests(None)
    # conftest sets IG_AGENT_PYTEST=1
    from feeder import event_bus

    assert event_bus.is_enabled() is False


def test_emit_signal_eval_helpers(feeder_events_tmp: Path) -> None:
    emit_signal_eval(
        epic="IX.D.NIKKEI.IFM.IP",
        market="Japan 225",
        session="asia_early",
        direction="BUY",
        raw_score=88.0,
        adjusted_score=90.0,
        setup_key="BUY|bull|asia_early",
        would_fire=True,
        gates_passed=["session_open", "signal_confidence"],
    )
    line = next(feeder_events_tmp.glob("*.jsonl")).read_text(encoding="utf-8").strip()
    row = json.loads(line)
    assert row["event_type"] == "signal_eval"
    assert row["payload"]["would_fire"] is True
    assert "session_open" in row["payload"]["gates_passed"]


def test_emit_fill_close(feeder_events_tmp: Path) -> None:
    emit_fill_close(
        epic="IX.D.DOW.IFM.IP",
        market="Wall Street",
        trade_id=42,
        deal_id="DIAAA",
        pnl_gbp=120.5,
        pnl_points=15.0,
        result="WIN",
        exit_reason="trail",
        setup_key="BUY|bull",
        confidence=85.0,
    )
    row = json.loads(next(feeder_events_tmp.glob("*.jsonl")).read_text().strip())
    assert row["event_type"] == "fill_close"
    assert row["payload"]["pnl_gbp"] == 120.5


def test_feeder_events_dir_under_project_root() -> None:
    p = feeder_events_dir()
    assert p == project_root() / "data_lake" / "events"
