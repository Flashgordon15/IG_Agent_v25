"""DOW entry hour soft-gate — config-driven, no night blackout."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from system.strategy_quality_gate import (
    evaluate_entry_hour_gate,
    hour_gate_size_factor,
)

_LONDON = ZoneInfo("Europe/London")

CFG = {
    "entry_hour_gate": {
        "enabled": True,
        "timezone": "Europe/London",
        "epics": ["IX.D.DOW.IFM.IP"],
        "avoid_hours": [16, 18],
        "prefer_hours": [13, 17],
        "mode": "soft_block",
        "strong_signal_bypass_confidence": 0.72,
        "size_cut_factor": 0.5,
    }
}


def test_avoid_hour_soft_blocks():
    now = datetime(2026, 7, 21, 16, 30, tzinfo=_LONDON)
    ok, reason, meta = evaluate_entry_hour_gate(
        "IX.D.DOW.IFM.IP", cfg=CFG, now=now
    )
    assert ok is False
    assert "avoid_hour_16" in reason
    assert meta["hour"] == 16


def test_prefer_hour_allows():
    now = datetime(2026, 7, 21, 13, 15, tzinfo=_LONDON)
    ok, reason, _meta = evaluate_entry_hour_gate(
        "IX.D.DOW.IFM.IP", cfg=CFG, now=now
    )
    assert ok is True
    assert "prefer_hour_13" in reason


def test_strong_signal_bypass():
    now = datetime(2026, 7, 21, 18, 5, tzinfo=_LONDON)
    ok, reason, meta = evaluate_entry_hour_gate(
        "IX.D.DOW.IFM.IP", cfg=CFG, confidence=0.80, now=now
    )
    assert ok is True
    assert meta.get("bypassed") is True
    assert "bypass" in reason


def test_size_cut_mode():
    cfg = {
        "entry_hour_gate": {
            **CFG["entry_hour_gate"],
            "mode": "size_cut",
        }
    }
    now = datetime(2026, 7, 21, 16, 0, tzinfo=_LONDON)
    ok, reason, meta = evaluate_entry_hour_gate(
        "IX.D.DOW.IFM.IP", cfg=cfg, now=now
    )
    assert ok is True
    assert meta.get("size_cut") is True
    assert hour_gate_size_factor("IX.D.DOW.IFM.IP", cfg=cfg, now=now) == 0.5


def test_other_epic_unaffected():
    now = datetime(2026, 7, 21, 16, 0, tzinfo=_LONDON)
    ok, _reason, _meta = evaluate_entry_hour_gate(
        "IX.D.FTSE.IFM.IP", cfg=CFG, now=now
    )
    assert ok is True


def test_overnight_not_blocked():
    """Night matrix must remain — 22:00 is not in avoid_hours."""
    now = datetime(2026, 7, 21, 22, 30, tzinfo=_LONDON)
    ok, reason, _meta = evaluate_entry_hour_gate(
        "IX.D.DOW.IFM.IP", cfg=CFG, now=now
    )
    assert ok is True
    assert "hour_22_ok" in reason


def test_entry_rate_limit_cooldown():
    from runtime.entry_rate_limit import (
        check_entry_rate_limit,
        record_entry,
        reset_for_tests,
    )

    reset_for_tests()
    cfg = {
        "entry_rate_limit": {
            "enabled": True,
            "per_epic_min_interval_sec": 60,
            "per_epic_max_per_hour": 6,
            "global_max_per_hour": 12,
        }
    }
    ok, _ = check_entry_rate_limit("IX.D.DOW.IFM.IP", cfg=cfg)
    assert ok is True
    record_entry("IX.D.DOW.IFM.IP")
    ok2, reason = check_entry_rate_limit("IX.D.DOW.IFM.IP", cfg=cfg)
    assert ok2 is False
    assert "entry_cooldown" in reason
    reset_for_tests()
