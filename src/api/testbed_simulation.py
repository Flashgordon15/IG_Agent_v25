"""HTTP surface for HARDENED_TESTBED replay telemetry and velocity controls."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()


class ReplaySpeedBody(BaseModel):
    speed: float = Field(..., gt=0, le=10_000)


@router.get("/api/testbed/status")
def api_testbed_status() -> dict[str, Any]:
    from simulation.replay_telemetry import telemetry_dict

    return {"ok": True, **telemetry_dict()}


@router.post("/api/testbed/replay-speed")
def api_testbed_replay_speed(body: ReplaySpeedBody) -> dict[str, Any]:
    try:
        from system.apex_runtime_mode import ApexRuntimeMode, get_apex_runtime_mode

        if get_apex_runtime_mode() is not ApexRuntimeMode.HARDENED_TESTBED:
            raise HTTPException(
                status_code=403,
                detail="replay speed control only available in HARDENED_TESTBED",
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    from simulation.replay_telemetry import set_speed, telemetry_dict

    applied = set_speed(body.speed)
    return {"ok": True, "speed": applied, **telemetry_dict()}
