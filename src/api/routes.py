"""
HTTP routes — read-only except POST /api/close/{deal_id} (Section 4.5 Step 8).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from api.close_handler import close_deal
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
    return get_tick()


@router.post("/api/close/{deal_id}")
def api_close_deal(deal_id: str) -> JSONResponse:
    """
    ONLY write endpoint — manual close routed to IG close_position().

    See spec Section 4.5 Step 8 and 6.3 manual close routing.
    """
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
