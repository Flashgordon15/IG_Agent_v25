"""v30 institutional kernel — supervisor, mock excision, quote provenance."""

from __future__ import annotations

import os

import pytest


def test_parallel_track_supervisor_pid_probe() -> None:
    from system.identity.process_orchestrator import ParallelTrackSupervisor, pid_alive

    assert pid_alive(os.getpid()) is True
    assert pid_alive(999999999) is False
    sup = ParallelTrackSupervisor(
        cycle_sec=900,
        live_pid=os.getpid(),
        shadow_pid=os.getpid(),
    )
    snap = sup.snapshot()
    assert snap["live_alive"] is True
    assert snap["shadow_alive"] is True


def test_live_production_mock_excision_blocks() -> None:
    from system.guard.live_path_guard import is_live_production_track

    saved = dict(os.environ)
    try:
        os.environ["IG_PARALLEL_TRACK"] = "live"
        os.environ["IG_APEX_RUNTIME_MODE"] = "PRODUCTION"
        assert is_live_production_track() is True
        os.environ["IG_PARALLEL_TRACK"] = "shadow"
        assert is_live_production_track() is False
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_normalize_hub_quote_source_labels() -> None:
    from system.market_data_hub import normalize_hub_quote_source

    assert normalize_hub_quote_source("ig_execution") == "ig_execution"
    assert normalize_hub_quote_source("rest") == "ig_rest"
    assert normalize_hub_quote_source("yahoo_heartbeat") == "yahoo"
    assert normalize_hub_quote_source("stream_a") == "synthetic"


def test_hub_quote_source_state_cache_roundtrip() -> None:
    from system.identity.state_cache import get_live_state_cache, reset_live_state_cache

    reset_live_state_cache()
    cache = get_live_state_cache()
    cache.update_hub_quote_source(
        epic="CS.D.CFPGOLD.CFP.IP",
        source="ig_rest",
        staleness_seconds=4,
    )
    snap = cache.read_snapshot()
    block = snap.get("hub_quote_source") or {}
    assert block["CS.D.CFPGOLD.CFP.IP"]["source"] == "ig_rest"
    assert block["CS.D.CFPGOLD.CFP.IP"]["staleness_seconds"] == 4
    reset_live_state_cache()


def test_isolated_cockpit_app_health_route() -> None:
    from api.isolated_cockpit_server import create_isolated_cockpit_app

    app = create_isolated_cockpit_app()
    assert app.title == "IG Agent Isolated Flight Deck"


def test_server_isolated_cockpit_policy() -> None:
    from api.server import isolated_cockpit_policy_summary

    policy = isolated_cockpit_policy_summary()
    assert policy["cockpit_port"] == "8787"
    assert policy["shm_segment"] == "ig_agent_v30_live_state"
    assert policy["mode"] == "read_only"
