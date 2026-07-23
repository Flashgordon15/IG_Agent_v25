"""Maintenance detachment — suppress broker order dispatch; surveillance continues."""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

from system.engine_log import log_engine

_SHIELD_LOG = "[🛡️ MAINTENANCE SAFETY SHIELD - ORDER DISPATCH SUPPRESSED]"


def is_core_detached() -> bool:
    """True when CORE_DETACHED=TRUE — order wire suppressed, ticks/telemetry OK."""
    return os.getenv("CORE_DETACHED", "FALSE").upper() == "TRUE"


def suppress_order_dispatch(
    *,
    source: str,
    epic: str | None = None,
    direction: str | None = None,
    action: str = "entry",
    **extra: Any,
) -> dict[str, Any]:
    """Log shield, return isolated mock confirmation — no IG REST order calls."""
    log_engine(
        f"{_SHIELD_LOG} source={source} action={action} "
        f"epic={epic or '-'} dir={direction or '-'}"
    )
    ref = f"MOCK-DETACHED-{uuid.uuid4().hex[:12].upper()}"
    out: dict[str, Any] = {
        "dealReference": ref,
        "status": "MAINTENANCE_DETACHED",
        "core_detached": True,
        "maintenance_detached": True,
        "mock_confirm": True,
        "source": source,
        "action": action,
        "ts": time.time(),
        "confirm": {
            "terminal": True,
            "accepted": True,
            "rejected": False,
            "deal_id": ref,
            "deal_reference": ref,
            "status": "MAINTENANCE_DETACHED",
            "reason": "core_detached",
        },
    }
    if epic:
        out["epic"] = str(epic)
    if direction:
        out["direction"] = str(direction).upper()
    out.update(extra)
    return out
