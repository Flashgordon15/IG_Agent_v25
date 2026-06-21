"""Backward-compatible re-export — unified monolith lives in ``api.server``."""

from api.server import (  # noqa: F401
    _NO_CACHE,
    _SharedMemoryTelemetryHub,
    _StreamHub,
    _dashboard_dist,
    _mount_dashboard_spa_fallback,
    _read_jsonl,
    _router_mounted,
    _serve_telemetry_stream,
    _watchdog_failed,
    live_state_hub,
    mount_deferred_routers,
    register_deferred_route_tables,
    router,
    stream_hub,
    telemetry_stream_hub,
    ws_router,
)

__all__ = [
    "mount_deferred_routers",
    "register_deferred_route_tables",
    "_router_mounted",
    "router",
    "ws_router",
    "stream_hub",
    "telemetry_stream_hub",
    "live_state_hub",
]
