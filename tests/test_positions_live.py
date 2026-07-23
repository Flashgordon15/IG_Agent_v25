"""Tests for cache-only positions/live API — no blocking REST on hot path."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from api.positions_live import build_live_positions_payload
from runtime.ig_position_sync import IgSyncSnapshot, SyncedPosition


@pytest.fixture(autouse=True)
def _no_disk_broker_snapshot():
    with patch("runtime.broker_snapshot.read_snapshot", return_value=None), patch(
        "api.positions_live._read_trade_support_status", return_value=None
    ):
        yield


def _synced_position(deal_id: str = "D1") -> SyncedPosition:
    return SyncedPosition(
        deal_id=deal_id,
        epic="IX.D.NIKKEI.IFM.IP",
        direction="BUY",
        size=0.5,
        level=68000.0,
        upl=2.5,
        bid=68001.0,
        offer=68002.0,
        currency="JPY",
    )


def _mock_sync(*, positions: list[SyncedPosition], fresh: bool = True) -> MagicMock:
    sync = MagicMock()
    snap = IgSyncSnapshot(
        positions=positions,
        total_open=len(positions),
        sync_status="ok",
        last_sync_at="2026-07-07T08:00:00",
    )
    sync.snapshot.return_value = snap
    sync.is_fresh.return_value = fresh
    sync._last_sync_ts = 1_783_407_000.0
    return sync


@patch("runtime.micro_gbp_exit.snapshot", return_value={
    "tracks": {
        "D1": {
            "epic": "IX.D.NIKKEI.IFM.IP",
            "direction": "BUY",
            "size": 0.5,
            "entry_level": 68000.0,
            "loss_cap_gbp": 4.0,
            "soft_loss_gbp": 1.68,
            "trail_floor_gbp": 0.0,
            "peak_profit_gbp": 0.0,
            "target_profit_gbp": 8.5,
        }
    }
})
@patch("runtime.virtual_stop_loss.virtual_stop_snapshot", return_value={
    "positions": [{"deal_id": "D1", "ceiling_pts": 3.5}]
})
@patch("runtime.dynamic_limit_engine.snapshot", return_value={"tracks": {"D1": {"peak_profit_ig_pts": 0}}})
@patch("runtime.agent_bootstrap.get_ig_position_sync")
def test_positions_live_from_sync_cache(mock_get_sync, *_mocks):
    mock_get_sync.return_value = _mock_sync(positions=[_synced_position()])

    payload = build_live_positions_payload()

    assert payload["ok"] is True
    assert payload["count"] == 1
    assert payload["positions"][0]["source"] == "sync_cache"
    assert payload["verdict"] == "HEALTHY"
    ps = payload["positions"][0]["protection_summary"]
    assert ps["gbp_armed"] is True
    assert ps["virtual_armed"] is True
    assert ps["dynamic_armed"] is True
    assert ps["loss_cap_gbp"] == 4.0
    assert ps["soft_loss_gbp"] == 1.68
    assert ps["mode"] == "software_primary"
    mock_get_sync.return_value.sync_once.assert_not_called()


@patch("runtime.micro_gbp_exit.snapshot", return_value={"tracks": {}})
@patch("runtime.virtual_stop_loss.virtual_stop_snapshot", return_value={"positions": []})
@patch("runtime.dynamic_limit_engine.snapshot", return_value={"tracks": {}})
@patch("runtime.agent_bootstrap.get_ig_position_sync")
def test_stale_empty_sync_does_not_block_on_rest(mock_get_sync, *_mocks):
    sync = _mock_sync(positions=[], fresh=False)
    mock_get_sync.return_value = sync

    payload = build_live_positions_payload()

    assert payload["count"] == 0
    # Flat SoT softens stale so desk_support / liveness do not red-line.
    assert payload["stale"] is False
    assert payload["verdict"] == "FLAT"
    sync.request_refresh.assert_not_called()
    sync.sync_once.assert_not_called()


@patch("runtime.micro_gbp_exit.snapshot", return_value={"tracks": {"D9": {"epic": "IX.D.DOW.IFM.IP", "direction": "BUY", "size": 0.5, "entry_level": 53000}}})
@patch("runtime.virtual_stop_loss.virtual_stop_snapshot", return_value={"positions": []})
@patch("runtime.dynamic_limit_engine.snapshot", return_value={"tracks": {}})
@patch("runtime.agent_bootstrap.get_ig_position_sync")
def test_gbp_track_fallback_when_sync_empty(mock_get_sync, *_mocks):
    mock_get_sync.return_value = _mock_sync(positions=[], fresh=False)

    payload = build_live_positions_payload()

    assert payload["count"] == 1
    assert payload["positions"][0]["source"] == "gbp_track_fallback"
    assert payload["verdict"] == "DEGRADED"


@patch("runtime.micro_gbp_exit.snapshot", return_value={"tracks": {}})
@patch("runtime.virtual_stop_loss.virtual_stop_snapshot", return_value={"positions": []})
@patch("runtime.dynamic_limit_engine.snapshot", return_value={"tracks": {}})
@patch("runtime.agent_bootstrap.get_ig_position_sync")
def test_blocking_refresh_only_when_allowed(mock_get_sync, *_mocks):
    sync = _mock_sync(positions=[], fresh=False)
    refreshed = IgSyncSnapshot(
        positions=[_synced_position("D2")],
        total_open=1,
        sync_status="ok",
    )

    def _sync_once():
        sync.snapshot.return_value = refreshed
        sync.is_fresh.return_value = True
        return refreshed

    sync.sync_once.side_effect = _sync_once
    mock_get_sync.return_value = sync

    payload = build_live_positions_payload(allow_blocking_refresh=True)

    assert payload["count"] == 1
    sync.sync_once.assert_called_once()


@patch("runtime.micro_gbp_exit.snapshot", return_value={"tracks": {}})
@patch("runtime.virtual_stop_loss.virtual_stop_snapshot", return_value={"positions": []})
@patch("runtime.dynamic_limit_engine.snapshot", return_value={"tracks": {}})
@patch("runtime.agent_bootstrap.get_ig_position_sync", return_value=None)
def test_missing_sync_returns_flat_not_stale(_mock_get_sync, *_mocks):
    payload = build_live_positions_payload()

    assert payload["count"] == 0
    assert payload["verdict"] == "FLAT"
    assert payload["stale"] is False


@patch("runtime.micro_gbp_exit.snapshot", return_value={"tracks": {}})
@patch("runtime.virtual_stop_loss.virtual_stop_snapshot", return_value={"positions": []})
@patch("runtime.dynamic_limit_engine.snapshot", return_value={"tracks": {}})
@patch("runtime.agent_bootstrap.get_ig_position_sync")
def test_open_book_keeps_stale_flag(mock_get_sync, *_mocks):
    mock_get_sync.return_value = _mock_sync(positions=[_synced_position()], fresh=False)
    payload = build_live_positions_payload()
    assert payload["count"] == 1
    assert payload["stale"] is True
    assert payload["verdict"] == "DEGRADED"


@patch("runtime.micro_gbp_exit.snapshot", return_value={
    "tracks": {
        "D9": {
            "epic": "IX.D.DOW.IFM.IP",
            "direction": "BUY",
            "size": 0.5,
            "entry_level": 53000,
            "loss_cap_gbp": 4.0,
            "soft_loss_gbp": 2.2,
            "trail_floor_gbp": 0.0,
            "peak_profit_gbp": 0.0,
            "target_profit_gbp": 8.5,
        }
    }
})
@patch("runtime.virtual_stop_loss.virtual_stop_snapshot", return_value={
    "positions": [{"deal_id": "D9", "ceiling_pts": 3.4}]
})
@patch("runtime.dynamic_limit_engine.snapshot", return_value={"tracks": {"D9": {}}})
@patch("runtime.agent_bootstrap.get_ig_position_sync")
def test_trade_support_overlay_fills_pnl_and_critical(mock_get_sync, *_mocks):
    mock_get_sync.return_value = _mock_sync(positions=[], fresh=False)
    ts = {
        "ts": __import__("time").time(),
        "broker_open": 1,
        "valued": 1,
        "unvalued": 0,
        "total_unrealized_gbp": -122.35,
        "actions": [
            {
                "deal_id": "D9",
                "epic": "IX.D.DOW.IFM.IP",
                "pnl_gbp": -122.35,
                "action": "flatten",
                "reason": "soft_loss breach",
                "ok": False,
                "error": "IGOrderError: Market not tradeable (status=EDITS_ONLY)",
            }
        ],
    }
    ts_overlay = {
        **ts,
        "status_age_sec": 1.0,
        "running": True,
    }
    with patch("api.positions_live._read_trade_support_status", return_value=ts_overlay):
        payload = build_live_positions_payload()

    assert payload["count"] == 1
    assert payload["positions"][0]["pnl_gbp"] == -122.35
    assert payload["total_pnl_gbp"] == -122.35
    assert payload["verdict"] == "CRITICAL"
    assert payload["critical"] is True
    assert payload["ok"] is False
    assert payload["positions"][0]["flatten_failed"] is True
    assert payload["trade_support"]["actions_failed"] == 1
    assert "EDITS_ONLY" in (payload["trade_support"]["last_flatten_error"] or "")


@patch("runtime.micro_gbp_exit.snapshot", return_value={"tracks": {}})
@patch("runtime.virtual_stop_loss.virtual_stop_snapshot", return_value={"positions": []})
@patch("runtime.dynamic_limit_engine.snapshot", return_value={"tracks": {}})
@patch("runtime.agent_bootstrap.get_ig_position_sync")
def test_broker_snapshot_preferred_over_empty_sync(mock_get_sync, *_mocks):
    mock_get_sync.return_value = _mock_sync(positions=[], fresh=False)
    shared = {
        "source": "trade_support",
        "age_sec": 5.0,
        "positions": [
            {
                "deal_id": "DX",
                "epic": "IX.D.DOW.IFM.IP",
                "direction": "BUY",
                "size": 0.5,
                "entry": 40000.0,
                "pnl_gbp": -12.5,
                "stop_level": 39990.0,
                "limit_level": None,
            }
        ],
    }
    with patch("runtime.broker_snapshot.read_snapshot", return_value=shared), patch(
        "api.positions_live._read_trade_support_status", return_value=None
    ):
        payload = build_live_positions_payload()

    assert payload["count"] == 1
    assert payload["positions"][0]["source"].startswith("broker_snapshot")
    assert payload["positions"][0]["pnl_gbp"] == -12.5
    assert payload["total_pnl_gbp"] == -12.5
