"""Size-up gated on rolling managed WR."""

from __future__ import annotations

from system.strategy_quality_gate import (
    canary_lot_for_epic,
    clamp_size_until_rolling_wr,
)


def test_canary_dow_is_half():
    assert canary_lot_for_epic("IX.D.DOW.IFM.IP", {"strategy_quality": {}}) == 0.5


def test_clamp_keeps_canary_when_sample_short(monkeypatch):
    monkeypatch.setattr(
        "system.strategy_quality_gate.rolling_managed_win_rate",
        lambda **k: (5, 5, 10, 0.50),
    )
    cfg = {
        "strategy_quality": {
            "enabled": True,
            "size_scale_min_wr": 0.55,
            "size_scale_min_sample": 20,
        }
    }
    size, reason = clamp_size_until_rolling_wr("IX.D.DOW.IFM.IP", 2.0, cfg=cfg)
    assert size == 0.5
    assert "size_capped_canary_n" in reason


def test_clamp_allows_size_up_when_wr_proven(monkeypatch):
    monkeypatch.setattr(
        "system.strategy_quality_gate.rolling_managed_win_rate",
        lambda **k: (14, 6, 20, 0.70),
    )
    cfg = {
        "strategy_quality": {
            "enabled": True,
            "size_scale_min_wr": 0.55,
            "size_scale_min_sample": 20,
        }
    }
    size, reason = clamp_size_until_rolling_wr("IX.D.DOW.IFM.IP", 2.0, cfg=cfg)
    assert size == 2.0
    assert "size_scale_ok" in reason


def test_at_canary_passes_through(monkeypatch):
    monkeypatch.setattr(
        "system.strategy_quality_gate.rolling_managed_win_rate",
        lambda **k: (0, 0, 0, 0.0),
    )
    cfg = {"strategy_quality": {"enabled": True}}
    size, reason = clamp_size_until_rolling_wr("IX.D.DOW.IFM.IP", 0.5, cfg=cfg)
    assert size == 0.5
    assert reason == "at_or_below_canary"
