"""Trade state and events API — lifecycle + rotation for IG Cockpit."""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


def get_trade_state_response() -> dict[str, Any]:
    from runtime.dynamic_limit_engine import snapshot as dynamic_snapshot
    from runtime.trade_lifecycle import snapshot as lifecycle_snapshot
    from runtime.virtual_stop_loss import virtual_stop_snapshot
    from system.unified_runtime_state import snapshot as unified_snapshot

    lc = lifecycle_snapshot()
    unified = unified_snapshot()
    return {
        "ok": True,
        "lifecycle": lc,
        "stops": virtual_stop_snapshot(),
        "dynamic_limits": dynamic_snapshot(),
        "sizing": unified.get("sizing") or {},
        "execution": unified.get("execution") or {},
        "startup_diagnostics": unified.get("startup_diagnostics") or {},
    }


def get_trade_events_response(*, limit: int = 50) -> dict[str, Any]:
    from runtime.trade_lifecycle import get_trade_events
    from system.trade_lifecycle_bus import get_lifecycle_bus

    return {
        "ok": True,
        "events": get_trade_events(limit=limit),
        "bus": get_lifecycle_bus().snapshot(),
    }


def get_rotation_state_response() -> dict[str, Any]:
    from api.unified_status import get_rotation_status_response

    return get_rotation_status_response()


def api_trade_state_json() -> JSONResponse:
    return JSONResponse(content=get_trade_state_response())


def api_trade_events_json(*, limit: int = 50) -> JSONResponse:
    return JSONResponse(content=get_trade_events_response(limit=limit))


def api_rotation_state_json() -> JSONResponse:
    return JSONResponse(content=get_rotation_state_response())
