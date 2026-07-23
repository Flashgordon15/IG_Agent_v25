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
        "apply_accounts": ["Z6BAH3", "Z6BAH4"],
        "prime_hours": [7, 8, 9, 13, 14, 15, 16, 17],
        "prefer_hours": [13, 17],
        "avoid_hours": [18, 19],
        "mode": "soft_block",
        "outside_prime_min_confidence": 0.68,
        "strong_signal_bypass_confidence": 0.75,
        "size_cut_factor": 0.5,
    }
}


def test_avoid_hour_soft_blocks():
    now = datetime(2026, 7, 21, 18, 30, tzinfo=_LONDON)
    ok, reason, meta = evaluate_entry_hour_gate(
        "IX.D.DOW.IFM.IP", cfg=CFG, now=now
    )
    assert ok is False
    assert "avoid_hour_18" in reason
    assert meta["hour"] == 18


def test_prime_hour_allows():
    now = datetime(2026, 7, 21, 13, 15, tzinfo=_LONDON)
    ok, reason, _meta = evaluate_entry_hour_gate(
        "IX.D.DOW.IFM.IP", cfg=CFG, now=now
    )
    assert ok is True
    assert "prime_hour_13" in reason


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
    now = datetime(2026, 7, 21, 18, 0, tzinfo=_LONDON)
    ok, reason, meta = evaluate_entry_hour_gate(
        "IX.D.DOW.IFM.IP", cfg=cfg, now=now
    )
    assert ok is True
    assert meta.get("size_cut") is True
    assert hour_gate_size_factor("IX.D.DOW.IFM.IP", cfg=cfg, now=now) == 0.5


def test_other_epic_unaffected():
    now = datetime(2026, 7, 21, 18, 0, tzinfo=_LONDON)
    ok, _reason, _meta = evaluate_entry_hour_gate(
        "IX.D.FTSE.IFM.IP", cfg=CFG, now=now
    )
    assert ok is True


def test_overnight_allowed_without_confidence():
    """Night matrix must remain — no conf ⇒ outside-prime soft allow."""
    now = datetime(2026, 7, 21, 22, 30, tzinfo=_LONDON)
    ok, reason, meta = evaluate_entry_hour_gate(
        "IX.D.DOW.IFM.IP", cfg=CFG, now=now
    )
    assert ok is True
    assert "outside_prime_hour_22" in reason
    assert meta.get("outside_prime") is True


def test_outside_prime_requires_higher_ml():
    now = datetime(2026, 7, 21, 22, 30, tzinfo=_LONDON)
    ok, reason, _meta = evaluate_entry_hour_gate(
        "IX.D.DOW.IFM.IP", cfg=CFG, confidence=0.55, now=now
    )
    assert ok is False
    assert "outside_prime_hour_22_ml_gate" in reason

    ok2, reason2, _ = evaluate_entry_hour_gate(
        "IX.D.DOW.IFM.IP", cfg=CFG, confidence=0.70, now=now
    )
    assert ok2 is True
    assert "outside_prime_hour_22_ok" in reason2


def test_applies_to_both_accounts():
    now = datetime(2026, 7, 21, 18, 0, tzinfo=_LONDON)
    for acct in ("Z6BAH3", "Z6BAH4"):
        ok, reason, _ = evaluate_entry_hour_gate(
            "IX.D.DOW.IFM.IP", cfg=CFG, now=now, account_id=acct
        )
        assert ok is False, acct
        assert "avoid_hour_18" in reason


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
