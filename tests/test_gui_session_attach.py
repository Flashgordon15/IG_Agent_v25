"""GUI attach protocol and /api/gui_status contract tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from api.gui_status import build_gui_status
from gui.session_attach import (
    NO_SESSION_REASON,
    ZOMBIE_ATTACH_REASON,
    attach_to_session,
    evaluate_health_payload,
    extract_session_identity,
)
from runtime.app_mode import reset_app_mode_for_tests
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
    for key in (
        "APP_MODE",
        "IG_ACCOUNT_SCOPE",
        "IG_ACCOUNT_ID",
        "IG_DATA_ROOT",
        "IG_API_PORT",
        "IG_AGENT_CONFIG",
        "IG_ALLOW_LIVE",
        "IG_MOCK_FEED",
    ):
        monkeypatch.delenv(key, raising=False)


def _healthy_payload(**overrides) -> dict:
    base = {
        "app_mode": "DEMO",
        "account_scope": "ig:***",
        "session_id": "12345-1700000000",
        "session_status": "HEALTHY",
        "data_root": "/tmp/production",
        "config_overlay": "config/config_v31.json",
        "engine_paths_armed": {"path_a": True, "path_b": True, "micro": True},
        "port": 8080,
        "pid": 12345,
    }
    base.update(overrides)
    return base


def test_attach_succeeds_when_healthy():
    result = evaluate_health_payload(_healthy_payload())
    assert result.attached is True
    assert result.reason == "attached"
    assert result.session_identity["app_mode"] == "DEMO"
    assert result.session_identity["session_id"] == "12345-1700000000"


def test_attach_refuses_zombie():
    result = evaluate_health_payload(_healthy_payload(session_status="ZOMBIE"))
    assert result.attached is False
    assert result.reason == ZOMBIE_ATTACH_REASON


def test_attach_refuses_no_session():
    assert evaluate_health_payload(None).attached is False
    assert evaluate_health_payload(None).reason == NO_SESSION_REASON
    assert evaluate_health_payload({}).attached is False
    assert evaluate_health_payload(_healthy_payload(session_status="")).attached is False
    assert evaluate_health_payload(_healthy_payload(session_id="")).attached is False


def test_attach_refuses_unreachable_health():
    with patch("gui.session_attach.fetch_health_payload", return_value=None):
        result = attach_to_session(port=8080)
    assert result.attached is False
    assert result.reason == NO_SESSION_REASON


def test_extract_session_identity_fields():
    payload = _healthy_payload(broker_plane="DEMO", api_port=8080)
    identity = extract_session_identity(payload)
    assert identity["account_scope"] == "ig:***"
    assert identity["config_overlay"] == "config/config_v31.json"
    assert identity["engine_paths_armed"]["path_a"] is True


def test_gui_status_returns_required_fields(tmp_path, monkeypatch):
    scope = "ig:GUIACCT"
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

    feed_stub = {
        "feeds": {
            "feed1": {"status": "OK", "latency_ms": 10.0, "last_update_timestamp": "2026-06-25T12:00:00Z", "label": "hub_quotes"},
            "feed2": {"status": "DEGRADED", "latency_ms": None, "last_update_timestamp": None, "label": "rest_stream"},
            "feed3": {"status": "OK", "latency_ms": 1.0, "last_update_timestamp": "2026-06-25T12:00:00Z", "label": "mock_or_secondary"},
        },
        "ranking": {"primary_feed": "feed1", "secondary_feed": "feed3", "tertiary_feed": "feed2"},
    }

    with patch("api.gui_status.build_api_feed_health", return_value=feed_stub), patch(
        "api.gui_status.build_trade_pipeline_health", return_value=[]
    ), patch(
        "api.gui_status.build_pipeline_governance",
        return_value={
            "pipeline_governance": {"per_epic": []},
            "session_governance": {"overall_session_health_score": 100, "session_anomalies": []},
            "gui_alerts": [],
        },
    ), patch(
        "api.gui_status._last_trade_timestamp", return_value="2026-06-25T12:00:00Z"
    ), patch("runtime.session_lock.session_is_healthy", return_value=True):
        status = build_gui_status()

    assert status["app_mode"] == "DEMO"
    assert status["account_scope"] == "ig:***"
    assert "GUIACCT" not in json.dumps(status)
    assert status["config_overlay"] == "config/config_v31.json"
    assert status["data_root"] == str(data_root)
    assert status["port"] == 8080
    assert status["gui_attach_ready"] is True
    assert status["engine_health"]["path_a"] in ("armed", "unarmed")
    assert set(status["api_feed_health"]["feeds"].keys()) == {"feed1", "feed2", "feed3"}
    assert status["api_feed_health"]["ranking"]["primary_feed"] == "feed1"
    assert status["trade_pipeline_health"] == []
    assert status["session_uptime"] is not None
    assert status["last_trade_timestamp"] == "2026-06-25T12:00:00Z"
    assert status["market_rotation_status"]["rotation_state"] == "IDLE"


def test_gui_status_testbed_isolated_root(tmp_path, monkeypatch):
    testbed_root = tmp_path / "testbed"
    testbed_root.mkdir()
    monkeypatch.setenv("APP_MODE", "TESTBED")
    monkeypatch.setenv("IG_ACCOUNT_SCOPE", TESTBED_ACCOUNT_SCOPE)
    monkeypatch.setenv("IG_DATA_ROOT", str(testbed_root))
    monkeypatch.setenv("IG_AGENT_CONFIG", "config/config_v31_testbed.json")
    monkeypatch.setenv("IG_API_PORT", "9199")
    monkeypatch.setenv("IG_MOCK_FEED", "1")
    reset_app_mode_for_tests()

    lock = lock_path_for_scope(TESTBED_ACCOUNT_SCOPE, testbed_root)
    write_session_lock(
        lock,
        pid=os.getpid(),
        port=9199,
        account_scope=TESTBED_ACCOUNT_SCOPE,
        started_at=1_700_000_100,
    )

    feed_stub = {
        "feeds": {
            "feed1": {"status": "DEGRADED", "latency_ms": None, "last_update_timestamp": None, "label": "hub_quotes"},
            "feed2": {"status": "DEGRADED", "latency_ms": None, "last_update_timestamp": None, "label": "rest_stream"},
            "feed3": {"status": "OK", "latency_ms": 1.0, "last_update_timestamp": "2026-06-25T12:00:00Z", "label": "mock_or_secondary"},
        },
        "ranking": {"primary_feed": "feed3", "secondary_feed": "feed1", "tertiary_feed": "feed2"},
    }

    with patch("runtime.session_lock.session_is_healthy", return_value=True), patch(
        "api.gui_status.build_api_feed_health",
        return_value=feed_stub,
    ), patch("api.gui_status.build_trade_pipeline_health", return_value=[]), patch(
        "api.gui_status.build_pipeline_governance",
        return_value={
            "pipeline_governance": {"per_epic": []},
            "session_governance": {"overall_session_health_score": 100, "session_anomalies": []},
            "gui_alerts": [],
        },
    ):
        status = build_gui_status()

    assert status["app_mode"] == "TESTBED"
    assert status["account_scope"] == TESTBED_ACCOUNT_SCOPE
    assert status["data_root"] == str(testbed_root)
    assert status["port"] == 9199
    assert status["gui_attach_ready"] is True


def test_gui_status_live_masks_account(tmp_path, monkeypatch):
    scope = "ig:LIVESECRET123"
    data_root = tmp_path / "prod"
    data_root.mkdir()
    monkeypatch.setenv("APP_MODE", "LIVE")
    monkeypatch.setenv("IG_ALLOW_LIVE", "1")
    monkeypatch.setenv("IG_ACCOUNT_SCOPE", scope)
    monkeypatch.setenv("IG_DATA_ROOT", str(data_root))
    monkeypatch.setenv("IG_AGENT_CONFIG", "config/config_v31_live_canary.json")
    monkeypatch.setenv("IG_API_PORT", "8080")
    reset_app_mode_for_tests()

    lock = lock_path_for_scope(scope, data_root)
    write_session_lock(lock, pid=os.getpid(), port=8080, account_scope=scope)

    with patch("runtime.session_lock.session_is_healthy", return_value=True):
        status = build_gui_status()

    assert status["app_mode"] == "LIVE"
    assert status["account_scope"] == "ig:***"
    assert "LIVESECRET" not in json.dumps(status)


def test_attach_refuses_when_no_lock_in_health_payload():
    payload = {
        "app_mode": "DEMO",
        "session_status": "",
        "session_id": "",
    }
    result = evaluate_health_payload(payload)
    assert result.attached is False
    assert result.reason == NO_SESSION_REASON


def test_attach_via_gui_status_payload():
    payload = _healthy_payload()
    payload["gui_attach_ready"] = True
    payload["engine_health"] = {"path_a": "armed", "path_b": "armed", "micro": "armed"}
    result = evaluate_health_payload(payload)
    assert result.attached is True
