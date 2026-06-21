"""
Alpha matrix compiler pulse + SHM watchdog — binds :9199 compiler to :8080 live lookup.

Shadow track emits a hardware pulse every 100ms. If the pulse stalls, the watchdog
instantly unlinks ``ig_agent_v30_alpha_matrix`` so live naked-pointer lookups
fail into the 65% protective lockout.
"""

from __future__ import annotations

import struct
import threading
import time
from typing import Any

from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception

PULSE_SHM_NAME = "ig_agent_v30_alpha_pulse"
PULSE_INTERVAL_SEC = 0.1
PULSE_CHECK_SEC = 0.05
PULSE_MISS_CYCLES = 3
_PULSE_STRUCT = struct.Struct("!QQ")
_PULSE_BYTES = _PULSE_STRUCT.size

_WATCHDOG_THREAD: threading.Thread | None = None
_WATCHDOG_STOP = threading.Event()
_WATCHDOG_STATE: dict[str, Any] = {
    "running": False,
    "last_seq": 0,
    "miss_cycles": 0,
    "shm_state": "unknown",
    "last_action": "",
    "unmap_count": 0,
}


def _pulse_shm(*, create: bool = False):
    from multiprocessing import shared_memory

    if create:
        try:
            existing = shared_memory.SharedMemory(name=PULSE_SHM_NAME, create=False)
            existing.close()
            existing.unlink()
        except FileNotFoundError:
            pass
        return shared_memory.SharedMemory(
            name=PULSE_SHM_NAME, create=True, size=_PULSE_BYTES
        )
    return shared_memory.SharedMemory(name=PULSE_SHM_NAME, create=False)


def emit_compiler_pulse() -> int:
    """Shadow compiler hardware pulse — increment sequence every 100ms."""
    try:
        shm = _pulse_shm(create=False)
    except FileNotFoundError:
        shm = _pulse_shm(create=True)
    try:
        raw = bytes(shm.buf[:_PULSE_BYTES])
        seq, _ = _PULSE_STRUCT.unpack(raw) if len(raw) == _PULSE_BYTES else (0, 0)
        seq += 1
        shm.buf[:_PULSE_BYTES] = _PULSE_STRUCT.pack(seq, 0)
        return int(seq)
    finally:
        shm.close()


def read_compiler_pulse() -> tuple[int, bool]:
    """Return (sequence, mapped)."""
    try:
        shm = _pulse_shm(create=False)
    except FileNotFoundError:
        return 0, False
    try:
        raw = bytes(shm.buf[:_PULSE_BYTES])
        if len(raw) != _PULSE_BYTES:
            return 0, True
        seq, _ = _PULSE_STRUCT.unpack(raw)
        return int(seq), True
    finally:
        shm.close()


def _compiler_pulse_loop() -> None:
    while not _WATCHDOG_STOP.is_set():
        try:
            emit_compiler_pulse()
        except Exception as exc:
            log_guarded_exception("alpha_matrix_pulse", exc)
        _WATCHDOG_STOP.wait(PULSE_INTERVAL_SEC)


def _watchdog_loop() -> None:
    global _WATCHDOG_STATE
    last_seq = -1
    miss_cycles = 0
    _WATCHDOG_STATE["running"] = True

    while not _WATCHDOG_STOP.is_set():
        try:
            from intelligence.matrix_prebaker import alpha_matrix_mapped, force_unmap_alpha_matrix

            seq, pulse_mapped = read_compiler_pulse()
            matrix_mapped = alpha_matrix_mapped()
            _WATCHDOG_STATE["shm_state"] = (
                "mapped" if matrix_mapped and pulse_mapped else "unmapped"
            )
            _WATCHDOG_STATE["last_seq"] = seq

            if not pulse_mapped:
                miss_cycles += 1
            elif seq == last_seq:
                miss_cycles += 1
            else:
                miss_cycles = 0
                last_seq = seq

            _WATCHDOG_STATE["miss_cycles"] = miss_cycles

            if miss_cycles >= PULSE_MISS_CYCLES and matrix_mapped:
                force_unmap_alpha_matrix()
                _WATCHDOG_STATE["unmap_count"] = int(_WATCHDOG_STATE.get("unmap_count") or 0) + 1
                _WATCHDOG_STATE["last_action"] = "force_unmap_alpha_matrix"
                _WATCHDOG_STATE["shm_state"] = "unmapped"
                log_engine(
                    "ShmWatchdog: compiler pulse missed — "
                    f"unlinked /ig_agent_v30_alpha_matrix (miss_cycles={miss_cycles})"
                )
                miss_cycles = 0
        except Exception as exc:
            log_guarded_exception("shm_watchdog", exc)
        _WATCHDOG_STOP.wait(PULSE_CHECK_SEC)

    _WATCHDOG_STATE["running"] = False


def start_shm_watchdog_async() -> None:
    """Start pulse emitter (shadow) + watchdog monitor (all tracks)."""
    global _WATCHDOG_THREAD
    if _WATCHDOG_THREAD is not None and _WATCHDOG_THREAD.is_alive():
        return

    import os

    track = os.environ.get("IG_PARALLEL_TRACK", "").strip().lower()
    if track == "shadow":
        pulse_thread = threading.Thread(
            target=_compiler_pulse_loop,
            name="alpha-matrix-pulse",
            daemon=True,
        )
        pulse_thread.start()
        log_engine("ShmWatchdog: compiler pulse thread started (100ms)")

    _WATCHDOG_STOP.clear()
    _WATCHDOG_THREAD = threading.Thread(
        target=_watchdog_loop,
        name="alpha-matrix-shm-watchdog",
        daemon=True,
    )
    _WATCHDOG_THREAD.start()
    log_engine("ShmWatchdog: memory watchdog daemon started")


def stop_shm_watchdog() -> None:
    _WATCHDOG_STOP.set()


def watchdog_telemetry() -> dict[str, Any]:
    return dict(_WATCHDOG_STATE)


def reset_shm_watchdog_for_tests() -> None:
    stop_shm_watchdog()
    global _WATCHDOG_THREAD
    _WATCHDOG_THREAD = None
    _WATCHDOG_STATE.update(
        {
            "running": False,
            "last_seq": 0,
            "miss_cycles": 0,
            "shm_state": "unknown",
            "last_action": "",
            "unmap_count": 0,
        }
    )
    try:
        shm = _pulse_shm(create=False)
        shm.close()
        shm.unlink()
    except FileNotFoundError:
        pass
