"""Lock-free gate completion sideband — unblocks boot when hydration threads wedge."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from system.system_state import GateId, SystemState

_sideband: set[str] = set()
_lock = threading.Lock()


def mark_gate_sideband(gate_id: str, *, detail: str = "") -> None:
    with _lock:
        _sideband.add(str(gate_id))
    try:
        from system.system_state import GateStatus, get_system_state

        state = get_system_state()
        if state.try_gate_complete(gate_id, timeout=0.25):
            return
        if state._lock.acquire(timeout=0.5):
            try:
                if state._snapshot.gates[gate_id].status != GateStatus.COMPLETE:
                    state.mark_gate_complete(gate_id, detail=detail or "sideband")
            finally:
                state._lock.release()
    except Exception:
        pass


def is_gate_sideband(gate_id: str) -> bool:
    with _lock:
        return str(gate_id) in _sideband


def clear_gate_sideband_for_tests() -> None:
    with _lock:
        _sideband.clear()


def gate_is_done(state: SystemState, gate_id: str) -> bool:
    """True when sideband or SystemState reports gate complete (non-blocking)."""
    if is_gate_sideband(gate_id):
        return True
    return state.try_gate_complete(gate_id, timeout=0)


def gate_status(state: SystemState, gate_id: str) -> str:
    snap = state.try_snapshot(timeout=0.25)
    if snap is None:
        if is_gate_sideband(gate_id):
            return "complete"
        return "unknown"
    return str((snap.get("gates") or {}).get(gate_id, {}).get("status") or "")
