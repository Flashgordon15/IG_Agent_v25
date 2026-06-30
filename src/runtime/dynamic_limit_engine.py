"""Volatility-aware dynamic profit targets — driven by Yahoo mids, non-blocking."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from system.engine_log import log_engine

_lock = threading.RLock()
_tracks: dict[str, "DynamicLimitTrack"] = {}
_active = False


@dataclass
class DynamicLimitTrack:
    deal_id: str
    epic: str
    direction: str
    entry_level: float
    limit_level: float
    limit_pts: float
    updated_at: float


def start_dynamic_limit_engine() -> None:
    global _active
    with _lock:
        _active = True
    try:
        from system.unified_runtime_state import update_stops_limits

        update_stops_limits(dynamic_limit_active=True)
    except Exception:
        pass
    log_engine("DynamicLimit: engine active")


def register_dynamic_limit(
    *,
    deal_id: str,
    epic: str,
    direction: str,
    entry_level: float,
    limit_pts: float,
) -> None:
    """Arm dynamic limit for a trade."""
    key = str(deal_id or epic)
    d = str(direction or "BUY").upper()
    limit_level = (
        entry_level + limit_pts if d == "BUY" else entry_level - limit_pts
    )
    with _lock:
        _tracks[key] = DynamicLimitTrack(
            deal_id=key,
            epic=str(epic),
            direction=d,
            entry_level=float(entry_level),
            limit_level=float(limit_level),
            limit_pts=float(limit_pts),
            updated_at=time.time(),
        )
    _publish(key)
    try:
        from runtime.trade_lifecycle import transition, LifecycleState

        transition(key, LifecycleState.DYNAMIC_LIMIT_ACTIVE, message="Dynamic limit armed")
    except Exception:
        pass


def update_from_mid(epic: str, mid: float) -> None:
    """Tighten limit when volatility expands (never widen away from profit)."""
    if mid <= 0:
        return
    with _lock:
        for key, track in list(_tracks.items()):
            if track.epic != epic:
                continue
            d = track.direction
            profit_pts = (mid - track.entry_level) if d == "BUY" else (track.entry_level - mid)
            if profit_pts > track.limit_pts * 0.5:
                new_pts = max(track.limit_pts, profit_pts * 0.8)
                if new_pts > track.limit_pts:
                    track.limit_pts = new_pts
                    track.limit_level = (
                        track.entry_level + new_pts
                        if d == "BUY"
                        else track.entry_level - new_pts
                    )
                    track.updated_at = time.time()
                    _publish(key)


def check_limit_hit(epic: str, mid: float) -> list[str]:
    """Return deal_ids that hit dynamic limit."""
    hits: list[str] = []
    if mid <= 0:
        return hits
    with _lock:
        for key, track in list(_tracks.items()):
            if track.epic != epic:
                continue
            d = track.direction
            if d == "BUY" and mid >= track.limit_level:
                hits.append(key)
            elif d == "SELL" and mid <= track.limit_level:
                hits.append(key)
    return hits


def remove_track(deal_id: str) -> None:
    with _lock:
        _tracks.pop(str(deal_id), None)


def snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "active": _active,
            "tracks": {
                k: {
                    "epic": v.epic,
                    "direction": v.direction,
                    "entry_level": v.entry_level,
                    "limit_level": v.limit_level,
                    "limit_pts": v.limit_pts,
                    "updated_at": v.updated_at,
                }
                for k, v in _tracks.items()
            },
        }


def reset_dynamic_limit_for_tests() -> None:
    global _active
    with _lock:
        _tracks.clear()
        _active = False


def _publish(deal_id: str) -> None:
    with _lock:
        track = _tracks.get(deal_id)
        if track is None:
            return
        row = {
            "epic": track.epic,
            "dynamic_limit_level": track.limit_level,
            "dynamic_limit_pts": track.limit_pts,
        }
    try:
        from system.unified_runtime_state import update_stops_limits

        update_stops_limits(
            dynamic_limit_active=True,
            deal_id=deal_id,
            trade_state=row,
        )
    except Exception:
        pass
