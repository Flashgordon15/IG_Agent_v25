"""
Broker reconciliation daemon — ~1s async position drift detection.

Detects mismatch between internal lifecycle state and broker positions;
trips strategy kill-switch on critical drift.
"""

from __future__ import annotations

import threading
import time
from typing import Any

_INTERVAL_SEC = 1.0
_lock = threading.RLock()
_snapshot: dict[str, Any] = {
    "ok": True,
    "healthy": True,
    "last_run_ts": 0.0,
    "broker_positions": 0,
    "internal_positions": 0,
    "drift_count": 0,
    "last_drift_reason": "",
}
_thread: threading.Thread | None = None
_stop = threading.Event()
_drift_streak: int = 0
_DRIFT_STREAK_TRIP = 3


def _count_internal_positions() -> int:
    """Single source of truth — active_lifecycle_trades only (no double-count)."""
    try:
        from runtime.active_lifecycle_trades import list_active_lifecycle_trades
        from system.config_loader import get_config
        from data.learning_store import LearningStore

        store = LearningStore(str(get_config().learning_db))
        return len(list_active_lifecycle_trades(store))
    except Exception:
        return 0


def _fetch_broker_positions(rest: Any | None) -> tuple[int, list[Any]]:
    if rest is None:
        return 0, []
    try:
        positions = rest.get_open_positions() if hasattr(rest, "get_open_positions") else []
        if isinstance(positions, dict):
            positions = positions.get("positions") or positions.get("data") or []
        rows = list(positions or [])
        return len(rows), rows
    except Exception:
        return -1, []


def _reconcile_lifecycle_registry(broker_positions: list[Any]) -> dict[str, int]:
    """Close orphan lifecycle rows before drift detection trips the kill-switch."""
    try:
        from runtime.active_lifecycle_trades import reconcile_active_lifecycle_trades
        from data.learning_store import LearningStore
        from system.config_loader import get_config

        store = LearningStore(str(get_config().learning_db))
        return reconcile_active_lifecycle_trades(
            store,
            broker_positions,
            source="broker_reconcile_daemon",
        )
    except Exception:
        return {"adopted": 0, "synced": 0, "closed_registry": 0}


def _count_broker_positions(rest: Any | None) -> int:
    n, _ = _fetch_broker_positions(rest)
    return n


def run_reconciliation_once(*, rest: Any | None = None) -> dict[str, Any]:
    global _drift_streak
    broker_n, broker_positions = _fetch_broker_positions(rest)
    reconcile_counts = (
        _reconcile_lifecycle_registry(broker_positions) if broker_n >= 0 else {}
    )
    internal_n = _count_internal_positions()
    drift = abs(broker_n - internal_n) if broker_n >= 0 else 0
    healthy = (broker_n < 0) or drift <= 1
    reason = ""
    if broker_n >= 0 and drift > 1:
        _drift_streak += 1
        reason = f"broker={broker_n}_internal={internal_n}"
        if _drift_streak >= _DRIFT_STREAK_TRIP:
            try:
                from runtime.strategy_kill_switch import trip_master_strategy_kill_switch

                trip_master_strategy_kill_switch(
                    deal_id="",
                    reason=f"reconcile_drift:{reason}",
                )
            except Exception:
                pass
    else:
        _drift_streak = 0

    snap = {
        "ok": healthy,
        "healthy": healthy,
        "last_run_ts": time.time(),
        "broker_positions": broker_n,
        "internal_positions": internal_n,
        "drift_count": drift,
        "last_drift_reason": reason,
        "lifecycle_reconcile": reconcile_counts,
    }
    with _lock:
        _snapshot.clear()
        _snapshot.update(snap)
    return snap


def get_reconciliation_snapshot() -> dict[str, Any]:
    with _lock:
        return dict(_snapshot)


def start_broker_reconciliation_daemon(*, rest: Any | None = None) -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()

    def _loop() -> None:
        while not _stop.wait(_INTERVAL_SEC):
            try:
                run_reconciliation_once(rest=rest)
            except Exception:
                pass

    _thread = threading.Thread(target=_loop, name="broker-reconcile", daemon=True)
    _thread.start()


def stop_broker_reconciliation_daemon() -> None:
    _stop.set()


def reset_reconciliation_for_tests() -> None:
    global _thread, _drift_streak
    _drift_streak = 0
    with _lock:
        _snapshot.clear()
        _snapshot.update({"ok": True, "healthy": True, "drift_count": 0})
    _thread = None
