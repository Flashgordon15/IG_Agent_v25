"""Predictive micro-trend, ML veto floor, regime arbitration, instant scalper lane."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from cockpit.telemetry_schema import OrderBookDepthPayload
from runtime import dual_core_execution as dce
from runtime import master_orchestrator as mo
from signals.indicators import (
    STRATEGY_THRESHOLD_HIGH_PCT,
    STRATEGY_THRESHOLD_LOW_PCT,
    evaluate_micro_trend_alpha,
)
from trading.probability_engine import (
    WIN_VETO_FLOOR_RELAXED,
    WIN_VETO_FLOOR_STRICT,
    apply_hierarchical_probability_gate,
    resolve_dynamic_veto_floor,
)


@pytest.fixture(autouse=True)
def _isolate():
    dce.reset_dual_core_for_tests()
    dce.reset_strategy_execution_for_tests()
    mo.reset_master_orchestrator_for_tests()
    yield
    dce.reset_dual_core_for_tests()
    dce.reset_strategy_execution_for_tests()
    mo.reset_master_orchestrator_for_tests()


def _rising_close(n: int = 64) -> np.ndarray:
    close = np.full(n, 100.0, dtype=np.float64)
    close[-8:] = np.linspace(100.0, 101.6, 8, dtype=np.float64)
    return close


def test_micro_trend_obi_boost_promotes_high_tier():
    close = _rising_close()
    base = evaluate_micro_trend_alpha(close)
    boosted = evaluate_micro_trend_alpha(
        close,
        obi_ratio=0.72,
        prior_obi_ratio=0.35,
        tick_velocity_engaged=True,
    )
    assert boosted["score_pct"] >= base["score_pct"]
    assert boosted["order_flow_aligned"] is True
    assert boosted["ofi_delta"] > 0
    assert boosted["forecast_ticks"] == 4
    assert boosted["promote_tier"] in ("low", "high")
    if boosted["score_pct"] >= STRATEGY_THRESHOLD_HIGH_PCT:
        assert boosted["promote_tier"] == "high"
        assert boosted["forecast_confidence"] > 0


def test_micro_trend_obi_from_depth_payload():
    import time

    close = _rising_close()
    depth = OrderBookDepthPayload(
        epic="CS.D.EURUSD.CFD.IP",
        ts=time.time(),
        bid_levels=[{"price": 1.0850, "size": 500.0}, {"price": 1.0849, "size": 300.0}],
        ask_levels=[{"price": 1.0852, "size": 100.0}, {"price": 1.0853, "size": 80.0}],
    )
    result = evaluate_micro_trend_alpha(
        close,
        order_book_depth=depth,
        prior_obi_ratio=0.0,
        tick_velocity_engaged=True,
    )
    assert result["obi_ratio"] > 0.35
    assert result["order_flow_aligned"] is True


def test_dynamic_veto_floor_strict_by_default():
    assert resolve_dynamic_veto_floor(epic="CS.D.EURUSD.CFD.IP") == WIN_VETO_FLOOR_STRICT


def test_dynamic_veto_floor_relaxed_on_shadow_near_miss_streak():
    manifest = {
        "near_miss_gate": "ml_veto",
        "published_at_epoch": __import__("time").time(),
        "live_floors": {"ml_veto_min_probability": 0.45},
    }
    stats = {"wins": 5, "trades": 6, "winrate": 0.83}

    with patch(
        "system.identity.live_tolerance_bridge.load_live_tolerance_manifest",
        return_value=manifest,
    ):
        with patch("data.learning_store.LearningStore") as mock_store:
            mock_store.return_value.setup_stats.return_value = stats
            with patch("system.config_loader.get_config") as mock_cfg:
                mock_cfg.return_value.learning_db = "/tmp/test_learning.db"
                floor = resolve_dynamic_veto_floor(
                    epic="CS.D.EURUSD.CFD.IP",
                    market="CS.D.EURUSD.CFD.IP",
                )
    assert floor == WIN_VETO_FLOOR_RELAXED


def test_probability_gate_vetoes_below_strict_floor():
    from signals.signal_engine import SignalResult

    sig = SignalResult(
        signal="BUY",
        raw_confidence=60.0,
        adjusted_confidence=60.0,
        learning_delta=0.0,
        setup_key="test",
        notes="",
        snapshot={"raw_signal": "BUY", "last": {"rsi": 55, "atr": 10}},
    )
    vector = np.zeros(128, dtype=np.float64)
    vector[5] = 0.6
    vector[0] = 0.55

    with patch("trading.probability_engine.compute_win_probability", return_value=0.50):
        with patch(
            "trading.probability_engine.resolve_dynamic_veto_floor",
            return_value=WIN_VETO_FLOOR_STRICT,
        ):
            verdict = apply_hierarchical_probability_gate(
                sig=sig,
                feature_payload={"vector": vector},
                peak_score=55.0,
                threshold=55.0,
            )
    assert verdict.veto is True
    assert verdict.model_verdict == "ML_VETO_REJECTION"


def test_regime_entropy_blocks_markov_chop():
    with patch("runtime.regime_switch_engine.evaluate_epic_regime") as mock_eval:
        from runtime.regime_switch_engine import RegimeState

        mock_eval.return_value = MagicMock(state=int(RegimeState.CHOP))
        ok, reason = mo.validate_regime_entropy_arbitration("IX.D.DOW.IFM.IP")
    assert ok is False
    assert "chop" in reason


def test_regime_entropy_blocks_stagnant_dead_zone():
    with patch(
        "runtime.regime_switch_engine.evaluate_epic_regime",
        side_effect=RuntimeError("skip markov"),
    ):
        with patch(
            "runtime.dual_core_execution.epic_in_stagnant_dead_zone",
            return_value=True,
        ):
            with patch(
                "runtime.regime_detection.detect_epic_regime",
                side_effect=RuntimeError("skip legacy"),
            ):
                ok, reason = mo.validate_regime_entropy_arbitration("CS.D.EURUSD.CFD.IP")
    assert ok is False
    assert reason == "unified_regime_stagnant_dead_zone"


def test_predictive_micro_scalp_trigger_requires_high_tier_and_obi():
    with patch("apex.microkernel.get_microkernel") as mock_mk:
        mock_mk.return_value.micro_trend_for.return_value = {
            "score_pct": 48.0,
            "promote_tier": "high",
            "direction": "BUY",
            "order_flow_aligned": True,
            "forecast_confidence": 0.62,
        }
        trigger = dce.evaluate_predictive_micro_scalp_trigger(
            epic="CS.D.EURUSD.CFD.IP",
            bid=1.0850,
            offer=1.0852,
        )
    assert trigger["armed"] is True
    assert trigger["direction"] == "BUY"
    assert trigger["bypass_signal_engine"] is True


def test_predictive_micro_scalp_blocked_when_obi_not_aligned():
    with patch("apex.microkernel.get_microkernel") as mock_mk:
        mock_mk.return_value.micro_trend_for.return_value = {
            "score_pct": 48.0,
            "promote_tier": "high",
            "direction": "BUY",
            "order_flow_aligned": False,
            "forecast_confidence": 0.40,
        }
        trigger = dce.evaluate_predictive_micro_scalp_trigger(
            epic="CS.D.EURUSD.CFD.IP",
            bid=1.0850,
            offer=1.0852,
        )
    assert trigger["armed"] is False
    assert trigger["reason"] == "obi_not_aligned"


def test_micro_scalper_tick_lane_registers_hub_listener():
    hub = MagicMock()
    unsub = MagicMock()
    hub.on_quote.return_value = unsub
    with patch("runtime.dual_core_execution.get_market_data_hub", return_value=hub):
        assert dce.start_micro_scalper_tick_lane() is True
        hub.on_quote.assert_called_once()
        dce.stop_micro_scalper_tick_lane()
        unsub.assert_called_once()
