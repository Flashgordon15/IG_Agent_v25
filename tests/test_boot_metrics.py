"""Tests for boot_metrics exposed on /api/health."""

from unittest.mock import patch

from system.boot_metrics import get_boot_metrics
from system.startup_tracker import mark, reset


def test_boot_metrics_stages_increment():
    with patch("api.snapshot_store.snapshot_age_s", return_value=None):
        reset()
        boot = get_boot_metrics()
        assert boot["percent"] == 0
        assert boot["label"] == "Broker Handshake"
        assert boot["ready"] is False

        mark("ig_auth")
        boot = get_boot_metrics()
        assert boot["percent"] == 20
        assert boot["label"] == "Database Core"

        mark("database")
        boot = get_boot_metrics()
        assert boot["percent"] == 40
        assert boot["label"] == "Trading Gates"

        mark("loops")
        boot = get_boot_metrics()
        assert boot["percent"] == 60
        assert boot["label"] == "Learning Plane"

        mark("learning")
        boot = get_boot_metrics()
        assert boot["percent"] == 80
        assert boot["label"] == "Initialization Complete"

        mark("ready")
        boot = get_boot_metrics()
        assert boot["percent"] == 100
        assert boot["label"] == "Initialization Complete"
        assert boot["ready"] is True


def test_startup_status_includes_boot_metrics():
    from fastapi.testclient import TestClient

    from api.server import create_app

    with patch("api.snapshot_store.snapshot_age_s", return_value=None):
        reset()
        client = TestClient(create_app(watch_snapshot=False))
        res = client.get("/api/startup/status")
        assert res.status_code == 200
        body = res.json()
        assert "boot_metrics" in body
        assert body["boot_metrics"]["percent"] == 0
        client.close()


def test_health_includes_boot_metrics():
    from api.agent_health import _build_fast_health_status

    with patch("api.snapshot_store.snapshot_age_s", return_value=None):
        reset()
        mark("ig_auth")
        health = _build_fast_health_status()
        assert "boot_metrics" in health
        assert health["boot_metrics"]["percent"] == 20
