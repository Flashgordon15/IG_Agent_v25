"""Trade pipeline health model and /api/gui_status extension tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from api.gui_status import build_gui_status
from runtime.app_mode import reset_app_mode_for_tests
from runtime.pipeline_health import (
    EpicPipelineHealth,
    MlAppetite,
    OrderSize,
    PipelineState,
    TrailingGuards,
    build_api_feed_health,
    build_market_rotation_status,
    build_trade_pipeline_health,
    derive_pipeline_state,
    observe_pipeline_for_test,
    reset_pipeline_health_for_tests,
)
from runtime.session_lock import (
    TESTBED_ACCOUNT_SCOPE,
    lock_path_for_scope,
    reset_session_lock_state_for_tests,
    write_session_lock,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    reset_app_mode_for_tests()
    reset_session_lock_state_for_tests()
    reset_pipeline_health_for_tests()
    for key in (
        "APP_MODE",
        "IG_ACCOUNT_SCOPE",
        "IG_DATA_ROOT",
        "IG_API_PORT",
        "IG_AGENT_CONFIG",
        "IG_MOCK_FEED",
        "IG_TRIAGE_DB",
    ):
        monkeypatch.delenv(key, raising=False)


def test_derive_pipeline_state_transitions():
    epic = "CS.D.EURUSD.CFD.IP"
    record = observe_pipeline_for_test(
        epic,
        signal_ingested=True,
        signal_timestamp="2026-06-25T10:00:00Z",
    )
    assert derive_pipeline_state(record) == PipelineState.SIGNAL_ONLY

    record = observe_pipeline_for_test(
        epic,
        signal_ingested=True,
        order_dispatched=True,
        order_dispatched_timestamp="2026-06-25T10:00:01Z",
    )
    assert derive_pipeline_state(record) == PipelineState.ORDER_PENDING

    record = observe_pipeline_for_test(
        epic,
        order_confirmed=True,
        live_tracking=True,
        unrealised_pnl=0.0,
    )
    assert derive_pipeline_state(record) == PipelineState.LIVE

    record = observe_pipeline_for_test(epic, live_tracking=True, unrealised_pnl=12.5)
    assert derive_pipeline_state(record) == PipelineState.IN_PROFIT

    record = observe_pipeline_for_test(epic, live_tracking=True, unrealised_pnl=-3.2)
    assert derive_pipeline_state(record) == PipelineState.IN_LOSS

    record = observe_pipeline_for_test(epic, closed=True, close_reason="STOP")
    assert derive_pipeline_state(record) == PipelineState.CLOSED

    record = observe_pipeline_for_test(
        epic,
        closed=True,
        reconciled=True,
        ledger_entry_id="DEAL123",
    )
    assert derive_pipeline_state(record) == PipelineState.RECONCILED


def test_full_mocked_hook_progression():
    epic = "IX.D.DOW.IFM.IP"
    steps = [
        ({"signal_ingested": True}, PipelineState.SIGNAL_ONLY),
        ({"order_prepared": True, "order_size": OrderSize(stake=1.0, stop=100.0, limit=110.0)}, PipelineState.SIGNAL_ONLY),
        ({"order_dispatched": True, "broker_epic": epic}, PipelineState.ORDER_PENDING),
        ({"order_confirmed": True, "ig_order_id": "IG-001", "fill_price": 42000.0}, PipelineState.IDLE),
        ({"live_tracking": True, "unrealised_pnl": 2.0}, PipelineState.IN_PROFIT),
        ({"closed": True, "close_reason": "LIMIT"}, PipelineState.CLOSED),
        ({"reconciled": True, "ledger_entry_id": "LEDGER-9"}, PipelineState.RECONCILED),
    ]
    cumulative: dict = {}
    for patch, expected in steps:
        cumulative.update(patch)
        record = observe_pipeline_for_test(epic, **cumulative)
        assert derive_pipeline_state(record) == expected


def test_api_feed_health_includes_latency_and_ranking(monkeypatch):
    monkeypatch.setenv("IG_MOCK_FEED", "1")
    feed = build_api_feed_health()
    assert "feeds" in feed
    assert "ranking" in feed
    assert set(feed["feeds"].keys()) == {"feed1", "feed2", "feed3"}
    for meta in feed["feeds"].values():
        assert "status" in meta
        assert "latency_ms" in meta
        assert "last_update_timestamp" in meta
    ranking = feed["ranking"]
    assert ranking["primary_feed"] in ("feed1", "feed2", "feed3")
    assert "secondary_feed" in ranking
    assert "tertiary_feed" in ranking


def test_market_rotation_status_well_formed():
    status = build_market_rotation_status()
    assert isinstance(status["active_markets"], list)
    assert isinstance(status["candidate_markets"], list)
    assert status["rotation_state"] in ("IDLE", "EVALUATING", "ROTATING")
    assert "last_rotation_timestamp" in status


def test_gui_status_includes_trade_pipeline_health(tmp_path, monkeypatch):
    scope = "ig:PIPE1"
    data_root = tmp_path / "production"
    data_root.mkdir()
    monkeypatch.setenv("APP_MODE", "DEMO")
    monkeypatch.setenv("IG_ACCOUNT_SCOPE", scope)
    monkeypatch.setenv("IG_DATA_ROOT", str(data_root))
    monkeypatch.setenv("IG_AGENT_CONFIG", "config/config_v31.json")
    monkeypatch.setenv("IG_API_PORT", "8080")
    reset_app_mode_for_tests()

    lock = lock_path_for_scope(scope, data_root)
    write_session_lock(lock, pid=os.getpid(), port=8080, account_scope=scope, started_at=1_700_000_000)

    status = build_gui_status()
    assert "trade_pipeline_health" in status
    assert isinstance(status["trade_pipeline_health"], list)
    assert "api_feed_health" in status
    assert "feeds" in status["api_feed_health"]
    assert "ranking" in status["api_feed_health"]
    assert status["account_scope"] == "ig:***"
    assert status["market_rotation_status"]["rotation_state"] == "IDLE"


def test_gui_status_testbed_fields(tmp_path, monkeypatch):
    testbed_root = tmp_path / "testbed"
    testbed_root.mkdir()
    monkeypatch.setenv("APP_MODE", "TESTBED")
    monkeypatch.setenv("IG_ACCOUNT_SCOPE", TESTBED_ACCOUNT_SCOPE)
    monkeypatch.setenv("IG_DATA_ROOT", str(testbed_root))
    monkeypatch.setenv("IG_AGENT_CONFIG", "config/config_v31_testbed.json")
    monkeypatch.setenv("IG_API_PORT", "9199")
    reset_app_mode_for_tests()

    lock = lock_path_for_scope(TESTBED_ACCOUNT_SCOPE, testbed_root)
    write_session_lock(
        lock,
        pid=os.getpid(),
        port=9199,
        account_scope=TESTBED_ACCOUNT_SCOPE,
    )

    status = build_gui_status()
    assert status["app_mode"] == "TESTBED"
    assert status["account_scope"] == TESTBED_ACCOUNT_SCOPE
    assert status["data_root"] == str(testbed_root)
    assert "trade_pipeline_health" in status


def test_trade_pipeline_health_from_triage_db(tmp_path, monkeypatch):
    db = tmp_path / "triage_test.db"
    monkeypatch.setenv("IG_TRIAGE_DB", str(db))

    from analytics.triage_db import connect_triage_sqlite

    conn = connect_triage_sqlite(db)
    conn.executescript(
        """
        CREATE TABLE production_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deal_reference TEXT NOT NULL UNIQUE,
            deal_id TEXT,
            epic TEXT NOT NULL,
            direction TEXT NOT NULL,
            size REAL NOT NULL,
            status TEXT NOT NULL,
            broker_payload TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
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
        """
    )
    conn.execute(
        """
        INSERT INTO production_orders
            (deal_reference, deal_id, epic, direction, size, status, created_at)
        VALUES ('REF1', 'DEAL1', 'CS.D.EURUSD.CFD.IP', 'BUY', 1.0, 'CONFIRMED', datetime('now'))
        """
    )
    conn.execute(
        """
        INSERT INTO active_lifecycle_trades
            (deal_id, epic, direction, size, lifecycle_state, broker_level, broker_upl,
             last_broker_sync_at, broker_stop, broker_limit)
        VALUES ('DEAL1', 'CS.D.EURUSD.CFD.IP', 'BUY', 1.0, 'AGENT_MANAGED', 1.10, 0.5,
                datetime('now'), 1.05, 1.15)
        """
    )
    conn.commit()
    conn.close()

    rows = build_trade_pipeline_health(lookback_hours=24)
    assert len(rows) >= 1
    match = next(r for r in rows if r["epic"] == "CS.D.EURUSD.CFD.IP")
    assert match["order_confirmed"] is True
    assert match["live_tracking"] is True
    assert match["ig_order_id"] == "DEAL1"
    assert match["pipeline_state"] in ("LIVE", "IN_PROFIT")
    assert match["trailing_guards"]["active"] is True


def test_epic_pipeline_summary_structure():
    record = EpicPipelineHealth(
        epic="CS.D.CFPGOLD.CFP.IP",
        market_name="Gold",
        signal_ingested=True,
        ml_appetite=MlAppetite(appetite="STRONG", probability=0.71, reason="blend"),
        trailing_guards=TrailingGuards(active=True, last_stop=2300.0),
    )
    summary = record.to_summary_dict()
    assert summary["epic"] == "CS.D.CFPGOLD.CFP.IP"
    assert summary["ml_appetite"]["appetite"] == "STRONG"
    assert "pipeline_state" in summary
    assert "trailing_guards" in summary
