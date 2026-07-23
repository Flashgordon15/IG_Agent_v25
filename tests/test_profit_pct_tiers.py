"""Tests for percentage profit tier banking and ML categorisation."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

from execution.profit_pct_tiers import (
    assess_profit_tier_strategy,
    build_pct_tiers,
    classify_tier_pct_from_reason,
    pct_tier_bank_reason,
    pct_tiers_enabled,
    runner_should_extend,
)


def _cfg(**overrides) -> SimpleNamespace:
    base = {
        "enabled": True,
        "default_tiers": [5, 7.5, 10, 15, 25, 50, 75, 100],
        "runner_extension": {
            "enabled": True,
            "min_pct": 25,
            "max_pct_skip_bank": 85,
            "sentiment_align_required": True,
            "sentiment_delta_min": 0.0,
            "news_countdown_max": 0.40,
            "min_hold_sec": 45,
            "use_regime_alpha": False,
        },
    }
    base.update(overrides)
    return SimpleNamespace(profit_pct_tiers=base)


def test_pct_tiers_enabled_from_config():
    assert pct_tiers_enabled(_cfg()) is True
    assert pct_tiers_enabled(SimpleNamespace(profit_pct_tiers={"enabled": False})) is False


def test_build_tiers_from_pct_target_10():
    cfg = _cfg()
    tiers = build_pct_tiers(epic="IX.D.DOW.IFM.IP", target_gbp=10.0, cfg=cfg)
    pcts = [t.pct for t in tiers]
    assert pcts == [5.0, 7.5, 10.0, 15.0, 25.0, 50.0, 75.0, 100.0]
    t5 = tiers[0]
    assert t5.peak_min_gbp == 0.5
    assert t5.label == "pct_5"
    t100 = tiers[-1]
    assert t100.peak_min_gbp == 10.0


def test_bank_fires_at_5pct_fade():
    cfg = _cfg()
    decision = pct_tier_bank_reason(
        peak=0.55,
        pnl=0.25,
        target_gbp=10.0,
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        trail_trigger_gbp=5.0,
        cfg=cfg,
    )
    assert decision is not None
    assert decision.runner_extended is False
    assert decision.tier_pct == 5.0
    assert "pct_5" in decision.reason


def test_bank_fires_at_10pct_fade():
    cfg = _cfg()
    decision = pct_tier_bank_reason(
        peak=1.15,
        pnl=0.50,
        target_gbp=10.0,
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        trail_trigger_gbp=5.0,
        cfg=cfg,
    )
    assert decision is not None
    assert decision.tier_pct == 10.0


def test_runner_extension_defers_bank_when_aligned(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr(
        "execution.profit_pct_tiers.capture_exit_context",
        lambda **_: {
            "sentiment_delta_5m": 0.01,
            "sentiment_align": 0.01,
            "news_countdown_norm": 0.1,
            "headline_accel": 0.0,
        },
    )
    armed = time.time() - 120.0
    # peak 30% of £10 target = £3.0; fade on 25% tier
    decision = pct_tier_bank_reason(
        peak=3.0,
        pnl=1.35,
        target_gbp=10.0,
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        trail_trigger_gbp=1.0,
        armed_at=armed,
        cfg=cfg,
    )
    assert decision is not None
    assert decision.runner_extended is True
    assert "runner_extend" in decision.reason


def test_runner_should_extend_false_when_news_hot(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr(
        "execution.profit_pct_tiers.capture_exit_context",
        lambda **_: {
            "sentiment_align": 0.05,
            "news_countdown_norm": 0.55,
        },
    )
    assert (
        runner_should_extend(
            epic="IX.D.DOW.IFM.IP",
            direction="BUY",
            peak_pct_of_target=30.0,
            armed_at=time.time() - 120,
            cfg=cfg,
        )
        is False
    )


def test_classify_tier_pct_from_reason():
    assert classify_tier_pct_from_reason("pct_10 pnl=1.00 peak=1.20") == 10.0
    assert classify_tier_pct_from_reason("pct_7.5 bank") == 7.5
    assert classify_tier_pct_from_reason("soft_loss pnl=-2") is None
    assert classify_tier_pct_from_reason("runner_extend defer_pct_25 peak=3") == 25.0


def test_assess_profit_tier_strategy_buckets():
    closes = [
        {
            "pnl_gbp": 1.0,
            "won": True,
            "profit_tier_pct": 10.0,
            "exit_reason": "pct_10 pnl=1.00",
            "runner_extended": False,
        },
        {
            "pnl_gbp": -2.0,
            "won": False,
            "exit_reason": "soft_loss",
        },
        {
            "pnl_gbp": 2.5,
            "won": True,
            "profit_tier_pct": 25.0,
            "exit_reason": "pct_25 pnl=2.50",
            "runner_extended": True,
        },
    ]
    out = assess_profit_tier_strategy(closes, cfg=_cfg())
    assert out["enabled"] is True
    by = out["by_tier_pct"]
    assert by["10%"]["n"] == 1
    assert by["10%"]["wins"] == 1
    assert by["25%"]["runner_extended"] == 1
    assert "unclassified" in by
