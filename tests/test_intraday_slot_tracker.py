"""Tests for intraday BST slot performance tracking."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from runtime.intraday_slot_tracker import (
    _STATE_PATH,
    load_persisted_state,
    record_slot_close,
    reset_intraday_slot_tracker_for_tests,
    slot_id_for_timestamp,
    snapshot,
)
from system.config import Config

_BST = ZoneInfo("Europe/London")


def _cfg(**overrides) -> Config:
    data = {
        "intraday_slots": {
            "enabled": True,
            "timezone": "Europe/London",
            "target_min_delta_wr": 0.01,
            "slots": [
                {"id": "pre_europe", "label": "Pre-Europe", "start": "06:00", "end": "08:00"},
                {"id": "europe_open", "label": "Europe Open", "start": "08:00", "end": "09:30"},
                {"id": "us_premarket", "label": "US Pre-Market", "start": "09:30", "end": "14:30"},
                {"id": "us_cash", "label": "US Cash", "start": "14:30", "end": "17:00"},
                {"id": "us_close", "label": "US Close", "start": "17:00", "end": "21:00"},
                {"id": "overnight", "label": "Overnight", "start": "21:00", "end": "06:00"},
            ],
        }
    }
    data.update(overrides)
    return Config(_data=data)


def _ts_bst(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=_BST).timestamp()


def setup_function():
    reset_intraday_slot_tracker_for_tests()


def teardown_function():
    reset_intraday_slot_tracker_for_tests()


def test_slot_assignment_by_time():
    cfg = _cfg()
    assert slot_id_for_timestamp(_ts_bst(2026, 7, 7, 7, 0), cfg) == "pre_europe"
    assert slot_id_for_timestamp(_ts_bst(2026, 7, 7, 8, 30), cfg) == "europe_open"
    assert slot_id_for_timestamp(_ts_bst(2026, 7, 7, 10, 0), cfg) == "us_premarket"
    assert slot_id_for_timestamp(_ts_bst(2026, 7, 7, 15, 0), cfg) == "us_cash"
    assert slot_id_for_timestamp(_ts_bst(2026, 7, 7, 18, 0), cfg) == "us_close"
    assert slot_id_for_timestamp(_ts_bst(2026, 7, 7, 22, 0), cfg) == "overnight"
    assert slot_id_for_timestamp(_ts_bst(2026, 7, 7, 3, 0), cfg) == "overnight"


def test_improvement_detection_one_percent():
    cfg = _cfg()
    ts = _ts_bst(2026, 7, 7, 10, 0)

    # Seed prior epoch stats for us_premarket: 50% WR
    for pnl in (2.0, -1.0):
        record_slot_close(
            epic="IX.D.DOW.IFM.IP",
            pnl_gbp=pnl,
            exit_reason="test",
            ts=ts,
            strategy_epoch="epoch_a",
            cfg=cfg,
        )

    # New epoch — 100% WR (delta +0.5 >= 0.01)
    for pnl in (2.0, 1.5, 3.0):
        record_slot_close(
            epic="IX.D.DOW.IFM.IP",
            pnl_gbp=pnl,
            exit_reason="test",
            ts=ts + 60,
            strategy_epoch="epoch_b",
            cfg=cfg,
        )

    snap = snapshot(cfg=cfg)
    slot = snap["slots"]["us_premarket"]
    assert slot["current"]["n"] == 3
    assert slot["current"]["wins"] == 3
    assert slot["improvement"]["delta_wr"] == 0.5
    assert slot["improvement"]["improving"] is True
    assert slot["improvement"]["regressing"] is False


def test_regression_detection():
    cfg = _cfg()
    ts = _ts_bst(2026, 7, 7, 15, 0)

    for pnl in (3.0, 2.0, 1.5, 2.5):
        record_slot_close(
            epic="IX.D.NIKKEI.IFM.IP",
            pnl_gbp=pnl,
            exit_reason="bank",
            ts=ts,
            strategy_epoch="epoch_a",
            cfg=cfg,
        )

    for pnl in (-2.0, -1.5, -1.0):
        record_slot_close(
            epic="IX.D.NIKKEI.IFM.IP",
            pnl_gbp=pnl,
            exit_reason="soft_loss",
            ts=ts + 120,
            strategy_epoch="epoch_b",
            cfg=cfg,
        )

    slot = snapshot(cfg=cfg)["slots"]["us_cash"]
    assert slot["improvement"]["delta_wr"] == -1.0
    assert slot["improvement"]["regressing"] is True
    assert slot["improvement"]["improving"] is False


def test_snapshot_shape():
    cfg = _cfg()
    record_slot_close(
        epic="IX.D.DOW.IFM.IP",
        pnl_gbp=1.0,
        exit_reason="micro_bank",
        ts=_ts_bst(2026, 7, 7, 7, 30),
        cfg=cfg,
    )

    snap = snapshot(cfg=cfg)
    assert snap["ok"] is True
    assert snap["enabled"] is True
    assert snap["timezone"] == "Europe/London"
    assert snap["current_slot_id"] in snap["slots"]
    assert "day_totals" in snap
    assert "improving_slots" in snap
    assert "regressing_slots" in snap

    pre = snap["slots"]["pre_europe"]
    assert pre["label"] == "Pre-Europe"
    assert pre["start"] == "06:00"
    assert pre["end"] == "08:00"
    assert pre["current"]["n"] == 1
    assert "improvement" in pre
    assert "delta_wr" in pre["improvement"]


def test_persistence_round_trip(tmp_path, monkeypatch):
    cfg = _cfg()
    fake_path = tmp_path / "intraday_slot_performance.json"
    monkeypatch.setattr(
        "runtime.intraday_slot_tracker._STATE_PATH",
        fake_path,
    )

    record_slot_close(
        epic="IX.D.DOW.IFM.IP",
        pnl_gbp=2.5,
        exit_reason="tier",
        ts=_ts_bst(2026, 7, 7, 10, 0),
        cfg=cfg,
    )
    assert fake_path.exists()

    import runtime.intraday_slot_tracker as ist

    with ist._lock:
        ist._state = ist.IntradaySlotState()
    load_persisted_state()

    snap = snapshot(cfg=cfg)
    assert snap["slots"]["us_premarket"]["current"]["n"] == 1
    assert snap["slots"]["us_premarket"]["current"]["total_pnl_gbp"] == 2.5

    raw = json.loads(fake_path.read_text(encoding="utf-8"))
    assert "slots" in raw
    assert "us_premarket" in raw["slots"]
