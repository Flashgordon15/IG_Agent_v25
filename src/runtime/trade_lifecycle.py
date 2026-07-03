"""
Trade lifecycle state machine — full deterministic states for GUI + APIs.

States:
  SIGNAL_DETECTED → PRE_TRADE_VALIDATION → ORDER_SUBMITTED → ORDER_ACCEPTED
  → ACTIVE → TRAILING_STOP_ACTIVE / DYNAMIC_LIMIT_ACTIVE → EXIT_PENDING → EXIT_FILLED
  Terminal: REJECTED, CANCELLED, ERROR

Legacy names (PENDING, CONFIRMED, ARMED_STOP, CLOSING, CLOSED) map to modern states.
"""

from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Any

from system.engine_log import log_engine

_lock = threading.RLock()

_LEGACY_TO_MODERN: dict[str, str] = {
    "PENDING": "ORDER_SUBMITTED",
    "CONFIRMED": "ORDER_ACCEPTED",
    "ARMED_STOP": "TRAILING_STOP_ACTIVE",
    "CLOSING": "EXIT_PENDING",
    "CLOSED": "EXIT_FILLED",
}

_TRANSITIONS: dict[str, set[str]] = {
    "SIGNAL_DETECTED": {"PRE_TRADE_VALIDATION", "REJECTED", "ERROR", "CANCELLED"},
    "PRE_TRADE_VALIDATION": {"ORDER_SUBMITTED", "REJECTED", "ERROR"},
    "ORDER_SUBMITTED": {"ORDER_ACCEPTED", "REJECTED", "ERROR", "CANCELLED"},
    "ORDER_ACCEPTED": {"ACTIVE", "TRAILING_STOP_ACTIVE", "REJECTED", "ERROR"},
    "ACTIVE": {
        "TRAILING_STOP_ACTIVE",
        "DYNAMIC_LIMIT_ACTIVE",
        "EXIT_PENDING",
        "REJECTED",
        "ERROR",
    },
    "TRAILING_STOP_ACTIVE": {
        "DYNAMIC_LIMIT_ACTIVE",
        "ACTIVE",
        "EXIT_PENDING",
        "EXIT_FILLED",
        "ERROR",
    },
    "DYNAMIC_LIMIT_ACTIVE": {
        "TRAILING_STOP_ACTIVE",
        "ACTIVE",
        "EXIT_PENDING",
        "EXIT_FILLED",
        "ERROR",
    },
    "EXIT_PENDING": {"EXIT_FILLED", "REJECTED", "ERROR", "CANCELLED"},
    "EXIT_FILLED": set(),
    "REJECTED": set(),
    "CANCELLED": set(),
    "ERROR": set(),
}


class LifecycleState(str, Enum):
    SIGNAL_DETECTED = "SIGNAL_DETECTED"
    PRE_TRADE_VALIDATION = "PRE_TRADE_VALIDATION"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_ACCEPTED = "ORDER_ACCEPTED"
    ACTIVE = "ACTIVE"
    TRAILING_STOP_ACTIVE = "TRAILING_STOP_ACTIVE"
    DYNAMIC_LIMIT_ACTIVE = "DYNAMIC_LIMIT_ACTIVE"
    EXIT_PENDING = "EXIT_PENDING"
    EXIT_FILLED = "EXIT_FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"
    # Legacy aliases (same string values as modern where applicable)
    PENDING = "ORDER_SUBMITTED"
    CONFIRMED = "ORDER_ACCEPTED"
    ARMED_STOP = "TRAILING_STOP_ACTIVE"
    CLOSING = "EXIT_PENDING"
    CLOSED = "EXIT_FILLED"


_trades: dict[str, dict[str, Any]] = {}
_history: list[dict[str, Any]] = []
_MAX_HISTORY = 50


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _normalize_state(state: str) -> str:
    s = str(state or "ORDER_SUBMITTED").upper()
    return _LEGACY_TO_MODERN.get(s, s)


def _can_transition(current: str, target: str) -> bool:
    cur = _normalize_state(current)
    tgt = _normalize_state(target)
    if cur == tgt:
        return True
    allowed = _TRANSITIONS.get(cur, set())
    return tgt in allowed


def _log_transition(key: str, cur: str, tgt: str, message: str) -> None:
    log_engine(f"Lifecycle: {key} {cur}->{tgt} {message}".strip())
    try:
        from system.unified_runtime_state import emit_event

        emit_event(
            "lifecycle_transition",
            {"deal_id": key, "from": cur, "to": tgt, "message": message},
        )
    except Exception:
        pass


def signal_detected(
    *,
    epic: str,
    direction: str,
    deal_id: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    key = str(deal_id or f"{epic}:{direction}:{int(time.time() * 1000)}")
    row = {
        "deal_id": key,
        "epic": str(epic),
        "direction": str(direction).upper(),
        "size": 0.0,
        "state": LifecycleState.SIGNAL_DETECTED.value,
        "started_at": _now_iso(),
        "updated_at": _now_iso(),
        "transitions": [
            {"from": "", "to": LifecycleState.SIGNAL_DETECTED.value, "ts": _now_iso()}
        ],
        **(extra or {}),
    }
    with _lock:
        _trades[key] = row
    _publish(key, row)
    _emit_bus_signal(epic, direction)
    return dict(row)


def begin_trade(
    *,
    deal_id: str,
    epic: str,
    direction: str,
    size: float,
    ref: str = "",
) -> dict[str, Any]:
    """Register at ORDER_SUBMITTED (legacy begin_trade entry)."""
    key = str(deal_id or ref or f"{epic}:{int(time.time() * 1000)}")
    existing = None
    with _lock:
        existing = _trades.get(key)
    if existing is None:
        signal_detected(epic=epic, direction=direction, deal_id=key)
        transition(key, LifecycleState.PRE_TRADE_VALIDATION, message="Size validation")
    transition(
        key,
        LifecycleState.ORDER_SUBMITTED,
        message="Order dispatch",
        extra={"size": float(size), "ref": ref},
    )
    with _lock:
        row = dict(_trades.get(key) or {})
    return row


def transition(
    deal_id: str,
    target_state: str | LifecycleState,
    *,
    message: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    key = str(deal_id or "").strip()
    if not key:
        return None
    tgt = _normalize_state(
        str(target_state.value if isinstance(target_state, LifecycleState) else target_state)
    )
    with _lock:
        row = _trades.get(key)
        if row is None:
            return None
        cur = _normalize_state(str(row.get("state") or "ORDER_SUBMITTED"))
        if not _can_transition(cur, tgt):
            log_engine(f"Lifecycle: invalid transition deal={key} {cur}->{tgt} msg={message}")
            return dict(row)
        row = dict(row)
        row["state"] = tgt
        row["updated_at"] = _now_iso()
        if message:
            row["last_message"] = message
        if extra:
            row.update({k: v for k, v in extra.items() if v is not None})
        transitions = list(row.get("transitions") or [])
        transitions.append({"from": cur, "to": tgt, "ts": _now_iso(), "message": message})
        row["transitions"] = transitions[-30:]
        terminal = {LifecycleState.EXIT_FILLED.value, LifecycleState.REJECTED.value,
                    LifecycleState.CANCELLED.value, LifecycleState.ERROR.value}
        if tgt in terminal:
            _history.append(dict(row))
            if len(_history) > _MAX_HISTORY:
                _history.pop(0)
            _trades.pop(key, None)
        else:
            _trades[key] = row
    _log_transition(key, cur, tgt, message)
    _publish(key, row)
    _emit_bus_for_state(row, tgt, message)
    if tgt == LifecycleState.TRAILING_STOP_ACTIVE.value:
        _arm_virtual_stop(row)
    return dict(row)


def _publish(deal_id: str, row: dict[str, Any]) -> None:
    try:
        from system.unified_runtime_state import update_lifecycle_trade

        update_lifecycle_trade(
            deal_id,
            str(row.get("state") or ""),
            epic=row.get("epic"),
            direction=row.get("direction"),
            size=row.get("size"),
            message=row.get("last_message", ""),
        )
    except Exception:
        pass


def _emit_bus_signal(epic: str, direction: str) -> None:
    try:
        from system.trade_lifecycle_bus import get_lifecycle_bus

        bus = get_lifecycle_bus()
        bus.begin_trade(epic=epic, direction=direction)
    except Exception:
        pass


def _emit_bus_for_state(row: dict[str, Any], state: str, message: str) -> None:
    try:
        from system.trade_lifecycle_bus import (
            STAGE_IG_RESPONSE,
            STAGE_POSITION_OPENED,
            STAGE_POSITION_TRACKING,
            STATUS_FAIL,
            STATUS_OK,
            get_lifecycle_bus,
        )

        bus = get_lifecycle_bus()
        epic = str(row.get("epic") or "")
        direction = str(row.get("direction") or "")
        deal_id = str(row.get("deal_id") or "")
        if state == LifecycleState.ORDER_ACCEPTED.value:
            bus.emit(STAGE_IG_RESPONSE, STATUS_OK, message or "IG accepted", deal_id=deal_id)
        elif state == LifecycleState.REJECTED.value:
            bus.emit(STAGE_IG_RESPONSE, STATUS_FAIL, message or "IG rejected")
            bus.finalize_failure(reason=message or "rejected")
            try:
                from system.unified_runtime_state import emit_event

                emit_event(
                    "ig_rejection",
                    {"epic": epic, "reason": message, "deal_id": deal_id},
                )
            except Exception:
                pass
        elif state in (
            LifecycleState.ACTIVE.value,
            LifecycleState.TRAILING_STOP_ACTIVE.value,
            LifecycleState.DYNAMIC_LIMIT_ACTIVE.value,
        ):
            bus.emit(
                STAGE_POSITION_OPENED,
                STATUS_OK,
                message or f"Position active ({state})",
                deal_id=deal_id,
            )
            bus.emit(STAGE_POSITION_TRACKING, STATUS_OK, "Tracking stops/limits")
        elif state == LifecycleState.EXIT_FILLED.value:
            bus.mark_position_closed(
                message=message or "Position closed",
                epic=epic,
                direction=direction,
                deal_id=deal_id,
            )
    except Exception:
        pass


def _arm_virtual_stop(row: dict[str, Any]) -> None:
    try:
        from execution.post_fill_risk_controls import arm_post_fill_risk_controls
        from system.config_loader import get_config

        entry = float(row.get("entry_level") or 0.0)
        if entry <= 0:
            return
        cfg = get_config()
        stop_pts = float(
            row.get("stop_distance_pts")
            or row.get("broker_stop_pts")
            or getattr(cfg, "stop_distance_points", 10.0)
            or 10.0
        )
        arm_post_fill_risk_controls(
            epic=str(row.get("epic") or ""),
            direction=str(row.get("direction") or "BUY"),
            size=float(row.get("size") or 0.0),
            entry_level=entry,
            deal_id=str(row.get("deal_id") or ""),
            stop_distance_pts=stop_pts,
            limit_distance_pts=float(row.get("limit_distance_pts") or 0.0) or None,
            cfg=cfg,
        )
    except Exception as exc:
        log_engine(f"Lifecycle: virtual stop arm failed: {type(exc).__name__}: {exc}")


def snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "active": dict(_trades),
            "history": list(reversed(_history[-30:])),
        }


def get_trade_events(*, limit: int = 50) -> list[dict[str, Any]]:
    try:
        from system.unified_runtime_state import get_events

        return get_events(limit=limit)
    except Exception:
        return []


def reset_trade_lifecycle_for_tests() -> None:
    with _lock:
        _trades.clear()
        _history.clear()
