"""Tests for Trading Desk liveness failsafe and recovery."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from runtime.trading_desk_liveness import (
    evaluate_liveness,
    reset_trading_desk_liveness_for_tests,
    run_recovery_tick,
)


def setup_function():
    reset_trading_desk_liveness_for_tests()


def teardown_function():
    reset_trading_desk_liveness_for_tests()


@patch("api.positions_live.build_live_positions_payload")
@patch("runtime.open_position_manager.snapshot")
@patch("runtime.agent_bootstrap.get_ig_position_sync")
def test_flat_book_softens_positions_snapshot_stale(
    mock_get_sync, mock_mgr_snap, mock_live
):
    """Broker-flat SoT must not red-line liveness on false snapshot stale."""
    mock_get_sync.return_value = None
    mock_mgr_snap.return_value = {
        "active": True,
        "last_tick_at": time.time(),
        "last_error": "",
    }
    mock_live.return_value = {
        "count": 0,
        "unmonitored": 0,
        "verdict": "FLAT",
        "stale": True,
        "critical": False,
        "trade_support": {"broker_open": 0, "actions_failed": 0},
    }

    liv = evaluate_liveness()

    assert liv["ok"] is True
    assert liv["has_open_risk"] is False
    assert "positions_snapshot_stale" not in liv["issues"]
    assert "ig_sync_missing" not in liv["issues"]


@patch("api.positions_live.build_live_positions_payload")
@patch("runtime.open_position_manager.snapshot")
@patch("runtime.agent_bootstrap.get_ig_position_sync")
def test_open_book_keeps_positions_snapshot_stale(
    mock_get_sync, mock_mgr_snap, mock_live
):
    sync = MagicMock()
    sync.snapshot.return_value = MagicMock(
        sync_status="ok", total_open=1, last_error="", rate_limit_paused=False
    )
    sync.is_fresh.return_value = True
    sync._last_sync_ts = time.time()
    mock_get_sync.return_value = sync
    mock_mgr_snap.return_value = {
        "active": True,
        "last_tick_at": time.time(),
        "last_error": "",
    }
    mock_live.return_value = {
        "count": 1,
        "unmonitored": 0,
        "verdict": "DEGRADED",
        "stale": True,
        "critical": False,
        "trade_support": {"broker_open": 1, "actions_failed": 0},
    }

    liv = evaluate_liveness()

    assert liv["ok"] is False
    assert liv["has_open_risk"] is True
    assert "positions_snapshot_stale" in liv["issues"]


@patch("api.positions_live.build_live_positions_payload")
@patch("runtime.open_position_manager.snapshot")
@patch("runtime.agent_bootstrap.get_ig_position_sync")
def test_flat_book_softens_ig_sync_missing_and_tick_timeout(
    mock_get_sync, mock_mgr_snap, mock_live
):
    """Flat broker book must not stay degraded on sync-missing + OPM timeout."""
    mock_get_sync.return_value = None
    mock_mgr_snap.return_value = {
        "active": True,
        "last_tick_at": time.time(),
        "last_error": "tick_timeout",
    }
    mock_live.return_value = {
        "count": 0,
        "unmonitored": 0,
        "verdict": "FLAT",
        "stale": False,
        "critical": False,
        "trade_support": {"broker_open": 0, "actions_failed": 0},
    }

    liv = evaluate_liveness()

    assert liv["ok"] is True
    assert liv["has_open_risk"] is False
    assert "ig_sync_missing" not in liv["issues"]
    assert "position_manager_error" not in liv["issues"]


@patch("api.positions_live.build_live_positions_payload")
@patch("runtime.open_position_manager.snapshot")
@patch("runtime.agent_bootstrap.get_ig_position_sync")
def test_open_book_keeps_tick_timeout_as_manager_error(
    mock_get_sync, mock_mgr_snap, mock_live
):
    """Open risk + OPM tick_timeout must stay sticky for REST recovery."""
    mock_get_sync.return_value = None
    mock_mgr_snap.return_value = {
        "active": True,
        "last_tick_at": time.time(),
        "last_error": "tick_timeout",
    }
    mock_live.return_value = {
        "count": 1,
        "unmonitored": 0,
        "verdict": "HEALTHY",
        "stale": False,
        "critical": False,
        "trade_support": {"broker_open": 1, "actions_failed": 0},
    }

    liv = evaluate_liveness()

    assert liv["has_open_risk"] is True
    assert "position_manager_error" in liv["issues"]
    assert liv["ok"] is False


@patch("runtime.open_position_manager.run_management_tick")
@patch("api.positions_live.build_live_positions_payload")
@patch("runtime.open_position_manager.snapshot")
@patch("runtime.agent_bootstrap.get_ig_position_sync")
def test_flat_recovery_skips_position_manager_rest_storm(
    mock_get_sync, mock_mgr_snap, mock_live, mock_tick
):
    mock_get_sync.return_value = None
    mock_mgr_snap.return_value = {
        "active": True,
        "last_tick_at": 0,
        "last_error": "tick_timeout",
    }
    mock_live.return_value = {
        "count": 0,
        "unmonitored": 0,
        "verdict": "FLAT",
        "stale": False,
        "trade_support": {"broker_open": 0},
    }

    with patch(
        "runtime.active_lifecycle_trades.reconcile_active_lifecycle_trades",
        return_value={"closed_registry": 0},
    ):
        result = run_recovery_tick(force=True)

    mock_tick.assert_not_called()
    assert "position_manager_tick" not in (result.get("actions") or [])


@patch("api.positions_live.build_live_positions_payload")
@patch("runtime.open_position_manager.snapshot")
@patch("runtime.agent_bootstrap.get_ig_position_sync")
def test_evaluate_liveness_healthy(mock_get_sync, mock_mgr_snap, mock_live):
    sync = MagicMock()
    sync.snapshot.return_value = MagicMock(
        sync_status="ok", total_open=1, last_error="", rate_limit_paused=False
    )
    sync.is_fresh.return_value = True
    sync._last_sync_ts = time.time()
    mock_get_sync.return_value = sync
    mock_mgr_snap.return_value = {
        "active": True,
        "last_tick_at": time.time(),
        "last_error": "",
    }
    mock_live.return_value = {
        "count": 1,
        "unmonitored": 0,
        "verdict": "HEALTHY",
        "stale": False,
    }

    liv = evaluate_liveness()

    assert liv["ok"] is True
    assert liv["connections"]["ig_sync"] is True
    assert liv["connections"]["position_manager"] is True
    assert liv["open_count"] == 1


@patch("runtime.micro_gbp_exit.snapshot")
@patch("api.positions_live.build_live_positions_payload")
@patch("runtime.open_position_manager.snapshot")
@patch("runtime.agent_bootstrap.get_ig_position_sync")
def test_open_count_follows_live_when_sync_empty(
    mock_get_sync, mock_mgr_snap, mock_live, mock_gbp
):
    sync = MagicMock()
    sync.snapshot.return_value = MagicMock(
        sync_status="idle", total_open=0, last_error="", rate_limit_paused=False
    )
    sync.is_fresh.return_value = False
    sync._last_sync_ts = 0.0
    mock_get_sync.return_value = sync
    mock_mgr_snap.return_value = {
        "active": False,
        "last_tick_at": 0,
        "last_error": "tick_timeout",
    }
    mock_live.return_value = {
        "count": 1,
        "unmonitored": 0,
        "verdict": "CRITICAL",
        "stale": True,
        "critical": True,
        "critical_alarms": ["flatten_failed:D9:EDITS_ONLY"],
        "trade_support": {
            "broker_open": 1,
            "actions_failed": 1,
            "last_flatten_error": "EDITS_ONLY",
        },
    }
    mock_gbp.return_value = {"tracks": {"D9": {}}}

    liv = evaluate_liveness()

    assert liv["ok"] is False
    assert liv["open_count"] == 1
    assert liv["has_open_risk"] is True
    assert "positions_critical" in liv["issues"]
    assert any("flatten_failed" in i for i in liv["issues"])


@patch("api.positions_live.build_live_positions_payload")
@patch("runtime.open_position_manager.snapshot")
@patch("runtime.agent_bootstrap.get_ig_position_sync")
def test_evaluate_detects_stale_sync(mock_get_sync, mock_mgr_snap, mock_live):
    sync = MagicMock()
    sync.snapshot.return_value = MagicMock(
        sync_status="idle", total_open=2, last_error="", rate_limit_paused=False
    )
    sync.is_fresh.return_value = False
    sync._last_sync_ts = 0.0
    mock_get_sync.return_value = sync
    mock_mgr_snap.return_value = {"active": True, "last_tick_at": 0, "last_error": ""}
    mock_live.return_value = {
        "count": 2,
        "unmonitored": 1,
        "verdict": "DEGRADED",
        "stale": True,
    }

    liv = evaluate_liveness()

    assert liv["ok"] is False
    assert "ig_sync_stale" in liv["issues"]
    assert any("unmonitored" in i for i in liv["issues"])


@patch("runtime.open_position_manager.run_management_tick")
@patch("execution.position_risk_stack.reconcile_open_positions_risk_stack")
@patch("runtime.agent_bootstrap.get_ig_position_sync")
@patch("api.positions_live.build_live_positions_payload")
@patch("runtime.open_position_manager.snapshot")
def test_recovery_nudges_sync_and_manager(
    mock_mgr_snap,
    mock_live,
    mock_get_sync,
    mock_reconcile,
    mock_tick,
):
    sync = MagicMock()
    sync.is_fresh.return_value = False
    sync._last_sync_ts = 0.0
    sync.snapshot.return_value = MagicMock(
        sync_status="error", total_open=1, last_error="timeout", rate_limit_paused=False
    )
    mock_get_sync.return_value = sync
    mock_mgr_snap.return_value = {"active": True, "last_tick_at": 0, "last_error": ""}
    mock_live.return_value = {
        "count": 1,
        "unmonitored": 1,
        "verdict": "DEGRADED",
        "stale": True,
    }
    mock_reconcile.return_value = {"armed": 1, "gbp": 1}
    mock_tick.return_value = {"ok": True}

    with patch("system.credentials_loader.try_load_credentials") as mock_cred:
        mock_cred.return_value = MagicMock(ok=True, credentials=MagicMock())
        with patch("system.ig_rest_session.get_shared_rest_client", return_value=MagicMock()):
            result = run_recovery_tick(force=True)

    assert "ig_sync_refresh" in result["actions"]
    assert any("position_manager_tick" in a for a in result["actions"])
    sync.request_refresh.assert_called_once()


@patch("runtime.open_position_manager.run_management_tick", return_value={"ok": True})
@patch("api.positions_live.build_live_positions_payload")
@patch("runtime.open_position_manager.snapshot")
@patch("runtime.agent_bootstrap.get_ig_position_sync")
def test_recovery_cooldown_skips_rapid_retries(
    mock_get_sync, mock_mgr_snap, mock_live, _mock_tick
):
    sync = MagicMock()
    sync.is_fresh.return_value = False
    sync._last_sync_ts = 0.0
    sync.snapshot.return_value = MagicMock(
        sync_status="idle", total_open=1, last_error="timeout", rate_limit_paused=False
    )
    mock_get_sync.return_value = sync
    mock_mgr_snap.return_value = {"active": True, "last_tick_at": 0, "last_error": ""}
    mock_live.return_value = {
        "count": 1,
        "unmonitored": 0,
        "verdict": "DEGRADED",
        "stale": True,
        "trade_support": {"broker_open": 1},
    }

    with patch("system.credentials_loader.try_load_credentials") as mock_cred:
        mock_cred.return_value = MagicMock(ok=True, credentials=MagicMock())
        with patch(
            "system.ig_rest_session.get_shared_rest_client", return_value=MagicMock()
        ):
            first = run_recovery_tick(force=True)
            second = run_recovery_tick(force=False)

    assert first.get("actions")
    assert second.get("skipped") is True
