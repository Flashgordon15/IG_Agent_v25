"""
Lightweight boot status API — O(1) HTTP response, <5ms target.

No external API calls on the request path; all state from boot_orchestrator cache.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi.responses import JSONResponse


def get_boot_status_response() -> dict[str, Any]:
    from system.boot.boot_orchestrator import get_boot_status_snapshot

    return get_boot_status_snapshot()


def get_boot_log_response(*, limit: int = 100) -> dict[str, Any]:
    from system.boot.boot_orchestrator import get_boot_log_entries

    return {
        "ok": True,
        "entries": get_boot_log_entries(limit=limit),
        "count": len(get_boot_log_entries(limit=limit)),
    }


def api_boot_status_json() -> JSONResponse:
    t0 = time.perf_counter()
    body = get_boot_status_response()
    try:
        from api.endpoint_profiler import record_request

        record_request("boot_status", (time.perf_counter() - t0) * 1000.0)
    except Exception:
        pass
    return JSONResponse(content=body)


def api_boot_log_json(*, limit: int = 100) -> JSONResponse:
    return JSONResponse(content=get_boot_log_response(limit=limit))
