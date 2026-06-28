"""Strategy profile metadata and strategy-aware governance tests."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from api.gui_status import build_gui_status
from runtime.app_mode import reset_app_mode_for_tests
from runtime.pipeline_governance import build_pipeline_governance
from runtime.pipeline_health import EpicPipelineHealth, MlAppetite, build_trade_pipeline_health
from runtime.session_lock import lock_path_for_scope, reset_session_lock_state_for_tests, write_session_lock
from runtime.strategy_profile import (
    StrategyDerivationHints,
    StrategyProfile,
    StrategySource,
    build_derivation_hints,
    derive_strategy_ownership,
    lifecycle_has_full_gate_chain,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    reset_app_mode_for_tests()
    reset_session_lock_state_for_tests()
    for key in ("APP_MODE", "IG_ACCOUNT_SCOPE", "IG_DATA_ROOT", "IG_TRIAGE_DB"):
        monkeypatch.delenv(key, raising=False)


def _ago_iso(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def test_scalp_tagging_production_orders_without_lifecycle_signal():
    record = EpicPipelineHealth(
        epic="CS.D.EURUSD.CFD.IP",
        order_confirmed=True,
        order_dispatched=True,
        signal_ingested=False,
    )
    hints = StrategyDerivationHints(
        has_production_orders=True,
        micro_deal_pattern=True,
        has_any_activity=True,
    )
    profile, source = derive_strategy_ownership(record, hints)
    assert profile is StrategyProfile.SCALP
    assert source is StrategySource.MICRO


def test_momentum_tagging_full_gate_chain_and_ml():
    record = EpicPipelineHealth(
        epic="IX.D.DOW.IFM.IP",
        signal_ingested=True,
        order_prepared=True,
        ml_appetite=MlAppetite(appetite="STRONG", probability=0.72, reason="blend"),
    )
    lifecycle = [
        {
            "stages": {
                "signal": {"status": "ok"},
                "validation": {"status": "ok"},
                "execution_request": {"status": "ok"},
            }
        }
    ]
    hints = build_derivation_hints(
        epic=record.epic,
        record=record,
        lifecycle_rows=lifecycle,
    )
    profile, source = derive_strategy_ownership(record, hints)
    assert profile is StrategyProfile.MOMENTUM
    assert source is StrategySource.PATH_A
    assert lifecycle_has_full_gate_chain(lifecycle) is True


def test_swing_tagging_extended_hold():
    record = EpicPipelineHealth(
        epic="CS.D.CFPGOLD.CFP.IP",
        signal_ingested=True,
        live_tracking=True,
        live_tracking_timestamp=_ago_iso(7200),
        ml_appetite=MlAppetite(appetite="WEAK", probability=0.55, reason="blend"),
    )
    hints = StrategyDerivationHints(lifecycle_full_chain=True, has_any_activity=True)
    profile, source = derive_strategy_ownership(record, hints)
    assert profile is StrategyProfile.SWING
    assert source is StrategySource.PATH_A


def test_rotation_tagging_active_stack_with_pierce():
    record = EpicPipelineHealth(epic="CS.D.EURUSD.CFD.IP")
    hints = StrategyDerivationHints(
        in_active_stack=True,
        z_score_pierce_active=True,
    )
    profile, source = derive_strategy_ownership(record, hints)
    assert profile is StrategyProfile.ROTATION
    assert source is StrategySource.PATH_B_HANDOFF


def test_rotation_handoff_to_scalp_when_micro_dispatched():
    record = EpicPipelineHealth(
        epic="CS.D.EURUSD.CFD.IP",
        order_dispatched=True,
        order_confirmed=True,
        signal_ingested=False,
    )
    hints = StrategyDerivationHints(
        in_active_stack=True,
        z_score_pierce_active=True,
        has_production_orders=True,
        has_any_activity=True,
    )
    profile, source = derive_strategy_ownership(record, hints)
    assert profile is StrategyProfile.SCALP
    assert source is StrategySource.MICRO


def test_unknown_tagging_no_activity():
    record = EpicPipelineHealth(epic="IX.D.NIKKEI.IFM.IP")
    hints = StrategyDerivationHints()
    profile, source = derive_strategy_ownership(record, hints)
    assert profile is StrategyProfile.UNKNOWN
    assert source is StrategySource.NONE


def test_governance_suppresses_ml_anomaly_for_scalp():
    row = {
        "epic": "CS.D.EURUSD.CFD.IP",
        "pipeline_state": "SIGNAL_ONLY",
        "active_strategy_profile": "SCALP",
        "strategy_source": "MICRO",
        "ml_appetite": {"appetite": "STRONG", "probability": 0.8, "reason": "x"},
        "signal_timestamp": _ago_iso(600),
        "order_prepared": False,
        "order_dispatched": False,
        "live_tracking": True,
        "trailing_guards": {"active": False},
    }
    gov = build_pipeline_governance(trade_pipeline_health=[row])["pipeline_governance"]["per_epic"][0]
    assert "ML_APPETITE_STRONG_BUT_NO_ORDER" not in gov["pipeline_anomalies"]
    assert "LIVE_WITHOUT_TRAILING_GUARDS" not in gov["pipeline_anomalies"]


def test_governance_rotation_skips_order_anomalies_without_dispatch():
    row = {
        "epic": "CS.D.EURUSD.CFD.IP",
        "pipeline_state": "IDLE",
        "active_strategy_profile": "ROTATION",
        "strategy_source": "PATH_B_HANDOFF",
        "signal_ingested": False,
        "order_dispatched": False,
        "ml_appetite": {"appetite": "STRONG", "probability": 0.9, "reason": "x"},
    }
    rotation = {"active_markets": ["CS.D.EURUSD.CFD.IP"], "candidate_markets": [], "rotation_state": "IDLE"}
    gov = build_pipeline_governance(
        trade_pipeline_health=[row],
        market_rotation_status=rotation,
    )["pipeline_governance"]["per_epic"][0]
    assert gov["pipeline_anomalies"] == []
    assert gov["feed_anomalies"] == []


def test_governance_path_a_fires_ml_anomaly():
    row = {
        "epic": "IX.D.DOW.IFM.IP",
        "pipeline_state": "SIGNAL_ONLY",
        "active_strategy_profile": "MOMENTUM",
        "strategy_source": "PATH_A",
        "signal_ingested": True,
        "signal_timestamp": _ago_iso(600),
        "ml_appetite": {"appetite": "STRONG", "probability": 0.85, "reason": "x"},
        "order_prepared": False,
        "order_dispatched": False,
    }
    gov = build_pipeline_governance(trade_pipeline_health=[row])["pipeline_governance"]["per_epic"][0]
    assert "ML_APPETITE_STRONG_BUT_NO_ORDER" in gov["pipeline_anomalies"]


def test_trade_pipeline_health_includes_strategy_fields(tmp_path, monkeypatch):
    db = tmp_path / "triage.db"
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
        """
    )
    conn.execute(
        """
        INSERT INTO production_orders
            (deal_reference, deal_id, epic, direction, size, status, created_at)
        VALUES ('MICRO-EURUSD-1', 'DEAL1', 'CS.D.EURUSD.CFD.IP', 'BUY', 1.0, 'CONFIRMED', datetime('now'))
        """
    )
    conn.commit()
    conn.close()

    rows = build_trade_pipeline_health(lookback_hours=24)
    match = next(r for r in rows if r["epic"] == "CS.D.EURUSD.CFD.IP")
    assert match["active_strategy_profile"] == "SCALP"
    assert match["strategy_source"] == "MICRO"


def test_gui_status_includes_strategy_fields(tmp_path, monkeypatch):
    scope = "ig:STRAT1"
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

    stub_row = {
        "epic": "CS.D.EURUSD.CFD.IP",
        "market_name": "EUR/USD",
        "pipeline_state": "LIVE",
        "active_strategy_profile": "MOMENTUM",
        "strategy_source": "PATH_A",
        "signal_ingested": True,
        "ml_appetite": {"appetite": "WEAK", "probability": 0.5, "reason": ""},
        "trailing_guards": {"active": True},
        "live_tracking": True,
    }
    stub_gov = {
        "pipeline_governance": {
            "per_epic": [
                {
                    "epic": "CS.D.EURUSD.CFD.IP",
                    "pipeline_health_score": 95,
                    "pipeline_anomalies": [],
                    "feed_anomalies": [],
                    "rotation_anomalies": [],
                    "active_strategy_profile": "MOMENTUM",
                    "strategy_source": "PATH_A",
                }
            ]
        },
        "session_governance": {"overall_session_health_score": 95, "session_anomalies": []},
        "gui_alerts": [],
    }

    with patch("api.gui_status.build_trade_pipeline_health", return_value=[stub_row]), patch(
        "api.gui_status.build_pipeline_governance",
        return_value=stub_gov,
    ):
        status = build_gui_status()

    assert status["trade_pipeline_health"][0]["active_strategy_profile"] == "MOMENTUM"
    assert status["pipeline_governance"]["per_epic"][0]["strategy_source"] == "PATH_A"
