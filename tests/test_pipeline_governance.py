"""Pipeline governance layer tests — rule-based anomaly detection."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from api.gui_status import build_gui_status
from runtime.app_mode import reset_app_mode_for_tests
from runtime.pipeline_governance import (
    FEED_LATENCY_DEGRADED_MS,
    ORDER_PENDING_MAX_SEC,
    RECONCILE_AFTER_CLOSE_MAX_SEC,
    build_pipeline_governance,
    evaluate_epic_governance_for_test,
)
from runtime.pipeline_health import reset_pipeline_health_for_tests
from runtime.session_lock import (
    lock_path_for_scope,
    reset_session_lock_state_for_tests,
    write_session_lock,
)


def _ago_iso(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


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
    ):
        monkeypatch.delenv(key, raising=False)


def _epic_row(**overrides) -> dict:
    base = {
        "epic": "CS.D.EURUSD.CFD.IP",
        "market_name": "EUR/USD",
        "pipeline_state": "IDLE",
        "signal_ingested": False,
        "ml_appetite": {"appetite": "NONE", "probability": 0.0, "reason": ""},
        "order_prepared": False,
        "order_dispatched": False,
        "order_confirmed": False,
        "live_tracking": False,
        "trailing_guards": {"active": False},
        "closed": False,
        "reconciled": False,
        "active_strategy_profile": "UNKNOWN",
        "strategy_source": "NONE",
    }
    base.update(overrides)
    return base


def test_governance_structure_in_gui_status(tmp_path, monkeypatch):
    scope = "ig:GOV1"
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

    status = build_gui_status()
    assert "pipeline_governance" in status
    assert "per_epic" in status["pipeline_governance"]
    assert "session_governance" in status
    assert "overall_session_health_score" in status["session_governance"]
    assert "session_anomalies" in status["session_governance"]
    assert isinstance(status["gui_alerts"], list)


def test_order_pending_too_long_flagged():
    row = _epic_row(
        pipeline_state="ORDER_PENDING",
        order_dispatched=True,
        order_dispatched_timestamp=_ago_iso(ORDER_PENDING_MAX_SEC + 60),
        active_strategy_profile="SCALP",
        strategy_source="MICRO",
    )
    gov = evaluate_epic_governance_for_test(row)
    assert "ORDER_PENDING_TOO_LONG" in gov["pipeline_anomalies"]
    assert gov["pipeline_health_score"] < 100
    alerts = build_pipeline_governance(trade_pipeline_health=[row])["gui_alerts"]
    assert any(a["code"] == "ORDER_STALL" and a["scope"] == "EPIC" for a in alerts)


def test_no_reconcile_after_close_flagged():
    row = _epic_row(
        pipeline_state="CLOSED",
        closed=True,
        closed_timestamp=_ago_iso(RECONCILE_AFTER_CLOSE_MAX_SEC + 120),
        reconciled=False,
    )
    gov = evaluate_epic_governance_for_test(row)
    assert "NO_RECONCILE_AFTER_CLOSE" in gov["pipeline_anomalies"]
    result = build_pipeline_governance(trade_pipeline_health=[row])
    assert "RECONCILIATION_LAG_DETECTED" in result["session_governance"]["session_anomalies"]
    assert any(a["code"] == "RECONCILE_LAG" for a in result["gui_alerts"])


def test_primary_feed_stale_flagged():
    row = _epic_row(
        pipeline_state="LIVE",
        live_tracking=True,
        order_confirmed=True,
    )
    feed_health = {
        "feeds": {
            "feed1": {
                "status": "DEGRADED",
                "latency_ms": FEED_LATENCY_DEGRADED_MS + 1000,
                "last_update_timestamp": _ago_iso(300),
                "label": "hub_quotes",
            },
            "feed2": {"status": "DEGRADED", "latency_ms": None, "last_update_timestamp": None, "label": "rest_stream"},
            "feed3": {"status": "DEGRADED", "latency_ms": None, "last_update_timestamp": None, "label": "mock"},
        },
        "ranking": {"primary_feed": "feed1", "secondary_feed": "feed2", "tertiary_feed": "feed3"},
    }
    gov = evaluate_epic_governance_for_test(row, api_feed_health=feed_health)
    assert "PRIMARY_FEED_STALE" in gov["feed_anomalies"]
    assert "ALL_FEEDS_DEGRADED" in gov["feed_anomalies"]


def test_ml_strong_without_order_flagged():
    row = _epic_row(
        pipeline_state="SIGNAL_ONLY",
        signal_ingested=True,
        signal_timestamp=_ago_iso(300),
        ml_appetite={"appetite": "STRONG", "probability": 0.75, "reason": "blend"},
        order_prepared=False,
        order_dispatched=False,
        active_strategy_profile="MOMENTUM",
        strategy_source="PATH_A",
    )
    gov = evaluate_epic_governance_for_test(row)
    assert "ML_APPETITE_STRONG_BUT_NO_ORDER" in gov["pipeline_anomalies"]


def test_session_governance_multiple_stalls():
    rows = [
        _epic_row(
            epic="CS.D.EURUSD.CFD.IP",
            pipeline_state="ORDER_PENDING",
            order_dispatched=True,
            order_dispatched_timestamp=_ago_iso(ORDER_PENDING_MAX_SEC + 30),
            active_strategy_profile="SCALP",
            strategy_source="MICRO",
        ),
        _epic_row(
            epic="IX.D.DOW.IFM.IP",
            pipeline_state="ORDER_PENDING",
            order_dispatched=True,
            order_dispatched_timestamp=_ago_iso(ORDER_PENDING_MAX_SEC + 45),
            active_strategy_profile="SCALP",
            strategy_source="MICRO",
        ),
    ]
    result = build_pipeline_governance(trade_pipeline_health=rows)
    assert "MULTIPLE_EPICS_STALLED_IN_ORDER_PENDING" in result["session_governance"]["session_anomalies"]
    codes = {a["code"] for a in result["gui_alerts"]}
    assert "MULTIPLE_EPICS_STALLED" in codes or "ORDER_STALL" in codes


def test_rotation_anomaly_active_market_no_signal():
    row = _epic_row(
        epic="CS.D.CFPGOLD.CFP.IP",
        signal_ingested=False,
        active_strategy_profile="MOMENTUM",
        strategy_source="PATH_A",
    )
    rotation = {
        "active_markets": ["CS.D.CFPGOLD.CFP.IP"],
        "candidate_markets": [],
        "rotation_state": "IDLE",
    }
    gov = evaluate_epic_governance_for_test(row, market_rotation_status=rotation)
    assert "ACTIVE_MARKET_WITHOUT_RECENT_SIGNAL" in gov["rotation_anomalies"]


def test_gui_alerts_severity_and_scope():
    row = _epic_row(
        pipeline_state="LIVE",
        live_tracking=True,
        trailing_guards={"active": False},
    )
    result = build_pipeline_governance(trade_pipeline_health=[row])
    alert = next(a for a in result["gui_alerts"] if a["code"] == "TRAILING_MISSING")
    assert alert["severity"] == "WARN"
    assert alert["scope"] == "EPIC"
    assert alert["epic"] == row["epic"]
