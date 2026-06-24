"""
Telemetry circuit breaker — Offline Replicator Mode for external data feeds.

On HTTP 429 or sustained fetch failure, freeze the alpha matrix and replay the
last known valid price vector until the feed recovers.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception
from system.paths import data_dir, project_root

AUDIT_LOG = project_root() / "src" / "data" / "logs" / "self_healing_audit.log"
AUDIT_LOG_PRODUCTION = data_dir() / "logs" / "self_healing_audit.log"

_lock = threading.Lock()
_offline = False
_offline_since: float | None = None
_consecutive_failures = 0
_FAILURE_THRESHOLD = 2


@dataclass(frozen=True)
class FrozenPriceVector:
    epic: str
    bid: float
    offer: float
    mid: float
    spread: float
    frozen_at: float


_last_vectors: dict[str, FrozenPriceVector] = {}
_frozen_matrix_snapshot: Any | None = None
_audit_lock = threading.Lock()


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append_audit(record: dict[str, Any]) -> None:
    line = json.dumps(record, separators=(",", ":"), default=str)
    for path in (AUDIT_LOG, AUDIT_LOG_PRODUCTION):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with _audit_lock:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception as exc:
            log_guarded_exception("telemetry_circuit_breaker_audit", exc)


def is_offline_replicator_mode() -> bool:
    with _lock:
        return bool(_offline)


def record_successful_tick(
    *,
    epic: str,
    bid: float,
    offer: float,
    mid: float,
    spread: float,
) -> None:
    global _offline, _offline_since, _consecutive_failures, _frozen_matrix_snapshot
    now = time.time()
    vec = FrozenPriceVector(
        epic=str(epic),
        bid=float(bid),
        offer=float(offer),
        mid=float(mid),
        spread=float(spread),
        frozen_at=now,
    )
    with _lock:
        was_offline = _offline
        _last_vectors[str(epic)] = vec
        _consecutive_failures = 0
        if was_offline:
            _offline = False
            _offline_since = None
            _frozen_matrix_snapshot = None
    if was_offline:
        msg = (
            f"Telemetry circuit breaker RECOVERED — feed online; "
            f"resuming live matrix writes epic={epic}"
        )
        log_engine(f"[TelemetryGasket] {msg}")
        _append_audit(
            {
                "ts": _utc_iso(),
                "component": "telemetry_circuit_breaker",
                "event": "recovery",
                "epic": epic,
                "message": msg,
            }
        )


def record_feed_failure(
    *,
    epic: str,
    reason: str,
    http_status: int | None = None,
) -> None:
    global _offline, _offline_since, _consecutive_failures, _frozen_matrix_snapshot
    enter_offline = False
    with _lock:
        _consecutive_failures += 1
        if http_status == 429 or _consecutive_failures >= _FAILURE_THRESHOLD:
            if not _offline:
                enter_offline = True
                _offline = True
                _offline_since = time.time()
    if enter_offline:
        try:
            from system.ipc.ring_buffer import get_alpha_ring_buffer

            ring = get_alpha_ring_buffer()
            _frozen_matrix_snapshot = ring.matrix_view().copy()
        except Exception as exc:
            log_guarded_exception("telemetry_cb_matrix_freeze", exc, epic=epic)
        msg = (
            f"Offline Replicator Mode ENGAGED — reason={reason} "
            f"http={http_status} epic={epic}; serving last valid vectors"
        )
        log_engine(f"[TelemetryGasket] {msg}")
        _append_audit(
            {
                "ts": _utc_iso(),
                "event": "offline_replicator_engaged",
                "component": "telemetry_circuit_breaker",
                "epic": epic,
                "reason": reason,
                "http_status": http_status,
                "message": msg,
            }
        )


def last_vector_for_epic(epic: str) -> FrozenPriceVector | None:
    with _lock:
        return _last_vectors.get(str(epic or "").strip())


def matrix_writes_frozen() -> bool:
    return is_offline_replicator_mode()


def frozen_matrix_view():
    with _lock:
        return _frozen_matrix_snapshot


def reset_telemetry_circuit_breaker_for_tests() -> None:
    global _offline, _offline_since, _consecutive_failures, _frozen_matrix_snapshot
    with _lock:
        _offline = False
        _offline_since = None
        _consecutive_failures = 0
        _last_vectors.clear()
        _frozen_matrix_snapshot = None
