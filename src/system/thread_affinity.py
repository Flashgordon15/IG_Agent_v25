"""
M-series hardware thread affinity — P-core prioritization for execution + streaming.

Linux: ``os.sched_setaffinity`` when available.
macOS: pthread QoS ``USER_INTERACTIVE`` for near-zero scheduler latency on P-cores.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable
from typing import Any

from system.engine_log import log_engine

_PINNED_ROLES: set[str] = set()
_PIN_LOCK = threading.Lock()

QOS_CLASS_USER_INTERACTIVE = 0x21


def performance_cpu_set() -> set[int] | None:
    """Heuristic P-core CPU mask — first half of logical CPUs on Apple Silicon."""
    count = os.cpu_count() or 0
    if count < 4:
        return None
    p_cores = max(2, count // 2)
    return set(range(p_cores))


def _pin_darwin_qos() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import ctypes

        lib = ctypes.CDLL("/usr/lib/libpthread.dylib")
        fn = getattr(lib, "pthread_set_qos_class_self_np", None)
        if fn is None:
            return False
        fn.argtypes = [ctypes.c_uint, ctypes.c_int]
        fn.restype = ctypes.c_int
        rc = fn(QOS_CLASS_USER_INTERACTIVE, 0)
        return rc == 0
    except Exception:
        return False


def pin_current_thread(*, role: str = "execution") -> bool:
    """Pin the calling thread to performance cores / QoS class (idempotent per role)."""
    with _PIN_LOCK:
        if role in _PINNED_ROLES:
            return True

    pinned = False
    cpus = performance_cpu_set()
    if cpus and hasattr(os, "sched_setaffinity"):
        try:
            os.sched_setaffinity(0, cpus)
            pinned = True
            log_engine(
                f"thread_affinity: sched_setaffinity role={role} cpus={sorted(cpus)}"
            )
        except (AttributeError, OSError, NotImplementedError):
            pinned = False

    if not pinned and _pin_darwin_qos():
        pinned = True
        log_engine(f"thread_affinity: darwin QoS USER_INTERACTIVE role={role}")

    if pinned:
        with _PIN_LOCK:
            _PINNED_ROLES.add(role)
    return pinned


def spawn_priority_thread(
    target: Callable[[], Any],
    *,
    name: str,
    role: str = "worker",
    daemon: bool = True,
    args: tuple[Any, ...] = (),
    kwargs: dict[str, Any] | None = None,
) -> threading.Thread:
    """Spawn a daemon thread with performance-core affinity applied at entry."""

    def _wrapped() -> None:
        pin_current_thread(role=role)
        target(*args, **(kwargs or {}))

    thread = threading.Thread(target=_wrapped, name=name, daemon=daemon)
    thread.start()
    return thread


def apply_process_affinity_bootstrap() -> None:
    """One-shot bootstrap — pin main process before trading loops spin up."""
    pin_current_thread(role="main_bootstrap")
