"""Tests for /api/stop pause truth + health cache overlay."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from api import agent_control, agent_health


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    monkeypatch.setenv("IG_TEST_HARNESS", "1")
    monkeypatch.setenv("IG_AGENT_PYTEST", "1")
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr("system.paths.state_dir", lambda: state)
    monkeypatch.setattr("system.paths.data_dir", lambda: tmp_path)
    agent_control.reset_agent_control_for_tests()
    agent_health.reset_health_cache_for_tests()
    yield state


def test_stop_sets_paused_and_busts_health_cache(_reset):
    loop = MagicMock()
    loop.is_running.return_value = False
    agent_control.register_trading_loop(loop)

    # Seed stale cache claiming not paused
    with agent_health._HEALTH_CACHE_LOCK:
        agent_health._HEALTH_CACHE = {
            "ok": True,
            "trading_paused": False,
            "issues": [],
        }

    out = agent_control.stop_trading()
    assert out["ok"] is True
    assert out["trading_paused"] is True
    assert out["status"] in {"paused", "already_paused"}
    assert agent_control.is_paused() is True

    cached = agent_health.get_cached_health_status(allow_slow_fallback=False)
    assert cached["trading_paused"] is True
    assert "trading_paused" in (cached.get("issues") or [])


def test_stop_when_running_returns_stopped(_reset):
    loop = MagicMock()
    loop.is_running.return_value = True
    agent_control.register_trading_loop(loop)
    out = agent_control.stop_trading()
    assert out["status"] == "stopped"
    assert out["trading_paused"] is True
    loop.stop.assert_called_once()


def test_start_clears_pause(_reset):
    loop = MagicMock()
    loop.is_running.return_value = False
    agent_control.register_trading_loop(loop)
    agent_control.stop_trading()
    out = agent_control.start_trading()
    assert out["trading_paused"] is False
    assert agent_control.is_paused() is False
