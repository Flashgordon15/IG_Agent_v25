"""
Unified status API — full runtime snapshot for IG Cockpit.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi.responses import JSONResponse


def get_unified_status_response() -> dict[str, Any]:
    from system.unified_runtime_state import snapshot

    return snapshot()


def get_trade_lifecycle_response() -> dict[str, Any]:
    from runtime.trade_lifecycle import snapshot as lifecycle_snapshot
    from system.trade_lifecycle_bus import get_lifecycle_bus

    bus = get_lifecycle_bus().snapshot()
    machine = lifecycle_snapshot()
    return {
        "ok": True,
        "machine": machine,
        "bus": bus,
    }


def get_rejections_response(*, limit: int = 20) -> dict[str, Any]:
    from runtime.broker_reject_guard import broker_reject_guard_status
    from system.unified_runtime_state import get_rejections

    return {
        "ok": True,
        "rejections": get_rejections(limit=limit),
        "guard": broker_reject_guard_status(),
    }


def get_rotation_status_response() -> dict[str, Any]:
    try:
        from runtime.dual_core_execution import get_rotation_state

        rot = get_rotation_state()
    except Exception:
        rot = {}
    from system.unified_runtime_state import snapshot

    unified = snapshot()
    feed_state: dict[str, Any] = {}
    try:
        from system.feeds.data_feed_orchestrator import get_data_feed_state

        feed_state = get_data_feed_state()
    except Exception:
        pass
    try:
        from runtime.market_orchestrator import MarketOrchestrator

        orchestrator_ranks = MarketOrchestrator.get_global_rotation_rank_snapshot()
        active_epics = MarketOrchestrator.get_global_active_epics()
    except Exception:
        orchestrator_ranks = []
        active_epics = []
    return {
        "ok": True,
        "rotation": rot,
        "rotation_scores": rot.get("rotation_scores") or [],
        "rotation_history": rot.get("rotation_history") or [],
        "orchestrator_ranks": orchestrator_ranks,
        "active_epics": active_epics,
        "routing": unified.get("routing") or {},
        "data_feeds": feed_state,
        "multi_market": {
            "universe": list(rot.get("rotation_universe") or []),
            "active_instruments": (rot.get("active_instruments") or [])[:8],
            "eligible_instruments": (rot.get("eligible_instruments") or [])[:8],
            "feed_health": feed_state.get("health"),
            "primary_feed": feed_state.get("primary_feed"),
        },
    }


def api_unified_status_json() -> JSONResponse:
    t0 = time.perf_counter()
    body = get_unified_status_response()
    try:
        from api.endpoint_profiler import record_request

        record_request("unified_status", (time.perf_counter() - t0) * 1000.0)
    except Exception:
        pass
    return JSONResponse(content=body)


def api_trade_lifecycle_json() -> JSONResponse:
    return JSONResponse(content=get_trade_lifecycle_response())


def api_rejections_json(*, limit: int = 20) -> JSONResponse:
    return JSONResponse(content=get_rejections_response(limit=limit))


def api_rotation_status_json() -> JSONResponse:
    return JSONResponse(content=get_rotation_status_response())
