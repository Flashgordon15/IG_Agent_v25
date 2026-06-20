"""v30 Apex — thread-safe array warmup progress (256-bar float64 rings)."""

from __future__ import annotations

import threading
from typing import Any, Literal

RING_TARGET_BARS = 256

WarmupStatus = Literal["idle", "warming", "ready", "failed"]

_lock = threading.RLock()
_done = threading.Event()
_status: WarmupStatus = "idle"
_bars_compiled = 0
_bars_target = RING_TARGET_BARS
_percent = 0
_epic = ""
_detail = ""


def reset_warmup_progress(*, bars_target: int = RING_TARGET_BARS) -> None:
    global _status, _bars_compiled, _bars_target, _percent, _epic, _detail
    with _lock:
        _status = "warming"
        _bars_compiled = 0
        _bars_target = max(1, int(bars_target))
        _percent = 0
        _epic = ""
        _detail = "Compiling Vector Arrays"
    _done.clear()


def update_warmup_progress(
    *,
    bars_compiled: int,
    bars_target: int | None = None,
    epic: str = "",
    detail: str = "",
) -> dict[str, Any]:
    """Publish incremental warmup state and fan-out over IPC."""
    global _bars_compiled, _bars_target, _percent, _epic, _detail, _status
    with _lock:
        if _status != "ready":
            _status = "warming"
        _bars_compiled = max(0, int(bars_compiled))
        if bars_target is not None:
            _bars_target = max(1, int(bars_target))
        _percent = min(100, int((_bars_compiled * 100) / _bars_target))
        if epic:
            _epic = str(epic)
        if detail:
            _detail = str(detail)
        snap = _snapshot_unlocked()
    _broadcast(snap)
    return snap


def mark_warmup_ready() -> dict[str, Any]:
    try:
        from apex.microkernel import get_microkernel

        get_microkernel().flush_deferred_live_ticks()
    except Exception:
        pass
    global _status, _percent, _bars_compiled, _bars_target, _detail
    with _lock:
        _status = "ready"
        _bars_compiled = _bars_target
        _percent = 100
        _detail = "Vector arrays compiled"
        snap = _snapshot_unlocked()
    _done.set()
    _broadcast(snap)
    return snap


def mark_warmup_failed(error: str) -> dict[str, Any]:
    global _status, _detail
    with _lock:
        _status = "failed"
        _detail = str(error or "warmup failed")
        snap = _snapshot_unlocked()
    _done.set()
    _broadcast(snap)
    return snap


def is_warmup_complete() -> bool:
    with _lock:
        return _status == "ready"


def is_warmup_active() -> bool:
    with _lock:
        return _status == "warming"


def wait_until_warmup_ready(timeout: float | None = None) -> bool:
    if is_warmup_complete():
        return True
    with _lock:
        status = _status
    if status == "idle":
        return True
    if status == "failed":
        return False
    return _done.wait(timeout=timeout)


def get_warmup_snapshot() -> dict[str, Any]:
    with _lock:
        return _snapshot_unlocked()


def _snapshot_unlocked() -> dict[str, Any]:
    return {
        "status": _status,
        "ready": _status == "ready",
        "warming": _status == "warming",
        "bars_compiled": int(_bars_compiled),
        "bars_target": int(_bars_target),
        "percent": int(_percent),
        "epic": _epic,
        "detail": _detail,
        "label": (
            f"Compiling Vector Arrays: {_bars_compiled} / {_bars_target} Bars… "
            f"[{_percent}%]"
            if _status == "warming"
            else _detail or "Vector arrays ready"
        ),
    }


def _broadcast(snap: dict[str, Any]) -> None:
    try:
        from apex.ipc_bridge import broadcast_warmup_event

        broadcast_warmup_event(snap)
    except Exception:
        pass
    try:
        from system.system_state import BootPhase, get_system_state

        if snap.get("status") == "warming":
            get_system_state().update_state(
                BootPhase.WARMING,
                int(snap.get("percent") or 0),
                str(snap.get("label") or "Compiling Vector Arrays"),
                ready=False,
            )
        elif snap.get("status") == "failed":
            get_system_state().update_state(
                BootPhase.FAILED,
                int(snap.get("percent") or 0),
                str(snap.get("detail") or "Array warmup failed"),
                ready=False,
                error=str(snap.get("detail") or "Array warmup failed"),
            )
    except Exception:
        pass


def reset_warmup_for_tests() -> None:
    global _status, _bars_compiled, _bars_target, _percent, _epic, _detail
    with _lock:
        _status = "idle"
        _bars_compiled = 0
        _bars_target = RING_TARGET_BARS
        _percent = 0
        _epic = ""
        _detail = ""
    _done.clear()
