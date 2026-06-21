"""
Shadow → Live weight transfer over native shared memory.

Shadow simulator publishes candidate model weights only when win-rate edge
beats the random-walk baseline by > 2.5%. Live vanguard reads proposals
without blocking the trading hot path.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception
from system.identity.shared_memory_bridge import SharedMemoryStateBridge

_SHM_NAME = "ig_agent_v30_weight_xfer"
_EDGE_THRESHOLD = 0.025


class WeightTransferBridge(SharedMemoryStateBridge):
    """Dedicated 64 KiB segment for approved ML weight handoff."""

    def __init__(self, *, create: bool = False) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._shm = None
        self._name = _SHM_NAME
        self._attach_named(_SHM_NAME, create=create)

    def _attach_named(self, name: str, *, create: bool) -> None:
        from multiprocessing import shared_memory

        if create:
            self._unlink_named(name)
            self._shm = shared_memory.SharedMemory(name=name, create=True, size=65536)
            self._write_header(0, 0)
            log_engine(f"WeightTransferBridge: created name={name}")
            return
        try:
            self._shm = shared_memory.SharedMemory(name=name, create=False)
        except FileNotFoundError:
            self._shm = shared_memory.SharedMemory(name=name, create=True, size=65536)
            self._write_header(0, 0)
            log_engine(f"WeightTransferBridge: created (lazy) name={name}")

    @staticmethod
    def _unlink_named(name: str) -> None:
        from multiprocessing import shared_memory

        try:
            existing = shared_memory.SharedMemory(name=name, create=False)
        except FileNotFoundError:
            return
        try:
            existing.close()
            existing.unlink()
        except Exception as exc:
            log_guarded_exception("weight_transfer_unlink", exc)

    def publish_candidate(
        self,
        *,
        weights: dict[str, Any],
        edge: float,
        telemetry: dict[str, Any] | None = None,
    ) -> bool:
        """
        Shadow-only publish — returns True when edge hurdle cleared and bytes written.
        """
        edge_f = float(edge)
        if edge_f <= _EDGE_THRESHOLD:
            log_engine(
                "WeightTransferBridge: REJECTED "
                f"edge={edge_f:.4f} below threshold {_EDGE_THRESHOLD:.4f}"
            )
            return False

        payload = {
            "approved": True,
            "edge": round(edge_f, 6),
            "threshold": _EDGE_THRESHOLD,
            "weights": dict(weights),
            "telemetry": dict(telemetry or {}),
            "published_at": time.time(),
            "track": "shadow",
        }
        written = self.write_json(payload)
        if written > 0:
            log_engine(
                f"WeightTransferBridge: APPROVED edge={edge_f:.4f} bytes={written}"
            )
            return True
        return False

    def read_candidate(self) -> dict[str, Any] | None:
        payload = self.read_json()
        if not payload or not payload.get("approved"):
            return None
        if float(payload.get("edge") or 0.0) <= _EDGE_THRESHOLD:
            return None
        return payload


_bridge_singleton: WeightTransferBridge | None = None
_bridge_lock = threading.Lock()


def get_weight_transfer_bridge(*, create: bool = False) -> WeightTransferBridge:
    global _bridge_singleton
    with _bridge_lock:
        if _bridge_singleton is None:
            _bridge_singleton = WeightTransferBridge(create=create)
        return _bridge_singleton


def reset_weight_transfer_bridge(*, unlink: bool = True) -> None:
    global _bridge_singleton
    with _bridge_lock:
        if _bridge_singleton is not None:
            _bridge_singleton.close(unlink=unlink)
        _bridge_singleton = None
    if unlink:
        WeightTransferBridge._unlink_named(_SHM_NAME)
