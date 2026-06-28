"""Regime detection engine (Phase 5 v35) tests — advisory only."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from api.gui_status import build_gui_status
from runtime.app_mode import reset_app_mode_for_tests
from runtime.regime_detection import (
    MarketRegime,
    align_strategy_to_regime,
    build_regime_detection,
    build_regime_detection_bundle,
    detect_epic_regime,
    reset_regime_detection_for_tests,
    set_regime_detection_for_tests,
)
from runtime.session_lock import lock_path_for_scope, reset_session_lock_state_for_tests, write_session_lock


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    reset_app_mode_for_tests()
    reset_session_lock_state_for_tests()
    reset_regime_detection_for_tests()
    for key in ("APP_MODE", "IG_ACCOUNT_SCOPE", "IG_DATA_ROOT", "IG_TRIAGE_DB"):
        monkeypatch.delenv(key, raising=False)


def _row(**kwargs) -> dict:
    base = {
        "epic": "CS.D.EURUSD.CFD.IP",
        "pipeline_state": "SIGNAL_ONLY",
        "active_strategy_profile": "MOMENTUM",
        "signal_ingested": True,
    }
    base.update(kwargs)
    return base


def test_trend_regime_detection():
    regime = detect_epic_regime(
        "CS.D.EURUSD.CFD.IP",
        epic_row=_row(
            volatility_z=1.4,
            pipeline_state="LIVE",
            active_strategy_profile="MOMENTUM",
        ),
        api_feed_health={"feeds": {"f1": {"status": "OK"}}},
    )
    assert regime["regime_classification"] in (MarketRegime.TREND.value, MarketRegime.CHOP.value)
    assert "TREND_REGIME" in regime["regime_flags"] or regime["regime_confidence"] >= 40


def test_chop_regime_detection():
    regime = detect_epic_regime(
        "CS.D.EURUSD.CFD.IP",
        epic_row=_row(volatility_z=0.3, pipeline_state="IDLE", signal_ingested=False),
        api_feed_health={"feeds": {"f1": {"status": "OK"}}},
    )
    assert regime["regime_classification"] in (
        MarketRegime.CHOP.value,
        MarketRegime.LOW_VOL.value,
        MarketRegime.LIQUIDITY_DROP.value,
    )
    assert "CHOP_REGIME" in regime["regime_flags"] or "LOW_VOL_REGIME" in regime["regime_flags"]


def test_breakout_regime_detection():
    regime = detect_epic_regime(
        "CS.D.EURUSD.CFD.IP",
        epic_row=_row(volatility_z=2.2, spread=0.0012, pipeline_state="LIVE"),
        api_feed_health={"feeds": {"f1": {"status": "OK"}}},
    )
    assert "BREAKOUT_REGIME" in regime["regime_flags"] or regime["regime_classification"] == MarketRegime.BREAKOUT.value


def test_reversal_regime_detection():
    regime = detect_epic_regime(
        "CS.D.EURUSD.CFD.IP",
        epic_row=_row(volatility_z=2.8, pierce_active=True, rotation_active=True),
        market_rotation_status={"active_markets": ["CS.D.EURUSD.CFD.IP"]},
        api_feed_health={"feeds": {"f1": {"status": "OK"}}},
    )
    assert "REVERSAL_REGIME" in regime["regime_flags"] or regime["regime_classification"] == MarketRegime.REVERSAL.value


def test_extreme_volatility_detection():
    regime = detect_epic_regime(
        "CS.D.EURUSD.CFD.IP",
        epic_row=_row(volatility_z=3.0),
        api_feed_health={"feeds": {"f1": {"status": "OK"}}},
    )
    assert regime["regime_classification"] == MarketRegime.EXTREME_VOL.value
    assert "EXTREME_VOL_REGIME" in regime["regime_flags"]


def test_low_volatility_detection():
    regime = detect_epic_regime(
        "CS.D.EURUSD.CFD.IP",
        epic_row=_row(volatility_z=0.2, pipeline_state="SIGNAL_ONLY"),
        api_feed_health={"feeds": {"f1": {"status": "OK"}}},
    )
    assert "LOW_VOL_REGIME" in regime["regime_flags"]
    assert regime["regime_classification"] in (MarketRegime.LOW_VOL.value, MarketRegime.CHOP.value)


def test_liquidity_drop_detection():
    regime = detect_epic_regime(
        "CS.D.EURUSD.CFD.IP",
        epic_row=_row(pipeline_state="IDLE", signal_ingested=False, order_dispatched=False),
        pipeline_governance={
            "per_epic": [
                {
                    "epic": "CS.D.EURUSD.CFD.IP",
                    "pipeline_anomalies": ["ORDER_PENDING_TOO_LONG"],
                }
            ]
        },
        api_feed_health={"feeds": {"f1": {"status": "DEGRADED"}, "f2": {"status": "DEGRADED"}}},
    )
    assert "LIQUIDITY_DROP_REGIME" in regime["regime_flags"] or regime["regime_classification"] == MarketRegime.LIQUIDITY_DROP.value


def test_strategy_alignment_trend_to_momentum():
    regime = {
        "epic": "CS.D.EURUSD.CFD.IP",
        "regime_classification": MarketRegime.TREND.value,
        "regime_confidence": 75,
        "regime_flags": ["TREND_REGIME"],
    }
    alignment = align_strategy_to_regime(regime)
    assert alignment["recommended_profile"] == "MOMENTUM"
    assert "TREND_ALIGNMENT" in alignment["alignment_flags"]


def test_strategy_alignment_chop_to_scalp():
    regime = {
        "epic": "CS.D.EURUSD.CFD.IP",
        "regime_classification": MarketRegime.CHOP.value,
        "regime_confidence": 70,
        "regime_flags": ["CHOP_REGIME"],
    }
    alignment = align_strategy_to_regime(regime)
    assert alignment["recommended_profile"] == "SCALP"


def test_strategy_alignment_reversal_to_rotation():
    regime = {
        "epic": "CS.D.EURUSD.CFD.IP",
        "regime_classification": MarketRegime.REVERSAL.value,
        "regime_confidence": 72,
        "regime_flags": ["REVERSAL_REGIME"],
    }
    alignment = align_strategy_to_regime(regime)
    assert alignment["recommended_profile"] == "ROTATION"


def test_strategy_alignment_liquidity_to_stand_down():
    regime = {
        "epic": "CS.D.EURUSD.CFD.IP",
        "regime_classification": MarketRegime.LIQUIDITY_DROP.value,
        "regime_confidence": 80,
        "regime_flags": ["LIQUIDITY_DROP_REGIME"],
    }
    alignment = align_strategy_to_regime(regime)
    assert alignment["recommended_profile"] == "STAND_DOWN"


def test_extreme_vol_scalp_override_from_performance_memory():
    regime = {
        "epic": "CS.D.EURUSD.CFD.IP",
        "regime_classification": MarketRegime.EXTREME_VOL.value,
        "regime_confidence": 85,
        "regime_flags": ["EXTREME_VOL_REGIME"],
    }
    alignment = align_strategy_to_regime(
        regime,
        strategy_weighting_advice={"recommended_bias": "SCALP", "bias_confidence": 70},
    )
    assert alignment["recommended_profile"] == "SCALP"
    assert "EXTREME_VOL_SCALP_OVERRIDE" in alignment["alignment_flags"]


def test_bundle_builds_per_epic():
    bundle = build_regime_detection_bundle(
        trade_pipeline_health=[_row(), _row(epic="CS.D.CFPGOLD.CFP.IP")],
        api_feed_health={"feeds": {"f1": {"status": "OK"}}},
    )
    assert len(bundle["regime_detection"]) == 2
    assert len(bundle["regime_strategy_alignment"]) == 2


def test_gui_status_includes_regime_fields(tmp_path, monkeypatch):
    scope = "ig:REG1"
    root = tmp_path / "production"
    root.mkdir()
    monkeypatch.setenv("APP_MODE", "DEMO")
    monkeypatch.setenv("IG_ACCOUNT_SCOPE", scope)
    monkeypatch.setenv("IG_DATA_ROOT", str(root))
    reset_app_mode_for_tests()
    write_session_lock(
        lock_path_for_scope(scope, root),
        pid=os.getpid(),
        port=8080,
        account_scope=scope,
    )

    with patch("api.gui_status.build_trade_pipeline_health", return_value=[_row()]), patch(
        "api.gui_status.build_pipeline_governance",
        return_value={
            "pipeline_governance": {"per_epic": []},
            "session_governance": {},
            "gui_alerts": [],
        },
    ), patch("api.gui_status.build_strategy_selector_advice", return_value=[]), patch(
        "api.gui_status.build_strategy_controller_decisions",
        return_value=[],
    ), patch(
        "api.gui_status.build_strategy_transition_advice",
        return_value=[],
    ), patch(
        "api.gui_status.build_strategy_enforcement_decisions",
        return_value=[],
    ), patch(
        "api.gui_status.build_hard_enforcement_decisions",
        return_value=[],
    ), patch(
        "api.gui_status.build_api_feed_health",
        return_value={"feeds": {"f1": {"status": "OK"}}, "ranking": {"primary": "f1"}},
    ), patch(
        "api.gui_status.build_market_rotation_status",
        return_value={"active_markets": []},
    ), patch(
        "api.gui_status.build_session_review_bundle",
        return_value={"session_review": {}, "loosening_advice": {}, "self_reflection": {}},
    ), patch(
        "api.gui_status.build_adaptive_thresholds",
        return_value={"threshold_adjustments": {}, "adjustment_flags": []},
    ), patch(
        "api.gui_status.build_strategy_performance_bundle",
        return_value={
            "strategy_performance_memory": {},
            "strategy_weighting_advice": {"recommended_bias": "MOMENTUM", "bias_confidence": 50},
        },
    ), patch(
        "runtime.regime_detection._volatility_z",
        return_value=1.2,
    ), patch(
        "runtime.regime_detection._hub_spread_age",
        return_value=(0.0003, 5.0),
    ), patch(
        "runtime.regime_detection.epic_z_pierce_active",
        return_value=False,
    ), patch(
        "runtime.regime_detection._stack_snapshot",
        return_value={"core_a_macro_active": True, "volatility_z_score": 1.2},
    ):
        payload = build_gui_status()

    assert "regime_detection" in payload
    assert "regime_strategy_alignment" in payload
    assert isinstance(payload["regime_detection"], list)
    assert isinstance(payload["regime_strategy_alignment"], list)


def test_no_execution_side_effects():
    """Regime detection must not invoke LiveExecutor or order placement."""
    with patch("execution.live_executor.LiveExecutor") as live_exec:
        build_regime_detection(
            trade_pipeline_health=[_row(volatility_z=1.5)],
            api_feed_health={"feeds": {"f1": {"status": "OK"}}},
        )
        live_exec.assert_not_called()


def test_test_override_hook():
    set_regime_detection_for_tests(
        detection=[{"epic": "X", "regime_classification": "TREND"}],
        alignment=[{"epic": "X", "recommended_profile": "MOMENTUM"}],
    )
    assert build_regime_detection(trade_pipeline_health=[_row()])[0]["epic"] == "X"
