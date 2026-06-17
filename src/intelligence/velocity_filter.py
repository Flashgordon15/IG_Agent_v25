"""
Order-book tick velocity filter — rolling 200ms burst detection.

Triggers a 90% confidence override when tick arrivals exceed 15 in 200ms.
Read-only microstructure buffer access; safe for cockpit telemetry collection.
"""

from __future__ import annotations

import time
from typing import Any

TICK_WINDOW_MS = 200
TICK_WINDOW_SEC = TICK_WINDOW_MS / 1000.0
TICK_OVERRIDE_THRESHOLD = 15
CONFIDENCE_OVERRIDE_PCT = 90.0


def _confidence_pct(micro_confidence: float) -> float:
    conf = float(micro_confidence or 0.0)
    if conf <= 1.0:
        return round(conf * 100.0, 1)
    return round(conf, 1)


def ticks_in_window(epic: str, *, window_sec: float = TICK_WINDOW_SEC) -> int:
    key = str(epic or "").strip()
    if not key or window_sec <= 0:
        return 0
    try:
        from intelligence.intelligence_worker import get_intelligence_worker

        clf = get_intelligence_worker().micro_model
        return int(clf.ticks_in_window(key, float(window_sec), now=time.time()))
    except Exception:
        return 0


def confidence_override_active(
    *,
    ticks_200ms: int,
    micro_confidence: float,
) -> bool:
    return int(ticks_200ms) >= TICK_OVERRIDE_THRESHOLD or _confidence_pct(
        micro_confidence
    ) >= CONFIDENCE_OVERRIDE_PCT


def scalping_velocity_telemetry(
    epic: str,
    *,
    micro_confidence: float = 0.0,
) -> dict[str, Any]:
    ticks = ticks_in_window(epic)
    confidence_pct = _confidence_pct(micro_confidence)
    override = confidence_override_active(
        ticks_200ms=ticks,
        micro_confidence=micro_confidence,
    )
    return {
        "ticks_200ms": int(ticks),
        "override_active": bool(override),
        "confidence_pct": confidence_pct,
        "threshold_ticks": TICK_OVERRIDE_THRESHOLD,
        "window_ms": TICK_WINDOW_MS,
        "override_confidence_pct": CONFIDENCE_OVERRIDE_PCT,
    }
