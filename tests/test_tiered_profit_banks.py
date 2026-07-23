"""Tests for tiered profit banking."""

from __future__ import annotations

from types import SimpleNamespace

from execution.tiered_profit_banks import load_profit_bank_tiers, tiered_bank_reason


def test_micro_bank_fires_on_fade():
    reason = tiered_bank_reason(
        peak=1.20,
        pnl=0.65,
        trail_trigger_gbp=2.0,
        cfg=None,
    )
    assert reason is not None
    assert "micro_bank" in reason


def test_no_bank_above_trail_trigger():
    reason = tiered_bank_reason(
        peak=2.0,
        pnl=1.5,
        trail_trigger_gbp=1.0,
        cfg=None,
    )
    assert reason is None


def test_mid_bank_tier():
    reason = tiered_bank_reason(
        peak=3.0,
        pnl=2.0,
        trail_trigger_gbp=5.0,
        cfg=None,
    )
    assert reason is not None
    assert "mid_bank" in reason


def test_load_tiers_from_config():
    cfg = SimpleNamespace(
        micro_risk={
            "tiered_profit_banks": [
                {
                    "peak_min_gbp": 1.0,
                    "bank_floor_gbp": 0.8,
                    "fade_ratio": 0.6,
                    "label": "custom",
                }
            ]
        }
    )
    tiers = load_profit_bank_tiers(cfg)
    assert len(tiers) == 1
    assert tiers[0].label == "custom"
