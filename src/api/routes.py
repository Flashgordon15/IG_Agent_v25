"""
HTTP routes — dashboard API (Section 4.5 Steps 8 + 13).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from api.agent_control import (
    is_paused,
    run_emergency_stop,
    start_trading,
    stop_trading,
)
from api.close_handler import close_deal
from api.dashboard_data import (
    dismiss_splash,
    get_closed_trades,
    get_signal_log,
    get_system_info,
    read_version_state,
    run_system_tests,
)
from api.snapshot_store import get_tick, snapshot_age_s

router = APIRouter()


@router.get("/health")
def health() -> dict[str, Any]:
    age = snapshot_age_s()
    return {
        "ok": True,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "api": "up",
        "snapshot_age_s": age,
    }


@router.get("/state")
def state() -> dict[str, Any]:
    """Full dashboard snapshot — same schema as WebSocket tick messages."""
    tick = get_tick()
    tick["trading_paused"] = is_paused()
    return tick


@router.get("/api/splash")
def api_splash_state() -> dict[str, Any]:
    return read_version_state()


@router.post("/api/splash/dismiss")
def api_splash_dismiss() -> dict[str, Any]:
    return dismiss_splash()


@router.post("/api/start")
def api_start() -> dict[str, Any]:
    result = start_trading()
    if not result.get("ok"):
        raise HTTPException(status_code=503, detail=result.get("error", "start failed"))
    return result


@router.post("/api/stop")
def api_stop() -> dict[str, Any]:
    result = stop_trading()
    if not result.get("ok"):
        raise HTTPException(status_code=503, detail=result.get("error", "stop failed"))
    return result


@router.post("/api/emergency_stop")
def api_emergency_stop() -> dict[str, Any]:
    result = run_emergency_stop()
    return JSONResponse(result, status_code=200 if result.get("ok") else 500)


@router.get("/api/trades")
def api_trades(limit: int = 50) -> dict[str, Any]:
    trades = get_closed_trades(limit=min(100, max(1, limit)))
    points_total = sum(float(t.get("points_score") or 0) for t in trades)
    return {"trades": trades, "points_total": points_total}


@router.get("/api/signals")
def api_signals(limit: int = 50) -> dict[str, Any]:
    return {"signals": get_signal_log(limit=min(100, max(1, limit)))}


@router.get("/api/system")
def api_system() -> dict[str, Any]:
    return get_system_info()


@router.post("/api/system/tests")
def api_system_tests() -> dict[str, Any]:
    return run_system_tests()


@router.post("/api/close/{deal_id}")
def api_close_deal(deal_id: str) -> JSONResponse:
    """Manual position close — routes to IG close_position()."""
    try:
        result = close_deal(deal_id)
        return JSONResponse({"ok": True, "deal_id": deal_id, "result": result})
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"close failed: {type(e).__name__}: {e}",
        ) from e
