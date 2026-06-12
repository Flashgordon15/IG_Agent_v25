"""Atomic limit entry + 200ms protection verify + emergency flatten."""

from __future__ import annotations

import time
from typing import Any

from execution.scalping.config import scalping_settings
from execution.scalping.entry_halt import halt_entries
from ig_api.exceptions import IGAPIError, IGOrderError
from system.demo_execution_trace import trace_execution
from system.engine_log import log_engine

_REJECT_PATTERNS = (
    "REJECTED",
    "INVALID_STOPS",
    "INVALID_STOP",
    "error.trading",
    "STOP",
)


def _touch_level(direction: str, bid: float, offer: float) -> float:
    d = str(direction or "").upper()
    if d == "BUY":
        return float(offer)
    return float(bid)


def position_has_stop_protection(client: Any, deal_id: str) -> bool:
    """True when open deal has a stop attached (fail-safe requirement)."""
    if not deal_id or client is None:
        return False
    try:
        row = (
            client.find_open_position(deal_id)
            if hasattr(client, "find_open_position")
            else None
        )
        if not row:
            return False
        pos = row.get("position") or {}
        return float(pos.get("stopLevel") or 0) > 0 or float(
            pos.get("stopDistance") or 0
        ) > 0
    except Exception as e:
        log_engine(f"EXEC_PROTECT stop check failed deal={deal_id}: {e}")
        return False


def position_has_full_protection(client: Any, deal_id: str) -> bool:
    if not deal_id or client is None:
        return False
    try:
        if hasattr(client, "position_protection_status"):
            return bool(client.position_protection_status(deal_id))
        row = client.find_open_position(deal_id) if hasattr(client, "find_open_position") else None
        if not row:
            return False
        pos = row.get("position") or {}
        has_stop = float(pos.get("stopLevel") or 0) > 0 or float(
            pos.get("stopDistance") or 0
        ) > 0
        has_limit = float(pos.get("limitLevel") or 0) > 0 or float(
            pos.get("limitDistance") or 0
        ) > 0
        return has_stop and has_limit
    except Exception as e:
        log_engine(f"SCALPING protection check failed deal={deal_id}: {e}")
        return False


def verify_protection_or_emergency(
    client: Any,
    *,
    deal_id: str,
    epic: str,
    direction: str,
    size: float,
    stop_distance: float,
    limit_distance: float,
    verify_ms: int | None = None,
) -> bool:
    """
    Poll broker for SL+TP within verify_ms. On failure: emergency market close + halt entries.
    Returns True when position is fully protected.
    """
    settings = scalping_settings()
    deadline_ms = int(verify_ms if verify_ms is not None else settings.get("protection_verify_ms", 200))
    start = time.monotonic()

    log_engine(
        f"SCALPING protection verify start deal={deal_id} epic={epic} "
        f"deadline={deadline_ms}ms"
    )

    while (time.monotonic() - start) * 1000.0 < deadline_ms:
        if position_has_full_protection(client, deal_id):
            log_engine(f"SCALPING protection OK deal={deal_id}")
            return True
        if hasattr(client, "ensure_protective_stops"):
            try:
                client.ensure_protective_stops(
                    deal_id,
                    epic=epic,
                    stop_distance=float(stop_distance),
                    limit_distance=float(limit_distance),
                )
            except Exception as e:
                log_engine(f"SCALPING attach stops retry failed: {e}")
        time.sleep(0.02)

    if position_has_full_protection(client, deal_id):
        log_engine(f"SCALPING protection OK (final) deal={deal_id}")
        return True

    log_engine(
        f"SCALPING PROTECTION FAIL deal={deal_id} — emergency market close"
    )
    emergency_close_and_halt(
        client,
        deal_id=deal_id,
        epic=epic,
        direction=direction,
        size=size,
        reason=f"SL/TP not attached within {deadline_ms}ms",
    )
    return False


def emergency_close_position(
    client: Any,
    *,
    deal_id: str,
    epic: str,
    direction: str,
    size: float,
    reason: str,
) -> None:
    """Market-close a single unprotected position."""
    close_dir = "SELL" if str(direction).upper() == "BUY" else "BUY"
    try:
        if hasattr(client, "close_position") and deal_id:
            client.close_position(
                deal_id,
                direction=close_dir,
                size=float(size),
                epic=epic,
                verify=True,
            )
            log_engine(f"EXEC_PROTECT emergency close deal={deal_id} — {reason}")
        elif hasattr(client, "flatten_epic_positions") and epic:
            client.flatten_epic_positions(epic)
            log_engine(f"EXEC_PROTECT emergency flatten epic={epic} — {reason}")
    except Exception as e:
        log_engine(f"EXEC_PROTECT emergency close error: {type(e).__name__}: {e}")


def emergency_close_and_halt(
    client: Any,
    *,
    deal_id: str,
    epic: str,
    direction: str,
    size: float,
    reason: str,
) -> None:
    emergency_close_position(
        client,
        deal_id=deal_id,
        epic=epic,
        direction=direction,
        size=size,
        reason=reason,
    )
    if scalping_settings().get("halt_entries_on_protection_failure", True):
        halt_entries(reason)


def verify_stop_or_emergency(
    client: Any,
    *,
    deal_id: str,
    epic: str,
    direction: str,
    size: float,
    stop_distance: float,
    verify_ms: int = 200,
    halt_all_entries: bool = False,
) -> bool:
    """Poll for stop attachment; close position if missing after verify_ms."""
    import time

    start = time.monotonic()
    log_engine(
        f"EXEC_PROTECT stop verify deal={deal_id} epic={epic} deadline={verify_ms}ms"
    )
    while (time.monotonic() - start) * 1000.0 < verify_ms:
        if position_has_stop_protection(client, deal_id):
            log_engine(f"EXEC_PROTECT stop OK deal={deal_id}")
            return True
        if hasattr(client, "ensure_protective_stops"):
            try:
                client.ensure_protective_stops(
                    deal_id,
                    epic=epic,
                    stop_distance=float(stop_distance),
                    limit_distance=0.0,
                )
            except Exception as e:
                log_engine(f"EXEC_PROTECT stop attach retry failed: {e}")
        time.sleep(0.02)

    if position_has_stop_protection(client, deal_id):
        return True

    reason = f"Stop loss not attached within {verify_ms}ms"
    log_engine(f"EXEC_PROTECT STOP FAIL deal={deal_id} — {reason}")
    emergency_close_position(
        client,
        deal_id=deal_id,
        epic=epic,
        direction=direction,
        size=size,
        reason=reason,
    )
    if halt_all_entries:
        halt_entries(reason)
    return False


def _is_protection_reject(exc: BaseException) -> bool:
    msg = str(exc).upper()
    return any(p in msg for p in _REJECT_PATTERNS)


def submit_atomic_limit_entry(
    client: Any,
    *,
    epic: str,
    direction: str,
    size: float,
    bid: float,
    offer: float,
    stop_distance: float,
    limit_distance: float,
    currency_code: str,
) -> dict[str, Any]:
    """
    Submit unified LIMIT-at-touch payload with stopDistance + limitDistance.
    Raises IGOrderError on broker rejection.
    """
    level = _touch_level(direction, bid, offer)
    trace_execution(
        "REST",
        "scalping.submit_atomic_limit_entry",
        decision="POST limit at touch",
        params={
            "epic": epic,
            "direction": direction,
            "level": level,
            "size": size,
            "stopDistance": stop_distance,
            "limitDistance": limit_distance,
        },
    )
    log_engine(
        f"SCALPING atomic limit entry {direction} {epic} level={level:.2f} "
        f"size={size} stop={stop_distance} limit={limit_distance}"
    )
    if not hasattr(client, "place_limit_entry_atomic"):
        raise IGOrderError("Broker client missing place_limit_entry_atomic")
    try:
        return client.place_limit_entry_atomic(
            epic=epic,
            direction=direction,
            size=float(size),
            level=level,
            stop_distance=float(stop_distance),
            limit_distance=float(limit_distance),
            currency_code=currency_code,
        )
    except (IGOrderError, IGAPIError) as e:
        if _is_protection_reject(e):
            log_engine(f"SCALPING broker reject (protection): {e}")
            if scalping_settings().get("halt_entries_on_protection_failure", True):
                halt_entries(f"Broker rejected stops: {e}")
        raise
