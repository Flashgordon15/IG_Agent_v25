"""Macro predictive steering — sentiment momentum, news vector, shadow-walk, stream buffer."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from intelligence.macro_radar import collect_macro_snapshot, reset_macro_radar_for_tests
from signals.feature_state import FEATURE_STATE_DIM, compile_current_feature_state
from system.calendar_gate import (
    news_proximity_features,
    quantize_news_countdown_vector,
    reset_calendar_gate_cache_for_tests,
    reset_news_proximity_cache_for_tests,
)
from system.market_data_hub import MarketDataHub
from trading.probability_engine import (
    _FORWARD_WALK_VETO_FLOOR,
    apply_hierarchical_probability_gate,
    compute_news_trailing_sensitivity,
    run_48bar_shadow_walk_expectation,
)
from trading.sentiment_momentum import (
    record_sentiment_sample,
    reset_sentiment_momentum_for_tests,
    sentiment_momentum_features,
)


@pytest.fixture(autouse=True)
def _reset_trackers():
    reset_sentiment_momentum_for_tests()
    reset_macro_radar_for_tests()
    reset_calendar_gate_cache_for_tests()
    reset_news_proximity_cache_for_tests()
    yield
    reset_sentiment_momentum_for_tests()
    reset_macro_radar_for_tests()
    reset_calendar_gate_cache_for_tests()
    reset_news_proximity_cache_for_tests()


def test_sentiment_momentum_5m_30m_derivatives():
    epic = "CS.D.EURUSD.CFD.IP"
    now = time.time()
    record_sentiment_sample(epic, 50.0, ts=now - 1800)
    record_sentiment_sample(epic, 52.0, ts=now - 1500)
    record_sentiment_sample(epic, 55.0, ts=now - 600)
    record_sentiment_sample(epic, 76.0, ts=now - 120)
    record_sentiment_sample(epic, 82.0, ts=now)

    feats = sentiment_momentum_features(epic, now=now)
    assert feats["long_pct"] == pytest.approx(82.0)
    assert feats["delta_5m"] > 0.0
    assert feats["delta_30m"] > 0.0
    assert feats["contrarian_pressure"] > 0.0


def test_feature_vector_includes_sentiment_and_news_slots():
    epic = "CS.D.EURUSD.CFD.IP"
    now = time.time()
    record_sentiment_sample(epic, 40.0, ts=now - 400)
    record_sentiment_sample(epic, 35.0, ts=now)

    payload = compile_current_feature_state(
        epic=epic,
        market=epic,
        snapshot={"last": {"close": 1.085, "rsi": 55, "atr": 5}},
    )
    vec = payload["vector"]
    assert len(vec) == FEATURE_STATE_DIM
    assert vec[98] > 0.0
    assert vec[99] != 0.0 or vec[100] != 0.0


def test_news_proximity_countdown_and_trailing_scale():
    epic = "CS.D.EURUSD.CFD.IP"
    future = datetime.now(timezone.utc) + timedelta(minutes=5)
    with patch("system.calendar_gate._iter_high_impact_events") as mock_events:
        mock_events.return_value = [(future, "NFP", "high")]
        with patch("system.calendar_gate.is_calendar_blocked", return_value=(False, "")):
            feats = news_proximity_features(epic, use_cache=False)
    assert feats["seconds_to_next"] < 400.0
    assert feats["countdown_norm"] > 0.5
    assert feats["trailing_sensitivity_scale"] > 1.2
    qvec = quantize_news_countdown_vector(epic)
    assert len(qvec) == 8
    assert sum(qvec) >= 1.0


def test_news_trailing_sensitivity_scales_with_proximity():
    epic = "CS.D.EURUSD.CFD.IP"
    with patch(
        "system.calendar_gate.news_proximity_features",
        return_value={"trailing_sensitivity_scale": 1.75},
    ):
        scale = compute_news_trailing_sensitivity(epic=epic)
    assert scale == pytest.approx(1.75)


def test_48bar_shadow_walk_warming_when_insufficient_bars():
    with patch("runtime.regime_switch_engine.evaluate_epic_regime") as mock_reg:
        mock_reg.return_value = MagicMock(state=1, confidence=0.0, reason="insufficient_bars")
        walk = run_48bar_shadow_walk_expectation(
            epic="CS.D.CFPGOLD.CFP.IP",
            direction="BUY",
            feature_payload={"vector": np.zeros(128, dtype=np.float64)},
        )
    assert walk["projected_win_prob"] is None
    assert walk["veto"] is False
    assert walk["reason"] == "warming"


def test_48bar_shadow_walk_vetoes_weak_trend_hold():
    vector = np.zeros(128, dtype=np.float64)
    vector[4] = 0.0  # bearish EMA bias
    vector[105] = 0.8  # imminent news

    with patch("runtime.regime_switch_engine.evaluate_epic_regime") as mock_reg:
        mock_reg.return_value = MagicMock(state=2, confidence=0.8)
        with patch(
            "runtime.regime_switch_engine.get_regime_transition_matrix",
            return_value=np.array(
                [[0.20, 0.15, 0.65], [0.20, 0.15, 0.65], [0.20, 0.15, 0.65]],
                dtype=np.float64,
            ),
        ):
            walk = run_48bar_shadow_walk_expectation(
                epic="IX.D.DOW.IFM.IP",
                direction="BUY",
                feature_payload={"vector": vector},
            )
    assert walk["projected_win_prob"] < _FORWARD_WALK_VETO_FLOOR
    assert walk["veto"] is True


def test_momentum_breakout_shadow_walk_veto_in_probability_gate():
    from signals.signal_engine import SignalResult

    sig = SignalResult(
        signal="BUY",
        raw_confidence=70.0,
        adjusted_confidence=70.0,
        learning_delta=0.0,
        setup_key="test",
        notes="",
        snapshot={"raw_signal": "BUY", "last": {"rsi": 60, "atr": 12}},
    )
    vector = np.zeros(128, dtype=np.float64)
    vector[4] = 0.0
    vector[105] = 0.9

    with patch("trading.probability_engine.compute_win_probability", return_value=0.72):
        with patch(
            "trading.probability_engine.run_48bar_shadow_walk_expectation",
            return_value={
                "projected_win_prob": 0.42,
                "veto": True,
                "density": [],
                "reason": "shadow_walk_below_floor",
            },
        ):
            verdict = apply_hierarchical_probability_gate(
                sig=sig,
                feature_payload={"vector": vector},
                peak_score=70.0,
                threshold=55.0,
                epic="IX.D.DOW.IFM.IP",
                execution_path="momentum_breakout",
            )
    assert verdict.veto is True
    assert verdict.model_verdict == "SHADOW_WALK_VETO"


def test_hub_stream_frame_queue_drains_to_publish():
    hub = MarketDataHub()
    hub.start_stream_frame_consumer()
    time.sleep(0.15)
    ok = hub.enqueue_stream_frame(
        "CS.D.EURUSD.CFD.IP",
        1.0850,
        1.0852,
        source="websocket",
    )
    assert ok is True
    time.sleep(0.25)
    snap = hub.get_snapshot("CS.D.EURUSD.CFD.IP")
    hub.stop_stream_frame_consumer()
    assert snap is not None
    assert snap.bid == pytest.approx(1.0850)
    assert snap.source == "websocket"
    metrics = hub.stream_frame_metrics()
    assert metrics["frames_ingested"] >= 1


def test_macro_radar_collects_sentiment_fields():
    record_sentiment_sample("CS.D.EURUSD.CFD.IP", 72.0, ts=time.time() - 200)
    record_sentiment_sample("CS.D.EURUSD.CFD.IP", 78.0, ts=time.time())
    snap = collect_macro_snapshot()
    assert snap.sentiment_long_pct > 50.0
    assert snap.sentiment_delta_5m != 0.0
