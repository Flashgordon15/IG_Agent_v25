"""
Stale-cache fallback circuit breaker for boot SoT hydration.

When the desk is booting and a network stall ages ``trade_support_sot`` past
budget, hydrate the boot gate from a *verified* local ``broker_snapshot.json``
instead of hard-freezing on GATE HOLD / latency hold.

Never invents open positions from empty/missing stubs — missing or invalid
snapshots fail soft with a documented reason so the gate can advance as warn
rather than infinite freeze.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from system.engine_log import log_engine

_LOCK = threading.RLock()
_ARMED = False
_ACTIVE = False
_LAST: dict[str, Any] = {}
_ARMED_AT = 0.0


def arm_boot_fallback_circuit(*, reason: str = "boot_start") -> dict[str, Any]:
    """Arm the circuit at process start (main / post_ready)."""
    global _ARMED, _ARMED_AT, _ACTIVE, _LAST
    with _LOCK:
        _ARMED = True
        _ARMED_AT = time.time()
        _ACTIVE = False
        _LAST = {
            "armed": True,
            "active": False,
            "reason": reason,
            "armed_at": _ARMED_AT,
        }
    try:
        log_engine(f"boot_sot_fallback: armed ({reason})")
    except Exception:
        pass
    return dict(_LAST)


def reset_boot_sot_fallback_for_tests() -> None:
    global _ARMED, _ACTIVE, _LAST, _ARMED_AT
    with _LOCK:
        _ARMED = False
        _ACTIVE = False
        _LAST = {}
        _ARMED_AT = 0.0


def boot_fallback_state() -> dict[str, Any]:
    with _LOCK:
        return {
            "armed": _ARMED,
            "active": _ACTIVE,
            "armed_at": _ARMED_AT,
            **dict(_LAST),
        }


def verify_broker_snapshot_for_boot(
    snap: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Validate local snapshot is usable for boot hydration.

    A verified *flat* book (count=0, positions=[]) is OK.
    Empty stubs / corrupt / missing structure are rejected.
    """
    if not isinstance(snap, dict) or not snap:
        return {
            "ok": False,
            "reason": "snapshot_missing_or_empty_stub",
            "broker_open": None,
            "age_sec": None,
        }
    try:
        ts = float(snap.get("ts") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    if ts <= 0:
        return {
            "ok": False,
            "reason": "snapshot_missing_ts",
            "broker_open": None,
            "age_sec": None,
        }
    positions = snap.get("positions")
    if positions is not None and not isinstance(positions, list):
        return {
            "ok": False,
            "reason": "snapshot_positions_invalid",
            "broker_open": None,
            "age_sec": snap.get("age_sec"),
        }
    # Reject hollow placeholder objects with neither count nor positions key.
    if "count" not in snap and positions is None:
        return {
            "ok": False,
            "reason": "snapshot_structure_incomplete",
            "broker_open": None,
            "age_sec": snap.get("age_sec"),
        }
    try:
        if snap.get("count") is not None:
            count = int(snap.get("count"))
        else:
            count = len(positions or [])
    except (TypeError, ValueError):
        return {
            "ok": False,
            "reason": "snapshot_count_invalid",
            "broker_open": None,
            "age_sec": snap.get("age_sec"),
        }
    if count < 0:
        return {
            "ok": False,
            "reason": "snapshot_count_negative",
            "broker_open": None,
            "age_sec": snap.get("age_sec"),
        }
    # Never invent opens: if count>0 every row must have deal_id+epic.
    rows = list(positions or [])
    if count > 0:
        if len(rows) <= 0:
            return {
                "ok": False,
                "reason": "snapshot_count_without_rows",
                "broker_open": None,
                "age_sec": snap.get("age_sec"),
            }
        for row in rows:
            if not isinstance(row, dict):
                return {
                    "ok": False,
                    "reason": "snapshot_row_invalid",
                    "broker_open": None,
                    "age_sec": snap.get("age_sec"),
                }
            deal = str(row.get("deal_id") or "").strip()
            epic = str(row.get("epic") or "").strip()
            if not deal or not epic:
                return {
                    "ok": False,
                    "reason": "snapshot_row_missing_identity",
                    "broker_open": None,
                    "age_sec": snap.get("age_sec"),
                }
    age = snap.get("age_sec")
    try:
        age_f = float(age) if age is not None else max(0.0, time.time() - ts)
    except (TypeError, ValueError):
        age_f = max(0.0, time.time() - ts)
    return {
        "ok": True,
        "reason": "verified_broker_snapshot",
        "broker_open": count,
        "age_sec": round(age_f, 2),
        "source": str(snap.get("source") or "broker_snapshot"),
        "path": snap.get("_path"),
    }


def read_verified_boot_snapshot(
    *,
    max_age_sec: float | None = None,
) -> dict[str, Any]:
    """Load + verify broker_snapshot via system.paths / broker_snapshot helpers."""
    try:
        from runtime import broker_snapshot

        snap = broker_snapshot.read_snapshot(max_age_sec=max_age_sec)
    except Exception as exc:
        return {
            "ok": False,
            "reason": f"snapshot_read_error:{type(exc).__name__}",
            "broker_open": None,
            "age_sec": None,
        }
    verified = verify_broker_snapshot_for_boot(snap)
    if verified.get("ok") and snap is not None:
        verified["snapshot"] = snap
    return verified


def should_activate_boot_sot_fallback(
    *,
    booting: bool,
    sot_age_sec: float | None,
    stale_budget_sec: float,
    network_timeout: bool = False,
) -> bool:
    """Activate when booting and SoT age crossed budget (or explicit network stall)."""
    if not booting:
        return False
    if network_timeout:
        return True
    if sot_age_sec is None:
        return True
    try:
        return float(sot_age_sec) >= float(stale_budget_sec)
    except (TypeError, ValueError):
        return True


def resolve_boot_sot_fallback(
    *,
    booting: bool,
    sot_age_sec: float | None,
    stale_budget_sec: float,
    network_timeout: bool = False,
    sot_ok: bool = False,
    broker_open: int | None = None,
) -> dict[str, Any]:
    """
    Resolve SoT plane during boot — snapshot hydrate or soft-fail warn.

    Returns fields suitable for StabilityComponents / boot_gate checks.
    """
    global _ACTIVE, _LAST

    base: dict[str, Any] = {
        "fallback_active": False,
        "fallback_reason": None,
        "sot_ok": bool(sot_ok),
        "sot_source": "trade_support" if sot_ok else "unknown",
        "sot_age_sec": sot_age_sec,
        "broker_open": broker_open,
        "gate_status": "pass" if sot_ok else "fail",
        "gate_detail": None,
        "soft_fail": False,
    }

    if sot_ok and not network_timeout:
        return base

    if not should_activate_boot_sot_fallback(
        booting=booting,
        sot_age_sec=sot_age_sec,
        stale_budget_sec=stale_budget_sec,
        network_timeout=network_timeout,
    ):
        return base

    verified = read_verified_boot_snapshot(max_age_sec=None)
    if verified.get("ok"):
        with _LOCK:
            _ACTIVE = True
            _LAST = {
                "armed": _ARMED,
                "active": True,
                "reason": "network_stall_broker_snapshot_hydrate",
                "verified": {
                    k: verified.get(k)
                    for k in ("broker_open", "age_sec", "source", "path", "reason")
                },
                "ts": time.time(),
            }
        try:
            log_engine(
                "boot_sot_fallback: hydrated from broker_snapshot "
                f"opens={verified.get('broker_open')} age={verified.get('age_sec')}"
            )
        except Exception:
            pass
        return {
            "fallback_active": True,
            "fallback_reason": "broker_snapshot_boot_hydrate",
            "sot_ok": True,
            "sot_source": "broker_snapshot_boot_fallback",
            "sot_age_sec": verified.get("age_sec"),
            "broker_open": verified.get("broker_open"),
            "gate_status": "pass",
            "gate_detail": (
                f"boot_fallback broker_snapshot age={verified.get('age_sec')}s "
                f"opens={verified.get('broker_open')}"
            ),
            "soft_fail": False,
            "verified": verified,
        }

    # Soft fail — document reason; during boot use warn (not infinite freeze).
    reason = str(verified.get("reason") or "snapshot_unavailable")
    with _LOCK:
        _ACTIVE = True
        _LAST = {
            "armed": _ARMED,
            "active": True,
            "reason": reason,
            "soft_fail": True,
            "ts": time.time(),
        }
    try:
        log_engine(f"boot_sot_fallback: soft_fail ({reason})")
    except Exception:
        pass
    return {
        "fallback_active": True,
        "fallback_reason": reason,
        "sot_ok": False,
        "sot_source": "boot_fallback_soft_fail",
        "sot_age_sec": sot_age_sec,
        "broker_open": broker_open if broker_open is not None else 0,
        "gate_status": "warn" if booting else "fail",
        "gate_detail": f"boot_fallback soft_fail:{reason} — gate advances without freeze",
        "soft_fail": True,
        "verified": verified,
    }
