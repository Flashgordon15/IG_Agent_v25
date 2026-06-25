"""v31 telemetry API — hub quotes, positions, history, health risk fields."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from analytics.triage_db import connect_triage_sqlite
from api.v31_telemetry import (
    build_v31_history,
    build_v31_positions,
    build_v31_telemetry,
    resolve_risk_tracking_fields,
)

# Static fixture — never hit live GET /accounts or broker gateway during tests.
STATIC_ACCOUNT_FIXTURE: dict[str, float | None] = {
    "account_balance_gbp": 10000.0,
    "account_available_gbp": 9850.0,
    "account_profit_loss_gbp": 12.5,
}

STATIC_RTT_MS = 42.0


@pytest.fixture(autouse=True)
def _mock_live_broker_network_paths():
    """Block live account refresh + broker RTT probes for entire module."""
    with patch(
        "api.v31_telemetry.resolve_account_capital_fields",
        return_value=dict(STATIC_ACCOUNT_FIXTURE),
    ), patch(
        "api.v31_telemetry._probe_broker_network_rtt_ms",
        return_value=STATIC_RTT_MS,
    ), patch(
        "api.v31_telemetry._warm_stack_from_rest_fallback",
    ), patch(
        "api.v31_telemetry._resolve_rest_for_telemetry",
        return_value=None,
    ):
        yield


def test_build_v31_telemetry_maps_night_matrix():
    snap = MagicMock()
    snap.bid = 100.0
    snap.offer = 100.5
    snap.age_seconds.return_value = 2.0
    snap.source = "hub"

    hub = MagicMock()
    hub.get_snapshot.return_value = snap

    with patch("api.v31_telemetry.get_market_data_hub", return_value=hub):
        with patch("api.v31_telemetry.NIGHT_MATRIX_EPICS", ["CS.D.TEST.IP"]):
            payload = build_v31_telemetry()

    assert payload["ok"] is True
    assert payload["asset_count"] == 1
    assert payload["fresh_count"] == 1
    asset = payload["assets"]["CS.D.TEST.IP"]
    assert asset["mid"] == 100.25
    assert asset["fresh"] is True
    assert payload["account_balance_gbp"] == STATIC_ACCOUNT_FIXTURE["account_balance_gbp"]
    assert payload["broker_network_rtt_ms"] == STATIC_RTT_MS


def test_build_v31_positions_empty_when_db_missing():
    with patch("api.v31_telemetry._triage_v31_path", return_value=Path("/nonexistent/triage_v31.db")):
        payload = build_v31_positions()
    assert payload["sync_status"] == "triage_sql_live"
    assert payload["total_open"] == 0


def test_build_v31_positions_from_triage_sql():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "triage_v31.db"
        conn = connect_triage_sqlite(path)
        try:
            conn.executescript(
                """
                CREATE TABLE active_lifecycle_trades (
                    deal_id TEXT PRIMARY KEY,
                    trade_id INTEGER,
                    epic TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    size REAL NOT NULL DEFAULT 0,
                    lifecycle_state TEXT NOT NULL,
                    broker_level REAL,
                    broker_stop REAL,
                    broker_limit REAL,
                    broker_upl REAL,
                    last_broker_sync_at TEXT NOT NULL,
                    last_event TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT ''
                );
                INSERT INTO active_lifecycle_trades (
                    deal_id, epic, direction, size, lifecycle_state,
                    broker_level, broker_upl, last_broker_sync_at
                ) VALUES (
                    'D1', 'CS.D.EURUSD.CFD.IP', 'BUY', 1.0, 'AGENT_MANAGED',
                    1.2345, 4.5, '2026-06-25T12:00:00Z'
                );
                """
            )
            conn.commit()
        finally:
            conn.close()

        with patch("api.v31_telemetry._triage_v31_path", return_value=path):
            payload = build_v31_positions()

    assert payload["total_open"] == 1
    assert "D1" in payload["positions"]
    assert payload["positions"]["D1"]["epic"] == "CS.D.EURUSD.CFD.IP"


def test_build_v31_history_reads_closed_rows():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "triage_v31.db"
        conn = connect_triage_sqlite(path)
        try:
            conn.executescript(
                """
                CREATE TABLE closed_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    size REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    direction TEXT NOT NULL,
                    gross_pnl REAL NOT NULL,
                    net_pnl REAL NOT NULL,
                    exit_timestamp TEXT NOT NULL,
                    epic TEXT,
                    result TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                INSERT INTO closed_positions (
                    ticket, asset, size, entry_price, exit_price, direction,
                    gross_pnl, net_pnl, exit_timestamp, epic, result
                ) VALUES (
                    'T1', 'GOLD', 1.0, 100.0, 101.0, 'BUY',
                    10.0, 9.5, '2026-06-25T10:00:00Z', 'CS.D.CFPGOLD.CFP.IP', 'WIN'
                );
                """
            )
            conn.commit()
        finally:
            conn.close()

        with patch("api.v31_telemetry._triage_v31_path", return_value=path):
            payload = build_v31_history(limit=10)

    assert payload["ok"] is True
    assert payload["count"] == 1
    assert payload["rows"][0]["result"] == "WIN"
    assert payload["rows"][0]["pnl_gbp"] == 9.5


def test_resolve_risk_tracking_fields_includes_boot_gate():
    with patch("api.v31_telemetry.resolve_active_boot_gate", return_value=3):
        with patch("api.v31_telemetry.resolve_pacing_interval_sec", return_value=5.0):
            with patch("api.v31_telemetry.resolve_block_reason", return_value="api_trading_paused"):
                fields = resolve_risk_tracking_fields()
    assert fields["boot_gate"] == 3
    assert fields["pacing_interval_sec"] == 5.0
    assert fields["block_reason"] == "api_trading_paused"


def test_gate_health_response_merges_risk_fields():
    from api.gate_health_matrix import build_gate_health_response

    with patch("api.gate_health_matrix.resolve_gate_health_matrix", return_value=(503, {"status": "HYDRATING"})):
        with patch("api.v31_telemetry.resolve_risk_tracking_fields", return_value={
            "boot_gate": 3,
            "pacing_interval_sec": 0.2,
            "block_reason": "",
        }):
            code, body = build_gate_health_response()
    assert code == 503
    assert body["boot_gate"] == 3
    assert "pacing_interval_sec" in body


def test_build_v31_telemetry_includes_failover_fields():
    snap = MagicMock()
    snap.bid = 100.0
    snap.offer = 100.5
    snap.age_seconds.return_value = 2.0
    snap.source = "hub"

    hub = MagicMock()
    hub.get_snapshot.return_value = snap

    with patch("api.v31_telemetry.get_market_data_hub", return_value=hub):
        with patch("api.v31_telemetry.NIGHT_MATRIX_EPICS", ["CS.D.TEST.IP"]):
            with patch("runtime.dual_core_execution.get_failover_state", return_value={
                "failover_active": False,
                "failover_state": "NORMAL",
            }):
                payload = build_v31_telemetry()

    assert "failover_active" in payload
    assert "ml_alpha_weight" in payload
    assert "broker_network_rtt_ms" in payload
    assert payload["broker_network_rtt_ms"] == STATIC_RTT_MS


def test_broker_ready_payload_shape():
    from api.v31_broker_ready import build_broker_ready_payload

    with patch("api.v31_broker_ready._resolve_rest_client", return_value=None):
        with patch("api.v31_broker_ready._socket_stream_active", return_value=(False, "warming")):
            with patch("api.v31_broker_ready._order_execution_ready", return_value=(True, "ok")):
                with patch("api.v31_broker_ready._ledger_synced", return_value=(True, "synced")):
                    payload = build_broker_ready_payload()

    assert payload["ok"] is True
    assert "broker_auth_valid" in payload
    assert payload["display"]["data_stream"] == "WARMING"
    assert payload["display"]["order_valve"] == "READY"
