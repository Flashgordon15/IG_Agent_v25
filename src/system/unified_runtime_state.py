"""
Unified runtime state — thread-safe singleton for boot, feeds, routing,
execution, sizing, lifecycle, stops/limits, rejections, and GUI events.

All subsystems publish here; APIs and cockpit poll ``snapshot()``.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

_lock = threading.RLock()
_initialized = False

_MAX_EVENTS = 200
_MAX_REJECTIONS = 50

_events: deque[dict[str, Any]] = deque(maxlen=_MAX_EVENTS)
_rejections: deque[dict[str, Any]] = deque(maxlen=_MAX_REJECTIONS)

_state: dict[str, Any] = {
    "boot": {
        "stage": "A",
        "trade_ready": False,
        "subsystems": {},
    },
    "feeds": {
        "heartbeat_per_epic": {},
        "yahoo_ok": None,
        "ig_ok": None,
        "feed_heartbeat_live": False,
    },
    "routing": {
        "armed_count": 0,
        "rotation_sweep_count": 0,
        "current_epic": "",
        "rotation_active": False,
        "routing_armed": False,
    },
    "execution": {
        "loop_active": False,
        "last_dispatch_at": "",
        "pending_orders": 0,
        "execution_loop_ready": False,
    },
    "sizing": {
        "rules_loaded": False,
        "last_validation": {},
    },
    "lifecycle": {
        "active_trades": {},
    },
    "stops_limits": {
        "trailing_stop_engine_active": False,
        "dynamic_limit_engine_active": False,
        "per_trade": {},
    },
    "rejections": [],
    "startup_diagnostics": {
        "size_rules_loaded": False,
        "trailing_stop_engine_active": False,
        "dynamic_limit_engine_active": False,
        "execution_loop_ready": False,
        "ig_connectivity_validated": False,
        "rotation_logic_active": False,
        "feed_heartbeat_live": False,
        "routing_armed": False,
        "lifecycle_state_machine_initialized": False,
    },
    "updated_at": "",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def init_unified_runtime_state() -> None:
    """Idempotent init — call from server boot or post-ready."""
    global _initialized
    with _lock:
        if _initialized:
            return
        _initialized = True
        _state["updated_at"] = _utc_now_iso()
        _state["startup_diagnostics"]["lifecycle_state_machine_initialized"] = True
    emit_event("unified_state_init", {"ts": _utc_now_iso()})


def emit_event(event_type: str, payload: dict[str, Any] | None = None) -> None:
    """Append typed event for GUI polling."""
    entry = {
        "type": str(event_type),
        "ts": _utc_now_iso(),
        "payload": dict(payload or {}),
    }
    with _lock:
        _events.append(entry)


def record_rejection(
    *,
    epic: str,
    reason: str,
    classification: str,
    self_correction_attempted: bool = False,
    extra: dict[str, Any] | None = None,
) -> None:
    """Classified broker rejection — never silent."""
    rec = {
        "epic": str(epic or ""),
        "reason": str(reason or ""),
        "classification": str(classification or "UNKNOWN").upper(),
        "self_correction_attempted": bool(self_correction_attempted),
        "ts": _utc_now_iso(),
        **(extra or {}),
    }
    with _lock:
        _rejections.append(rec)
        _state["rejections"] = list(_rejections)
        _state["updated_at"] = _utc_now_iso()
    emit_event("broker_rejection", rec)
    try:
        from system.engine_log import log_engine

        log_engine(
            f"UnifiedReject: {rec['classification']} epic={epic} "
            f"reason={reason} self_correct={self_correction_attempted}"
        )
    except Exception:
        pass


def update_boot(
    *,
    stage: str | None = None,
    trade_ready: bool | None = None,
    subsystems: dict[str, Any] | None = None,
) -> None:
    with _lock:
        boot = _state["boot"]
        if stage is not None:
            boot["stage"] = stage
        if trade_ready is not None:
            boot["trade_ready"] = trade_ready
        if subsystems is not None:
            boot["subsystems"] = dict(subsystems)
        _state["updated_at"] = _utc_now_iso()


def update_feeds(
    *,
    heartbeat_per_epic: dict[str, float] | None = None,
    yahoo_ok: bool | None = None,
    ig_ok: bool | None = None,
    feed_heartbeat_live: bool | None = None,
) -> None:
    with _lock:
        feeds = _state["feeds"]
        if heartbeat_per_epic is not None:
            feeds["heartbeat_per_epic"] = dict(heartbeat_per_epic)
        if yahoo_ok is not None:
            feeds["yahoo_ok"] = yahoo_ok
        if ig_ok is not None:
            feeds["ig_ok"] = ig_ok
        if feed_heartbeat_live is not None:
            feeds["feed_heartbeat_live"] = feed_heartbeat_live
            _state["startup_diagnostics"]["feed_heartbeat_live"] = feed_heartbeat_live
        _state["updated_at"] = _utc_now_iso()


def update_routing(
    *,
    armed_count: int | None = None,
    rotation_sweep_count: int | None = None,
    current_epic: str | None = None,
    rotation_active: bool | None = None,
    routing_armed: bool | None = None,
    rotation_reason: str = "",
) -> None:
    with _lock:
        routing = _state["routing"]
        if armed_count is not None:
            routing["armed_count"] = int(armed_count)
        if rotation_sweep_count is not None:
            routing["rotation_sweep_count"] = int(rotation_sweep_count)
        if current_epic is not None:
            routing["current_epic"] = str(current_epic)
        if rotation_active is not None:
            routing["rotation_active"] = bool(rotation_active)
            _state["startup_diagnostics"]["rotation_logic_active"] = bool(rotation_active)
        if routing_armed is not None:
            routing["routing_armed"] = bool(routing_armed)
            _state["startup_diagnostics"]["routing_armed"] = bool(routing_armed)
        _state["updated_at"] = _utc_now_iso()
    if rotation_reason:
        emit_event(
            "rotation_sweep",
            {
                "sweep_count": rotation_sweep_count,
                "current_epic": current_epic,
                "reason": rotation_reason,
            },
        )


def update_execution(
    *,
    loop_active: bool | None = None,
    last_dispatch_at: str | None = None,
    pending_orders: int | None = None,
    execution_loop_ready: bool | None = None,
) -> None:
    with _lock:
        exe = _state["execution"]
        if loop_active is not None:
            exe["loop_active"] = bool(loop_active)
        if last_dispatch_at is not None:
            exe["last_dispatch_at"] = last_dispatch_at
        if pending_orders is not None:
            exe["pending_orders"] = int(pending_orders)
        if execution_loop_ready is not None:
            exe["execution_loop_ready"] = bool(execution_loop_ready)
            _state["startup_diagnostics"]["execution_loop_ready"] = bool(execution_loop_ready)
        _state["updated_at"] = _utc_now_iso()


def update_sizing(
    *,
    rules_loaded: bool | None = None,
    epic: str | None = None,
    validation: dict[str, Any] | None = None,
) -> None:
    with _lock:
        sizing = _state["sizing"]
        if rules_loaded is not None:
            sizing["rules_loaded"] = bool(rules_loaded)
            _state["startup_diagnostics"]["size_rules_loaded"] = bool(rules_loaded)
        if epic and validation is not None:
            sizing["last_validation"][str(epic)] = dict(validation)
        _state["updated_at"] = _utc_now_iso()


def update_lifecycle_trade(deal_id: str, state: str, **fields: Any) -> None:
    """Update active trade lifecycle entry keyed by deal_id."""
    key = str(deal_id or "").strip()
    if not key:
        return
    with _lock:
        trades = _state["lifecycle"]["active_trades"]
        row = dict(trades.get(key) or {})
        row["deal_id"] = key
        row["state"] = str(state)
        row["updated_at"] = _utc_now_iso()
        for k, v in fields.items():
            if v is not None:
                row[k] = v
        trades[key] = row
        if state in ("CLOSED", "REJECTED"):
            trades.pop(key, None)
        _state["updated_at"] = _utc_now_iso()
    emit_event("lifecycle_transition", {"deal_id": key, "state": state, **fields})


def update_stops_limits(
    *,
    trailing_active: bool | None = None,
    dynamic_limit_active: bool | None = None,
    deal_id: str | None = None,
    trade_state: dict[str, Any] | None = None,
) -> None:
    with _lock:
        sl = _state["stops_limits"]
        if trailing_active is not None:
            sl["trailing_stop_engine_active"] = bool(trailing_active)
            _state["startup_diagnostics"]["trailing_stop_engine_active"] = bool(trailing_active)
        if dynamic_limit_active is not None:
            sl["dynamic_limit_engine_active"] = bool(dynamic_limit_active)
            _state["startup_diagnostics"]["dynamic_limit_engine_active"] = bool(dynamic_limit_active)
        if deal_id and trade_state is not None:
            sl["per_trade"][str(deal_id)] = dict(trade_state)
        _state["updated_at"] = _utc_now_iso()


def mark_ig_connectivity(validated: bool) -> None:
    with _lock:
        _state["startup_diagnostics"]["ig_connectivity_validated"] = bool(validated)
        _state["updated_at"] = _utc_now_iso()


def update_from_health_light(hl: dict[str, Any] | None = None) -> None:
    """Sync cached health_light snapshot into unified state."""
    if hl is None:
        try:
            from api.health_light import get_health_light_response

            hl = get_health_light_response()
        except Exception:
            return
    routing = hl.get("routing_state") or {}
    armed = int(routing.get("armed") or 0)
    sweep = int(hl.get("rotation_sweep_count") or 0)
    exec_ok = bool(hl.get("execution_loop_active"))
    feed_age = hl.get("feed_heartbeat_age_ms")
    feed_live = feed_age is not None and float(feed_age) < 15000.0
    stack = hl.get("stack_tpm") or {}
    current_epic = ""
    if isinstance(stack, dict) and stack:
        try:
            current_epic = max(stack, key=lambda k: int(stack.get(k) or 0))
        except Exception:
            current_epic = next(iter(stack), "")

    update_feeds(
        yahoo_ok=hl.get("yahoo_available"),
        ig_ok=hl.get("ig_available"),
        feed_heartbeat_live=feed_live,
    )
    if hl.get("ig_available") is True:
        mark_ig_connectivity(True)
    update_routing(
        armed_count=armed,
        rotation_sweep_count=sweep,
        current_epic=current_epic,
        rotation_active=sweep > 0,
        routing_armed=armed > 0,
    )
    update_execution(
        loop_active=exec_ok,
        execution_loop_ready=exec_ok and bool(hl.get("stacked_sweep_alive")),
    )


def update_from_boot_snapshot(boot: dict[str, Any]) -> None:
    """Hook from boot orchestrator refresh."""
    subs = {s.get("id", ""): s for s in boot.get("subsystems", []) if isinstance(s, dict)}
    update_boot(
        stage=str(boot.get("current_stage") or "A"),
        trade_ready=bool(boot.get("trade_ready")),
        subsystems=subs,
    )


def snapshot() -> dict[str, Any]:
    """Full copy for API / GUI."""
    with _lock:
        return {
            "ok": True,
            "ts": _utc_now_iso(),
            "boot": dict(_state["boot"]),
            "feeds": dict(_state["feeds"]),
            "routing": dict(_state["routing"]),
            "execution": dict(_state["execution"]),
            "sizing": dict(_state["sizing"]),
            "lifecycle": {
                "active_trades": dict(_state["lifecycle"]["active_trades"]),
            },
            "stops_limits": {
                "trailing_stop_engine_active": _state["stops_limits"]["trailing_stop_engine_active"],
                "dynamic_limit_engine_active": _state["stops_limits"]["dynamic_limit_engine_active"],
                "per_trade": dict(_state["stops_limits"]["per_trade"]),
            },
            "rejections": list(_rejections),
            "startup_diagnostics": dict(_state["startup_diagnostics"]),
            "events": list(_events)[-50:],
            "updated_at": _state["updated_at"],
        }


def get_events(*, limit: int = 50) -> list[dict[str, Any]]:
    with _lock:
        return list(_events)[-limit:]


def get_rejections(*, limit: int = 20) -> list[dict[str, Any]]:
    with _lock:
        return list(_rejections)[-limit:]


def reset_unified_runtime_state_for_tests() -> None:
    """Test isolation."""
    global _initialized
    with _lock:
        _initialized = False
        _events.clear()
        _rejections.clear()
        _state["boot"] = {"stage": "A", "trade_ready": False, "subsystems": {}}
        _state["feeds"] = {
            "heartbeat_per_epic": {},
            "yahoo_ok": None,
            "ig_ok": None,
            "feed_heartbeat_live": False,
        }
        _state["routing"] = {
            "armed_count": 0,
            "rotation_sweep_count": 0,
            "current_epic": "",
            "rotation_active": False,
            "routing_armed": False,
        }
        _state["execution"] = {
            "loop_active": False,
            "last_dispatch_at": "",
            "pending_orders": 0,
            "execution_loop_ready": False,
        }
        _state["sizing"] = {"rules_loaded": False, "last_validation": {}}
        _state["lifecycle"] = {"active_trades": {}}
        _state["stops_limits"] = {
            "trailing_stop_engine_active": False,
            "dynamic_limit_engine_active": False,
            "per_trade": {},
        }
        _state["startup_diagnostics"] = {
            k: False
            for k in _state["startup_diagnostics"]
        }
        _state["rejections"] = []
        _state["updated_at"] = ""
