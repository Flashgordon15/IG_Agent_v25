"""Unified system diagnostics for cockpit and operators — read-only telemetry."""

from __future__ import annotations

import time
from typing import Any


def build_system_diagnostics() -> dict[str, Any]:
    """Aggregate routing, risk, feeds, execution, gates — O(1) from caches."""
    out: dict[str, Any] = {"ts": time.time()}

    try:
        from api.endpoint_profiler import timing_summary

        out["endpoint_profile"] = timing_summary()
    except Exception:
        out["endpoint_profile"] = {}

    try:
        from api.readiness_snapshot import _META, get_gui_snapshot, get_health_snapshot

        code, health = get_health_snapshot()
        out["health"] = {
            "http_code": code,
            "status": health.get("status"),
            "ready": health.get("ready"),
            "readiness_level": health.get("readiness_level"),
            "trading_loops_running": health.get("trading_loops_running"),
            "trading_paused": health.get("trading_paused"),
            "quotes_fresh_count": health.get("quotes_fresh_count"),
        }
        gui = get_gui_snapshot()
        out["gui"] = {
            "snapshot_tier": gui.get("snapshot_tier"),
            "readiness_level": gui.get("readiness_level"),
            "cockpit_usable": gui.get("cockpit_usable"),
            "route_count": len(gui.get("unified_execution_route") or []),
            "feed_keys": list((gui.get("api_feed_health") or {}).get("feeds", {}).keys())
            if isinstance(gui.get("api_feed_health"), dict)
            else len(gui.get("api_feed_health") or []),
        }
        out["snapshot_meta"] = dict(_META)
    except Exception as exc:
        out["health_error"] = str(exc)

    try:
        from api.agent_state import get_agent_state

        state = get_agent_state()
        out["agent_state"] = {
            "version": state.get("version"),
            "positions": len(state.get("positions") or []),
            "routing": len(state.get("routing") or []),
            "feeds": len(state.get("feeds") or []),
            "gate_phase": (state.get("gate_progression") or {}).get("phase"),
        }
    except Exception:
        pass

    try:
        from runtime.unified_execution import cached_unified_routes

        routes = cached_unified_routes()
        armed = sum(
            1 for r in routes if str(r.get("execution_path") or "NONE") != "NONE"
        )
        out["routing"] = {
            "route_count": len(routes),
            "armed_count": armed,
            "routes_none": len(routes) - armed,
        }
    except Exception:
        pass

    try:
        from runtime.hard_enforcement import _DECISIONS_CACHE, _DECISIONS_CACHE_AT

        active = sum(1 for r in _DECISIONS_CACHE.values() if r.get("active"))
        out["governance"] = {
            "hard_enforcement_active": active,
            "decisions_cached": len(_DECISIONS_CACHE),
            "cache_age_s": round(time.time() - float(_DECISIONS_CACHE_AT or 0.0), 2),
        }
    except Exception:
        pass

    try:
        from api.agent_control import is_paused, is_trading_running

        out["execution"] = {
            "loops_running": is_trading_running(),
            "paused": is_paused(),
        }
    except Exception:
        pass

    try:
        from system.system_state import get_system_state

        snap = get_system_state().snapshot()
        gates = snap.get("gates") or {}
        out["gates"] = {
            "phase": snap.get("phase"),
            "percent": snap.get("percent"),
            "ready": snap.get("ready"),
            "G1": (gates.get("G1") or {}).get("status"),
            "G2": (gates.get("G2") or {}).get("status"),
            "G3": (gates.get("G3") or {}).get("status"),
            "G4": (gates.get("G4") or {}).get("status"),
            "G5": (gates.get("G5") or {}).get("status"),
        }
    except Exception:
        pass

    try:
        from runtime.pipeline_health import build_market_rotation_status

        out["rotation"] = build_market_rotation_status()
    except Exception:
        pass

    return out
