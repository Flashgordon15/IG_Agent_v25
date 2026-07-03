"""Agent API proxy for decoupled Flight Deck cockpit processes."""

from __future__ import annotations

from unittest.mock import patch

import pytest


def test_gates_all_pending_detects_stub():
    from cockpit.agent_api_proxy import gates_all_pending

    assert gates_all_pending([]) is True
    assert gates_all_pending([{"id": "G1", "status": "pending"}]) is True
    assert gates_all_pending([{"id": "G1", "status": "complete"}]) is False


def test_iron_cage_is_agent_coupled_trade_ready_without_gates():
    from cockpit.agent_api_proxy import iron_cage_is_agent_coupled

    local = {"trade_ready": True, "gates": None}
    agent = {"trade_ready": True, "gates": None}
    assert iron_cage_is_agent_coupled(local, agent) is True


def test_iron_cage_is_agent_coupled_detects_orphan_stub():
    from cockpit.agent_api_proxy import iron_cage_is_agent_coupled

    pending = {"trade_ready": False, "gates": {"G1": {"status": "pending"}}}
    assert iron_cage_is_agent_coupled(pending, pending) is False


def test_iron_cage_is_agent_coupled_agent_trade_ready_rescues_local_pending():
    from cockpit.agent_api_proxy import iron_cage_is_agent_coupled

    local = {"trade_ready": False, "gates": {"G1": {"status": "pending"}}}
    agent = {"trade_ready": True, "gates": None}
    assert iron_cage_is_agent_coupled(local, agent) is True


def test_resolve_iron_cage_uses_fast_health_light_snapshot():
    from cockpit.agent_api_proxy import resolve_iron_cage_status

    fast = {
        "trade_ready": True,
        "blockers": [],
        "source": "health_light_fast",
        "execution": {"routes_armed": 7},
    }
    with patch(
        "system.iron_cage_readiness.fast_iron_cage_status_snapshot",
        return_value=fast,
    ):
        out = resolve_iron_cage_status()
    assert out["trade_ready"] is True
    assert out["source"] == "health_light_fast"


def test_resolve_iron_cage_never_proxies_in_agent_process():
    from cockpit.agent_api_proxy import resolve_iron_cage_status

    local = {
        "trade_ready": True,
        "blockers": [],
        "source": "health_light_fast",
    }
    with patch(
        "cockpit.agent_api_proxy.in_trading_agent_process",
        return_value=True,
    ), patch(
        "system.iron_cage_readiness.fast_iron_cage_status_snapshot",
        return_value=local,
    ), patch("cockpit.agent_api_proxy.fetch_agent_json") as mock_fetch:
        out = resolve_iron_cage_status()
    mock_fetch.assert_not_called()
    assert out["trade_ready"] is True


def test_hydrate_telemetry_merges_gate_map():
    from cockpit.agent_api_proxy import hydrate_telemetry_from_agent

    iron = {
        "gates": [
            {"id": "G1", "status": "complete", "detail": "ok"},
            {"id": "G2", "status": "running", "detail": "warming"},
        ],
        "ig_account_id": "Z6BAH4",
    }
    live_snap = {
        "ts": 1.0,
        "epics": {"CS.D.CFPGOLD.CFP.IP": {"bid": 1.0, "offer": 1.1, "spread": 0.1}},
        "spread": {"CS.D.CFPGOLD.CFP.IP": {"z_score": 0.5, "throttle": 0.1}},
        "gates": {"G1": {"status": "complete", "detail": "ok"}},
    }
    with patch(
        "cockpit.agent_api_proxy.collect_live_telemetry_snapshot",
        return_value=live_snap,
    ):
        out = hydrate_telemetry_from_agent({"gates": {}})
    assert out["gates"]["G1"]["status"] == "complete"
    assert out["epics"]["CS.D.CFPGOLD.CFP.IP"]["bid"] == 1.0


def test_ensure_cockpit_web_server_waits_without_spawning():
    from cockpit import desktop_app_shell as shell

    with patch.object(shell, "_url_alive", return_value=False), patch(
        "cockpit.desktop_app_shell.time.sleep"
    ):
        assert shell._ensure_cockpit_web_server() is False
