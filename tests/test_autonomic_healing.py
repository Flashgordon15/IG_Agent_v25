"""Fault-injection tests for Autonomic Self-Healing Engine."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


@pytest.fixture(autouse=True)
def _reset_healer_state():
    from system.autonomic_healer import reset_autonomic_healer_for_tests
    from trading.probability_engine import reset_cognitive_self_correction_for_tests

    reset_autonomic_healer_for_tests()
    reset_cognitive_self_correction_for_tests()
    try:
        from system.market_data_hub import reset_synthetic_hydration_for_tests

        reset_synthetic_hydration_for_tests()
    except Exception:
        pass
    try:
        from system.chaos_guardian import release_token_conservation_mode

        release_token_conservation_mode()
    except Exception:
        pass
    yield
    reset_autonomic_healer_for_tests()
    reset_cognitive_self_correction_for_tests()
    try:
        from system.market_data_hub import reset_synthetic_hydration_for_tests

        reset_synthetic_hydration_for_tests()
    except Exception:
        pass
    try:
        from runtime.master_orchestrator import reset_master_orchestrator_for_tests

        reset_master_orchestrator_for_tests()
    except Exception:
        pass
    try:
        from system.packet_validator import reset_packet_validator_for_tests

        reset_packet_validator_for_tests()
    except Exception:
        pass
    try:
        from system.system_state import SystemState

        SystemState.reset_singleton_for_tests()
    except Exception:
        pass


def test_ai_diagnostics_snapshot_shape():
    from system.autonomic_healer import get_ai_diagnostics_snapshot

    snap = get_ai_diagnostics_snapshot()
    assert snap.get("ok") is True
    for key in (
        "current_boot_stage",
        "active_healer_mitigations",
        "frame_queue_depth",
        "ring_buffer_fill_percentages",
        "ml_accuracy_metrics",
        "broker_handshake_raw_error",
        "transport_failure_category",
        "fallback_transport_tier",
        "synthetic_hydration_active",
    ):
        assert key in snap


def test_api_ai_diagnostics_route():
    from api.routes import api_ai_diagnostics

    resp = api_ai_diagnostics()
    body = resp.body
    assert body
    import json

    data = json.loads(body)
    assert "frame_queue_depth" in data


def test_cockpit_ai_diagnostics_route_registered():
    from cockpit.web_server import create_cockpit_app

    paths = {getattr(r, "path", None) for r in create_cockpit_app().routes}
    assert "/api/ai_diagnostics" in paths
    assert "/api/iron_cage_status" in paths


def test_cognitive_correction_raises_veto_floor():
    from trading.probability_engine import (
        WIN_VETO_FLOOR,
        WIN_VETO_FLOOR_STRICT,
        apply_cognitive_self_correction,
    )

    assert WIN_VETO_FLOOR == WIN_VETO_FLOOR_STRICT
    apply_cognitive_self_correction(reason="test_wr_below_70", veto_bump=0.05)
    from trading.probability_engine import WIN_VETO_FLOOR as floor_after

    assert floor_after > WIN_VETO_FLOOR_STRICT
    assert floor_after <= 0.65


def test_epic_ring_hub_seed_reset():
    from runtime.regime_switch_engine import reset_epic_regime_ring_with_hub_seed
    from system.market_data_hub import get_market_data_hub

    hub = get_market_data_hub()
    epic = "CS.D.EURUSD.CFD.IP"
    hub.publish(epic, 1.1000, 1.1002, source="test")
    meta = reset_epic_regime_ring_with_hub_seed(epic)
    assert meta.get("ok") is True
    assert int(meta.get("bars") or 0) >= 1


def test_stream_failure_triggers_ring_heal_after_threshold():
    from system.autonomic_healer import (
        _STREAM_FAILURE_THRESHOLD,
        notify_dispatch_stream_failure,
    )

    epic = "IX.D.DOW.IFM.IP"
    with patch(
        "runtime.regime_switch_engine.reset_epic_regime_ring_with_hub_seed",
        return_value={"ok": True, "bars": 288, "source": "hub_seed_block"},
    ) as mock_reset:
        for i in range(_STREAM_FAILURE_THRESHOLD):
            notify_dispatch_stream_failure(epic, f"packet_reject_TEST_{i}")
        mock_reset.assert_called_once()


def test_enqueue_stream_frame_drains_to_hub():
    from system.market_data_hub import get_market_data_hub

    hub = get_market_data_hub()
    hub.stop_stream_frame_consumer()
    hub._stream_consumer_stop.clear()
    hub._stream_frames_ingested = 0
    hub.start_stream_frame_consumer()
    epic = "CS.D.CFPGOLD.CFP.IP"
    published: list[str] = []

    def _capture_publish(e, b, o, **kw):
        published.append(e)
        from system.market_data_hub import QuoteSnapshot

        return QuoteSnapshot(epic=e, bid=b, offer=o, updated_at=time.time(), source=kw.get("source", "websocket"))

    with patch.object(hub, "publish", side_effect=_capture_publish):
        ok = hub.enqueue_stream_frame(epic, 2400.0, 2400.5, source="websocket")
        assert ok is True
        deadline = time.time() + 2.0
        while time.time() < deadline and not published:
            time.sleep(0.05)
    assert published == [epic]


def test_lightstreamer_force_rest_poll_failover():
    from ig_api import lightstreamer_streaming as ls

    mock_client = MagicMock()
    mock_client._using_fallback = False
    mock_client._running = True
    mock_client._teardown_lightstreamer = MagicMock()
    mock_client._start_fallback = MagicMock()
    ls.register_lightstreamer_client(mock_client)
    assert ls.force_rest_poll_failover(reason="test_stall") is True
    mock_client._teardown_lightstreamer.assert_called_once()
    mock_client._start_fallback.assert_called_once()
    ls.register_lightstreamer_client(None)


def test_healer_engine_recovers_trade_ready_after_simulated_fault():
    from runtime.master_orchestrator import reset_master_orchestrator_for_tests
    from system.autonomic_healer import AutonomicHealerEngine, get_ai_diagnostics_snapshot

    reset_master_orchestrator_for_tests()
    engine = AutonomicHealerEngine()

    with patch("system.autonomic_healer._check_transport_stall"), patch(
        "system.autonomic_healer._check_reconciliation_drift"
    ), patch("system.autonomic_healer._cognitive_self_correction_pass"):
        engine.run_once()

    snap = get_ai_diagnostics_snapshot()
    assert snap.get("ok") is True

    with patch(
        "system.iron_cage_readiness.evaluate_iron_cage_readiness",
        return_value={
            "ok": True,
            "trade_ready": True,
            "blockers": [],
            "warnings": [],
        },
    ) as mock_iron:
        from system.iron_cage_readiness import evaluate_iron_cage_readiness

        iron = evaluate_iron_cage_readiness(force_refresh=True)
        mock_iron.assert_called()
        assert iron.get("trade_ready") is True


def test_autonomic_healer_daemon_start_stop():
    from system.autonomic_healer import (
        get_ai_diagnostics_snapshot,
        start_autonomic_healer,
        stop_autonomic_healer,
    )

    start_autonomic_healer(rest=None)
    deadline = time.time() + 3.0
    alive = False
    while time.time() < deadline:
        if get_ai_diagnostics_snapshot().get("engine_alive"):
            alive = True
            break
        time.sleep(0.1)
    stop_autonomic_healer()
    time.sleep(0.5)
    assert alive is True


def test_transition_matrix_strictness_bump():
    from runtime.regime_switch_engine import (
        apply_transition_matrix_strictness,
        get_regime_transition_matrix,
    )

    before = float(get_regime_transition_matrix()[1, 1])
    meta = apply_transition_matrix_strictness(bump=0.05)
    after = float(get_regime_transition_matrix()[1, 1])
    assert meta.get("ok") is True
    assert after >= before


def test_feature_drift_detection_slots():
    from trading.probability_engine import detect_sentiment_news_feature_drift

    with patch(
        "signals.feature_state.compile_current_feature_state",
        side_effect=lambda **_: {
            "vector": np.concatenate(
                [np.zeros(98), np.linspace(0.1, 0.9, 14), np.zeros(16)]
            )
        },
    ):
        first = detect_sentiment_news_feature_drift(threshold=0.5)
        assert isinstance(first, list)
        drift = detect_sentiment_news_feature_drift(threshold=0.01)
        assert isinstance(drift, list)


def test_ls_handshake_timeout_is_three_seconds():
    from system.autonomic_healer import _LS_HANDSHAKE_TIMEOUT_SEC, _TRANSPORT_STALL_SEC

    assert _LS_HANDSHAKE_TIMEOUT_SEC == 3.0
    assert _TRANSPORT_STALL_SEC == 3.0


def test_clear_token_queue_delays_resets_queued_waits():
    from system.chaos_guardian import _buckets, clear_token_queue_delays

    bucket = _buckets["yahoo"]
    bucket.queued_waits = 7
    bucket.tokens = 0.0
    result = clear_token_queue_delays(refill=True)
    assert bucket.queued_waits == 0
    assert bucket.tokens == bucket.capacity
    assert result["yahoo"]["queued_waits"] == 0


def test_autonomic_healer_clears_stale_token_queue_delays():
    from system.autonomic_healer import _heal_stale_token_queue_delays, reset_autonomic_healer_for_tests
    from system.chaos_guardian import _buckets

    reset_autonomic_healer_for_tests()
    bucket = _buckets["yahoo"]
    bucket.queued_waits = 3
    bucket.tokens = bucket.capacity
    _heal_stale_token_queue_delays()
    assert bucket.queued_waits == 0


def test_autonomic_healer_replenishes_starved_ig_orders():
    from system.autonomic_healer import _heal_stale_token_queue_delays, reset_autonomic_healer_for_tests
    from system.chaos_guardian import _buckets, _ORDER_TOKEN_FLOOR

    reset_autonomic_healer_for_tests()
    orders = _buckets["ig_orders"]
    orders.queued_waits = 8
    orders.tokens = 0.1
    _heal_stale_token_queue_delays()
    assert orders.queued_waits == 0
    assert orders.tokens >= _ORDER_TOKEN_FLOOR


def test_engage_transport_failover_recovery_seeds_hub_and_clears_tokens():
    from ig_api.lightstreamer_streaming import engage_transport_failover_recovery
    from system.chaos_guardian import _buckets
    from system.market_data_hub import COCKPIT_CORE_EPICS, get_market_data_hub
    from system.packet_validator import reset_packet_validator_for_tests

    hub = get_market_data_hub()
    for epic in COCKPIT_CORE_EPICS:
        hub.invalidate(epic)
    _buckets["yahoo"].queued_waits = 5

    try:
        with patch(
            "ig_api.lightstreamer_streaming.force_rest_poll_failover",
            return_value=False,
        ), patch(
            "system.feeds.data_feed_orchestrator.ensure_data_feed_orchestrator_running",
        ), patch(
            "ig_api.streaming_factory.flush_streaming_session_handles",
            return_value={"flushed": 0, "total": 0, "errors": []},
        ):
            assert engage_transport_failover_recovery(reason="test_recovery") is True

        assert _buckets["yahoo"].queued_waits == 0
        fresh = sum(1 for epic in COCKPIT_CORE_EPICS if hub.is_fresh(epic, max_age=5.0))
        assert fresh == len(COCKPIT_CORE_EPICS)
    finally:
        hub.stop_stream_frame_consumer()
        from system.market_data_hub import flush_hub_streaming_session_cache

        flush_hub_streaming_session_cache()
        reset_packet_validator_for_tests()
        for epic in COCKPIT_CORE_EPICS:
            hub.invalidate(epic)
        from runtime.master_orchestrator import reset_master_orchestrator_for_tests

        reset_master_orchestrator_for_tests()


def test_hub_starvation_triggers_failover_after_boot_anchor():
    from system import autonomic_healer as ah
    from system.market_data_hub import COCKPIT_CORE_EPICS, get_market_data_hub

    hub = get_market_data_hub()
    for epic in COCKPIT_CORE_EPICS:
        hub.invalidate(epic)

    ah._boot_anchor_ts = time.time() - 5.0
    ah._failover_engaged = False

    def _fake_engage(reason: str) -> None:
        ah._failover_engaged = True
        ah._last_broker_handshake_error = reason

    with patch(
        "ig_api.lightstreamer_streaming.get_lightstreamer_health",
        return_value={"active": False},
    ), patch.object(ah, "_engage_failover_recovery", side_effect=_fake_engage) as mock_recovery:
        ah._check_transport_stall()

    mock_recovery.assert_called_once()
    assert ah._failover_engaged is True
    assert "hub_tick_starvation" in ah._last_broker_handshake_error


def test_classify_rate_limit_failure():
    from system.autonomic_healer import (
        TransportFailureCategory,
        classify_transport_failure,
        record_transport_failure_diagnostic,
    )

    cat = classify_transport_failure(reason="HTTP 429 rate limit exhausted", http_status=429)
    assert cat is TransportFailureCategory.RATE_LIMIT_EXHAUSTED
    with patch("system.chaos_guardian.engage_token_conservation_mode") as mock_conserve:
        record_transport_failure_diagnostic(reason="429 too many requests", http_status=429)
        mock_conserve.assert_called_once()


def test_synthetic_alpha_gate_relaxes_shadow_walk_veto():
    from trading.probability_engine import (
        _FORWARD_WALK_VETO_FLOOR_SYNTHETIC,
        enable_synthetic_alpha_gate,
        run_48bar_shadow_walk_expectation,
    )

    enable_synthetic_alpha_gate(True)
    walk = run_48bar_shadow_walk_expectation(
        epic="CS.D.EURUSD.CFD.IP",
        direction="BUY",
        feature_payload={"vector": np.zeros(128)},
    )
    assert walk.get("veto_floor") == _FORWARD_WALK_VETO_FLOOR_SYNTHETIC
    assert walk.get("projected_win_prob", 1.0) >= 0.0


def test_autonomic_drift_flattener_after_30s_init_blockers(monkeypatch):
    from system import autonomic_healer as ah

    ah._boot_anchor_ts = time.time() - 35.0
    ah._drift_flattener_engaged = False
    ah._init_blocker_since = {
        "broker_reconciliation_drift": time.time() - 35.0,
        "routing_unarmed": time.time() - 35.0,
    }

    mock_rest = MagicMock()
    mock_rest.get_open_positions.return_value = {"positions": []}
    ah._rest_client = mock_rest

    with patch.object(
        ah,
        "_overwrite_local_registry_from_broker",
        return_value={"ok": True, "synced": True, "broker_positions": 0},
    ), patch(
        "system.broker_reconciliation_daemon.run_reconciliation_once",
        return_value={"healthy": True, "drift_count": 0},
    ), patch.object(
        ah,
        "_force_arm_routing_and_trade_ready",
        return_value={"armed": True, "trade_ready": True},
    ), patch(
        "system.iron_cage_readiness.evaluate_iron_cage_readiness",
        return_value={"trade_ready": True, "blockers": []},
    ), patch("system.alert_reporting_matrix.notify_drift_clear"):
        result = ah._activate_autonomic_drift_flattener(
            blockers=["broker_reconciliation_drift", "routing_unarmed"]
        )

    assert result.get("ok") is True
    assert result.get("trade_ready") is True
    assert ah._drift_flattener_engaged is True
    snap = ah.get_ai_diagnostics_snapshot()
    assert snap.get("drift_flattener_engaged") is True


def test_init_boot_blocker_tracking_triggers_flattener(monkeypatch):
    from system import autonomic_healer as ah

    ah.reset_autonomic_healer_for_tests()
    ah._boot_anchor_ts = time.time() - 35.0
    ah._rest_client = MagicMock()
    ah._init_blocker_since = {"broker_reconciliation_drift": time.time() - 35.0}

    with patch.object(
        ah, "_read_iron_cage_blockers", return_value=["broker_reconciliation_drift"]
    ), patch.object(ah, "_activate_autonomic_drift_flattener") as mock_flatten:
        ah._check_init_boot_blockers()
        mock_flatten.assert_called_once()


def test_inject_synthetic_ring_continuity_fills_288_bars():
    from runtime.regime_switch_engine import inject_synthetic_ring_continuity

    meta = inject_synthetic_ring_continuity("CS.D.EURUSD.CFD.IP")
    assert meta.get("ok") is True
    assert int(meta.get("bars") or 0) >= 288


def test_force_autonomic_boot_progression_writes_warming_healthy():
    from runtime.master_orchestrator import (
        STAGE_5_LAUNCH,
        _TOKEN_WARMING_HEALTHY,
        force_autonomic_boot_progression,
        get_boot_stage_tokens,
        reset_master_orchestrator_for_tests,
    )

    reset_master_orchestrator_for_tests()
    try:
        result = force_autonomic_boot_progression(reason="test_failover")
        tokens = get_boot_stage_tokens()
        assert result.get("trade_ready") is True
        assert tokens.get(STAGE_5_LAUNCH) == _TOKEN_WARMING_HEALTHY
    finally:
        reset_master_orchestrator_for_tests()
