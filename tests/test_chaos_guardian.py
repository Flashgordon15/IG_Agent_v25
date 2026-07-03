"""Chaos guardian — fault injection tests for SRE hardening layer."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from system import chaos_guardian as cg
from system import packet_validator as pv


@pytest.fixture(autouse=True)
def _isolate():
    cg.reset_chaos_guardian_for_tests()
    pv.reset_packet_validator_for_tests()
    yield
    cg.reset_chaos_guardian_for_tests()
    pv.reset_packet_validator_for_tests()


def test_token_bucket_exhaustion_blocks_acquire():
    bucket = cg.TokenBucket("test", capacity=2.0, refill_rate=0.0)
    assert bucket.try_acquire(1.0) is True
    assert bucket.try_acquire(1.0) is True
    assert bucket.try_acquire(1.0) is False
    assert bucket.acquire(1.0, max_wait_sec=0.05) is False


def test_token_bucket_refill_allows_retry():
    bucket = cg.TokenBucket("test", capacity=1.0, refill_rate=10.0)
    assert bucket.try_acquire(1.0) is True
    assert bucket.try_acquire(1.0) is False
    time.sleep(0.15)
    assert bucket.try_acquire(1.0) is True


def test_queued_waits_decay_on_successful_acquire():
    bucket = cg.TokenBucket("test", capacity=3.0, refill_rate=0.0)
    bucket.queued_waits = 4
    assert bucket.try_acquire(1.0) is True
    assert bucket.queued_waits == 0
    bucket.queued_waits = 2
    bucket.tokens = 0.5
    assert bucket.try_acquire(0.5) is True
    assert bucket.queued_waits == 1


def test_refresh_snapshot_decays_stale_queued_waits():
    bucket = cg._buckets["yahoo"]
    bucket.queued_waits = 6
    bucket.tokens = bucket.capacity
    cg._refresh_snapshot()
    assert bucket.queued_waits == 0


def test_replenish_critical_buckets_restores_ig_orders():
    cg.reset_chaos_guardian_for_tests()
    orders = cg._buckets["ig_orders"]
    orders.tokens = 0.05
    orders.queued_waits = 12
    result = cg.replenish_critical_buckets()
    assert result["ig_orders"]["replenished"] is True
    assert orders.tokens >= cg._ORDER_TOKEN_FLOOR
    assert orders.queued_waits == 0


def test_acquire_outbound_token_replenishes_orders_on_exhaustion():
    cg.reset_chaos_guardian_for_tests()
    orders = cg._buckets["ig_orders"]
    orders.tokens = 0.0
    orders.refill_rate = 0.0
    orders.queued_waits = 5
    ok = cg.acquire_outbound_token(
        "ig", method="POST", path="/positions/otc", category="orders", max_wait_sec=0.01
    )
    assert ok is False
    assert orders.tokens >= cg._ORDER_TOKEN_FLOOR
    assert orders.queued_waits == 0


def test_ig_order_path_uses_orders_bucket():
    cg.reset_chaos_guardian_for_tests()
    orders = cg._buckets["ig_orders"]
    orders.tokens = 0.0
    orders.refill_rate = 0.0
    ok = cg.acquire_outbound_token(
        "ig", method="POST", path="/positions/otc", category="orders", max_wait_sec=0.01
    )
    assert ok is False


def test_demo_throughput_raises_ig_orders_capacity():
    cg.reset_chaos_guardian_for_tests()
    orders = cg._buckets["ig_orders"]
    orders.tokens = 8.0
    orders.refill_rate = 0.0
    with patch("system.demo_execution_plane.demo_throughput_active", return_value=True):
        with patch(
            "system.config_loader.get_config",
            return_value={"demo_throughput_mode": {"bypass_traffic_governor": True}},
        ):
            assert cg.acquire_outbound_token(
                "ig",
                method="POST",
                path="/positions/otc",
                category="orders",
                max_wait_sec=0.01,
            ) is True
            assert orders.capacity == 8.0
            for _ in range(7):
                assert cg.acquire_outbound_token(
                    "ig",
                    method="POST",
                    path="/positions/otc",
                    category="orders",
                    max_wait_sec=0.01,
                ) is True
            assert cg.acquire_outbound_token(
                "ig",
                method="POST",
                path="/positions/otc",
                category="orders",
                max_wait_sec=0.01,
            ) is False


def test_demo_throughput_bypass_respects_disabled_flag():
    cg.reset_chaos_guardian_for_tests()
    orders = cg._buckets["ig_orders"]
    orders.tokens = 0.0
    orders.refill_rate = 0.0
    with patch("system.demo_execution_plane.demo_throughput_active", return_value=True):
        with patch(
            "system.config_loader.get_config",
            return_value={"demo_throughput_mode": {"bypass_traffic_governor": False}},
        ):
            ok = cg.acquire_outbound_token(
                "ig",
                method="POST",
                path="/positions/otc",
                category="orders",
                max_wait_sec=0.01,
            )
            assert ok is False


def test_reconnect_backoff_capped_at_30s():
    delays = [cg.compute_reconnect_delay(i) for i in range(10)]
    assert all(d <= cg._BACKOFF_MAX_SEC for d in delays)
    assert delays[0] >= cg._BACKOFF_BASE_SEC
    assert delays[-1] <= cg._BACKOFF_MAX_SEC


def test_channel_disconnect_schedules_backoff():
    cg.notify_channel_disconnected("ig_stream", reason="test_drop")
    delayed, wait = cg.should_delay_reconnect("ig_stream")
    assert delayed is True
    assert wait > 0
    snap = cg.get_guardian_status_snapshot()
    assert any(h.get("channel") == "ig_stream" for h in snap.get("reconnection_history", []))


def test_state_reconcile_skips_broker_without_anomaly(monkeypatch):
    queried = {"n": 0}

    def _fake_reconcile(**kwargs):
        queried["n"] += 1
        return {"drift_count": 0}

    monkeypatch.setattr(
        "system.broker_reconciliation_daemon.run_reconciliation_once",
        _fake_reconcile,
    )
    result = cg.run_state_reconcile_tick(rest=MagicMock())
    assert result["broker_queried"] is False
    assert queried["n"] == 0


def test_state_reconcile_emergency_flatten_on_drift(monkeypatch):
    rest = MagicMock()
    rest.open_positions.return_value = [
        {
            "position": {"dealId": "D1", "direction": "BUY", "size": 1.0},
            "market": {"epic": "IX.D.DOW.IFM.IP"},
        }
    ]

    monkeypatch.setattr(cg, "_local_anomaly_flags", lambda: ["reconcile_cache_drift:3"])
    monkeypatch.setattr(
        "system.broker_reconciliation_daemon.run_reconciliation_once",
        lambda **kw: {
            "drift_count": 3,
            "broker_positions": 3,
            "internal_positions": 0,
            "last_drift_reason": "broker=3_internal=0",
        },
    )
    monkeypatch.setattr(cg, "acquire_outbound_token", lambda *a, **k: True)

    result = cg.run_state_reconcile_tick(rest=rest)
    assert result["broker_queried"] is True
    assert result["drift_verified"] is True
    assert rest.close_position.called
    assert result["emergency_flatten"]["ok"] is True


def test_confirm_poll_uses_ig_confirms_not_ig_orders(monkeypatch):
    """GET /confirms must not drain the ig_orders lane used by POST /positions/otc."""
    cg.reset_chaos_guardian_for_tests()
    orders = cg.TokenBucket("ig_orders", capacity=1.0, refill_rate=0.0)
    confirms = cg.TokenBucket("ig_confirms", capacity=1.0, refill_rate=0.0)
    monkeypatch.setitem(cg._buckets, "ig_orders", orders)
    monkeypatch.setitem(cg._buckets, "ig_confirms", confirms)
    monkeypatch.setattr(cg, "_demo_chaos_guardian_order_bypass", lambda: False)
    assert orders.try_acquire(1.0) is True
    assert cg.acquire_outbound_token(
        "ig",
        method="GET",
        path="/confirms/DEALREF123",
        category="orders",
        max_wait_sec=0.01,
    ) is True
    assert orders.tokens == 0.0
    assert confirms.tokens == 0.75


def test_demo_confirm_poll_bypasses_chaos_guardian(monkeypatch):
    cg.reset_chaos_guardian_for_tests()
    confirms = cg.TokenBucket("ig_confirms", capacity=0.0, refill_rate=0.0)
    monkeypatch.setitem(cg._buckets, "ig_confirms", confirms)
    monkeypatch.setattr(cg, "_demo_chaos_guardian_order_bypass", lambda: True)
    assert cg.acquire_outbound_token(
        "ig",
        method="GET",
        path="/confirms/DEALREF123",
        category="orders",
        max_wait_sec=0.01,
    ) is True


def test_post_positions_routes_to_ig_orders_bucket(monkeypatch):
    """POST /positions/otc categorized as positions must use ig_orders, not ig_ledger."""
    orders = cg.TokenBucket("ig_orders", capacity=1.0, refill_rate=0.0)
    ledger = cg.TokenBucket("ig_ledger", capacity=9.0, refill_rate=0.0)
    monkeypatch.setitem(cg._buckets, "ig_orders", orders)
    monkeypatch.setitem(cg._buckets, "ig_ledger", ledger)
    monkeypatch.setattr(cg, "_demo_chaos_guardian_order_bypass", lambda: False)
    assert orders.try_acquire(1.0) is True
    assert cg.acquire_outbound_token(
        "ig",
        method="POST",
        path="/positions/otc",
        category="positions",
        max_wait_sec=0.01,
    ) is False
    assert ledger.tokens >= 8.0


def test_demo_throughput_bypasses_post_positions_ledger_bucket(monkeypatch):
    """Demo soak raises ig_orders capacity so parallel POST /positions/otc can proceed."""
    monkeypatch.setattr(cg, "_demo_chaos_guardian_order_bypass", lambda: True)
    cg._demo_buckets_applied = False
    bucket = cg.TokenBucket("ig_orders", capacity=1.0, refill_rate=0.0)
    monkeypatch.setitem(cg._buckets, "ig_orders", bucket)
    assert cg.acquire_outbound_token(
        "ig",
        method="POST",
        path="/positions/otc",
        category="positions",
        max_wait_sec=0.01,
    ) is True
    assert bucket.capacity == 8.0
    bucket.tokens = 8.0
    assert cg.acquire_outbound_token(
        "ig",
        method="POST",
        path="/positions/otc",
        category="positions",
        max_wait_sec=0.01,
    ) is True


def test_malformed_json_rejected():
    ok, reason = pv.validate_json_frame(b"{not-json")
    assert ok is False
    assert reason == "malformed_json"


def test_corrupt_packet_burst_triggers_circuit_breaker():
    pv.reset_packet_validator_for_tests()
    for i in range(25):
        pv.validate_quote_packet_fast(epic="E", bid=100.0 + i * 0.01, offer=100.5 + i * 0.01)
    for _ in range(25):
        code = pv.validate_quote_packet_fast(epic="E", bid=100.0, offer=99.0)
        pv.reject_packet_code(code)
    health = pv.get_packet_sanitizer_health()
    assert health["circuit_breaker_active"] is True
    assert pv.validate_quote_packet_fast(epic="E", bid=100.0, offer=100.5) == pv.REASON_CIRCUIT_BREAKER


def test_out_of_order_packet_rejected():
    pv.reset_packet_validator_for_tests()
    assert pv.validate_quote_packet_fast(epic="X", bid=100.0, offer=100.5) == pv.REASON_OK
    code = pv.validate_quote_packet_fast(epic="X", bid=200.0, offer=200.5)
    assert code == pv.REASON_OUT_OF_ORDER


def test_guardian_status_snapshot_shape():
    cg.notify_channel_connected("yahoo_feed")
    cg._refresh_snapshot()
    snap = cg.get_guardian_status_snapshot()
    assert "token_buckets" in snap
    assert "connections" in snap
    assert "reconnection_history" in snap
    assert "packet_sanitization" in snap
    assert "state_sync_discrepancies" in snap


def test_valid_json_frame_passes():
    ok, reason = pv.validate_json_frame(json.dumps({"bid": 1.0, "offer": 1.1}))
    assert ok is True
    assert reason == "ok"
