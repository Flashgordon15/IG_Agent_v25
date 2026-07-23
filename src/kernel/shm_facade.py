"""
Thin SHM facade — API / UI polling without disk I/O on the hot path.

Writers dual-publish alongside existing in-process structures (MemoryContext,
micro_gbp_exit tracks). Readers prefer SHM on sweep loops when attached.
"""

from __future__ import annotations

import os
import time
from typing import Any

from kernel.ring_buffer import (
    ATR_LIMIT_MULT_DEFAULT,
    PositionRingBuffer,
    get_position_ring_buffer,
    resolve_position_ring_shm_name,
)

_FACADE: dict[str, Any] = {"enabled": True, "last_error": ""}


def shm_enabled() -> bool:
    return bool(_FACADE.get("enabled", True))


def set_shm_enabled(enabled: bool) -> None:
    _FACADE["enabled"] = bool(enabled)


def _ring(*, create: bool = False) -> PositionRingBuffer | None:
    if not shm_enabled():
        return None
    name = resolve_position_ring_shm_name()
    try:
        if create:
            return PositionRingBuffer.create(name=name)
        attached = PositionRingBuffer.try_attach(name=name)
        if attached is not None:
            return attached
        if create or os.environ.get("IG_SHM_RING_CREATE", "").lower() in ("1", "true", "yes"):
            return PositionRingBuffer.create(name=name)
    except Exception as exc:
        _FACADE["last_error"] = f"{type(exc).__name__}: {exc}"
    return get_position_ring_buffer(create=create)


def publish_position_risk(
    *,
    deal_id: str,
    epic: str,
    soft_loss_gbp: float = 0.0,
    trail_floor_gbp: float = 0.0,
    atr_limit_gbp: float = 0.0,
    atr_limit_pts: float = 0.0,
    atr_mult: float = ATR_LIMIT_MULT_DEFAULT,
    pnl_gbp: float | None = None,
    peak_profit_gbp: float | None = None,
    bid: float = 0.0,
    offer: float = 0.0,
) -> int | None:
    """Dual-write helper — never raises on hot path."""
    ring = _ring()
    if ring is None:
        return None
    try:
        return ring.publish_position_snapshot(
            deal_id=deal_id,
            epic=epic,
            soft_loss_gbp=soft_loss_gbp,
            trail_floor_gbp=trail_floor_gbp,
            atr_limit_gbp=atr_limit_gbp,
            atr_limit_pts=atr_limit_pts,
            atr_mult=atr_mult,
            pnl_gbp=pnl_gbp,
            peak_profit_gbp=peak_profit_gbp,
            bid=bid,
            offer=offer,
        )
    except Exception as exc:
        _FACADE["last_error"] = f"{type(exc).__name__}: {exc}"
        return None


def publish_tick(
    *,
    epic: str,
    bid: float,
    offer: float,
) -> int | None:
    ring = _ring()
    if ring is None:
        return None
    try:
        return ring.publish_tick(epic=epic, bid=bid, offer=offer)
    except Exception as exc:
        _FACADE["last_error"] = f"{type(exc).__name__}: {exc}"
        return None


def read_position(deal_id: str) -> dict[str, Any] | None:
    ring = _ring()
    if ring is None:
        return None
    try:
        return ring.consume_latest_position(deal_id=deal_id)
    except Exception:
        return None


def read_latest_tick(epic: str | None = None) -> dict[str, Any] | None:
    ring = _ring()
    if ring is None:
        return None
    try:
        return ring.consume_latest_tick(epic=epic)
    except Exception:
        return None


def snapshot_payload() -> dict[str, Any]:
    """FastAPI / Terminal multiplex payload."""
    ring = _ring()
    if ring is None:
        return {
            "ok": False,
            "attached": False,
            "error": _FACADE.get("last_error") or "ring not attached",
            "positions": [],
            "stats": {},
        }
    try:
        positions = ring.snapshot_positions(limit=64)
        stats = ring.header_stats()
        latest_tick = ring.consume_latest_tick()
        return {
            "ok": True,
            "attached": True,
            "positions": positions,
            "latest_tick": latest_tick,
            "stats": stats,
            "ts": time.time(),
        }
    except Exception as exc:
        return {
            "ok": False,
            "attached": True,
            "error": f"{type(exc).__name__}: {exc}",
            "positions": [],
            "stats": {},
        }


def reset_shm_facade_for_tests() -> None:
    from kernel.ring_buffer import reset_ring_buffer_for_tests

    reset_ring_buffer_for_tests()
    _FACADE["last_error"] = ""
    _FACADE["enabled"] = True
