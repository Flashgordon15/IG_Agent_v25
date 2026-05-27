"""Per-epic in-flight exit tracking — idempotent close protection."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from system.engine_log import log_engine


def _request_save() -> None:
    try:
        from system.runtime_state_persist import request_save

        request_save()
    except Exception:
        pass

DEFAULT_EXIT_TIMEOUT_SEC = 30.0
_DUPLICATE_LOG_INTERVAL_SEC = 60.0

_lock = threading.RLock()
_exits: dict[str, "InFlightExit"] = {}
_last_duplicate_log_ts: dict[str, float] = {}


@dataclass(frozen=True)
class InFlightExit:
    epic: str
    local_created_at: float
    broker_deal_reference: str = ""


def _epic_key(epic: str) -> str:
    return str(epic or "").strip()


def _is_expired(exit_rec: InFlightExit, now: float, timeout_sec: float) -> bool:
    return (now - exit_rec.local_created_at) > max(1.0, float(timeout_sec))


def purge_expired_exits(*, timeout_sec: float = DEFAULT_EXIT_TIMEOUT_SEC) -> int:
    now = time.time()
    removed = 0
    with _lock:
        expired = [k for k, e in _exits.items() if _is_expired(e, now, timeout_sec)]
        for key in expired:
            _exits.pop(key, None)
            removed += 1
    return removed


def recover_startup_exit_inflight_state(
    *, timeout_sec: float = DEFAULT_EXIT_TIMEOUT_SEC
) -> int:
    """Drop stale in-flight exits on bot startup."""
    purge_expired_exits(timeout_sec=timeout_sec)
    with _lock:
        cleared = len(_exits)
        _exits.clear()
        _last_duplicate_log_ts.clear()
    return cleared


def has_exit_in_flight(
    epic: str,
    *,
    timeout_sec: float = DEFAULT_EXIT_TIMEOUT_SEC,
) -> bool:
    key = _epic_key(epic)
    if not key:
        return False
    now = time.time()
    with _lock:
        rec = _exits.get(key)
        if rec is None:
            return False
        if _is_expired(rec, now, timeout_sec):
            _exits.pop(key, None)
            return False
        return True


def get_exit_in_flight(epic: str) -> InFlightExit | None:
    key = _epic_key(epic)
    if not key:
        return None
    with _lock:
        return _exits.get(key)


def _log_duplicate(epic: str) -> None:
    now = time.time()
    last = _last_duplicate_log_ts.get(epic, 0.0)
    if now - last < _DUPLICATE_LOG_INTERVAL_SEC:
        return
    _last_duplicate_log_ts[epic] = now
    log_engine(f"Exit already in flight for {epic} — skipped duplicate")


def try_begin_exit(
    epic: str,
    *,
    timeout_sec: float = DEFAULT_EXIT_TIMEOUT_SEC,
) -> bool:
    key = _epic_key(epic)
    if not key:
        return True
    now = time.time()
    with _lock:
        existing = _exits.get(key)
        if existing is not None:
            if _is_expired(existing, now, timeout_sec):
                _exits.pop(key, None)
            else:
                _log_duplicate(key)
                return False
        _exits[key] = InFlightExit(epic=key, local_created_at=now)
    _request_save()
    return True


def set_exit_deal_reference(epic: str, deal_reference: str) -> None:
    key = _epic_key(epic)
    if not key:
        return
    with _lock:
        rec = _exits.get(key)
        if rec is None:
            return
        _exits[key] = InFlightExit(
            epic=rec.epic,
            local_created_at=rec.local_created_at,
            broker_deal_reference=str(deal_reference or "").strip(),
        )
    _request_save()


def clear_exit(epic: str) -> None:
    key = _epic_key(epic)
    if not key:
        return
    with _lock:
        removed = _exits.pop(key, None) is not None
    if removed:
        _request_save()


def clear_exit_on_reconciled_close(epic: str) -> None:
    """Clear in-flight exit when broker reconciliation shows the position is gone."""
    clear_exit(epic)


def reset_exit_inflight_state_for_tests() -> None:
    with _lock:
        _exits.clear()
        _last_duplicate_log_ts.clear()


def dump_exit_state() -> dict[str, Any]:
    with _lock:
        return {
            "exits": [
                {
                    "epic": e.epic,
                    "local_created_at": e.local_created_at,
                    "broker_deal_reference": e.broker_deal_reference,
                }
                for e in _exits.values()
            ]
        }


def load_exit_state(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        return
    items = data.get("exits") or []
    if not isinstance(items, list):
        return
    now = time.time()
    with _lock:
        _exits.clear()
        for item in items:
            if not isinstance(item, dict):
                continue
            epic = _epic_key(str(item.get("epic") or ""))
            if not epic:
                continue
            try:
                ts = float(item.get("local_created_at") or 0.0)
            except (TypeError, ValueError):
                continue
            if ts <= 0 or (now - ts) > DEFAULT_EXIT_TIMEOUT_SEC:
                continue
            _exits[epic] = InFlightExit(
                epic=epic,
                local_created_at=ts,
                broker_deal_reference=str(item.get("broker_deal_reference") or ""),
            )
