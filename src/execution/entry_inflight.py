"""Per-epic in-flight entry tracking — idempotent market entry protection."""

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

DEFAULT_ENTRY_TIMEOUT_SEC = 30.0
_DUPLICATE_LOG_INTERVAL_SEC = 60.0

_lock = threading.RLock()
_entries: dict[str, InFlightEntry] = {}
_last_duplicate_log_ts: dict[str, float] = {}


@dataclass(frozen=True)
class InFlightEntry:
    epic: str
    direction: str
    size: float
    local_created_at: float
    broker_deal_reference: str = ""


def _epic_key(epic: str) -> str:
    return str(epic or "").strip()


def _is_expired(entry: InFlightEntry, now: float, timeout_sec: float) -> bool:
    return (now - entry.local_created_at) > max(1.0, float(timeout_sec))


def purge_expired_entries(*, timeout_sec: float = DEFAULT_ENTRY_TIMEOUT_SEC) -> int:
    now = time.time()
    removed = 0
    with _lock:
        expired = [k for k, e in _entries.items() if _is_expired(e, now, timeout_sec)]
        for key in expired:
            _entries.pop(key, None)
            removed += 1
    return removed


def recover_startup_inflight_state(*, timeout_sec: float = DEFAULT_ENTRY_TIMEOUT_SEC) -> int:
    """Drop stale in-flight entries on bot startup."""
    return purge_expired_entries(timeout_sec=timeout_sec)


def has_entry_in_flight(
    epic: str,
    *,
    timeout_sec: float = DEFAULT_ENTRY_TIMEOUT_SEC,
) -> bool:
    key = _epic_key(epic)
    if not key:
        return False
    now = time.time()
    with _lock:
        entry = _entries.get(key)
        if entry is None:
            return False
        if _is_expired(entry, now, timeout_sec):
            _entries.pop(key, None)
            return False
        return True


def get_entry_in_flight(epic: str) -> InFlightEntry | None:
    key = _epic_key(epic)
    if not key:
        return None
    with _lock:
        return _entries.get(key)


def _log_duplicate(epic: str) -> None:
    now = time.time()
    last = _last_duplicate_log_ts.get(epic, 0.0)
    if now - last < _DUPLICATE_LOG_INTERVAL_SEC:
        return
    _last_duplicate_log_ts[epic] = now
    log_engine(f"Entry already in flight for {epic} — skipped duplicate")


def try_begin_entry(
    epic: str,
    direction: str,
    size: float,
    *,
    timeout_sec: float = DEFAULT_ENTRY_TIMEOUT_SEC,
) -> bool:
    key = _epic_key(epic)
    if not key:
        return True
    now = time.time()
    with _lock:
        existing = _entries.get(key)
        if existing is not None:
            if _is_expired(existing, now, timeout_sec):
                _entries.pop(key, None)
            else:
                _log_duplicate(key)
                return False
        _entries[key] = InFlightEntry(
            epic=key,
            direction=str(direction or "").upper(),
            size=float(size or 0.0),
            local_created_at=now,
        )
    _request_save()
    return True


def set_entry_deal_reference(epic: str, deal_reference: str) -> None:
    key = _epic_key(epic)
    if not key:
        return
    with _lock:
        entry = _entries.get(key)
        if entry is None:
            return
        _entries[key] = InFlightEntry(
            epic=entry.epic,
            direction=entry.direction,
            size=entry.size,
            local_created_at=entry.local_created_at,
            broker_deal_reference=str(deal_reference or "").strip(),
        )
    _request_save()


def clear_entry(epic: str) -> None:
    key = _epic_key(epic)
    if not key:
        return
    with _lock:
        removed = _entries.pop(key, None) is not None
    if removed:
        _request_save()


def clear_entry_on_reconciled_position(epic: str) -> None:
    """Clear in-flight entry when broker reconciliation shows an open position."""
    clear_entry(epic)


def reset_entry_inflight_state_for_tests() -> None:
    with _lock:
        _entries.clear()
        _last_duplicate_log_ts.clear()


def dump_entry_state() -> dict[str, Any]:
    with _lock:
        return {
            "entries": [
                {
                    "epic": e.epic,
                    "direction": e.direction,
                    "size": e.size,
                    "local_created_at": e.local_created_at,
                    "broker_deal_reference": e.broker_deal_reference,
                }
                for e in _entries.values()
            ]
        }


def load_entry_state(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        return
    items = data.get("entries") or []
    if not isinstance(items, list):
        return
    now = time.time()
    with _lock:
        _entries.clear()
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
            if ts <= 0 or (now - ts) > DEFAULT_ENTRY_TIMEOUT_SEC:
                continue
            try:
                size = float(item.get("size") or 0.0)
            except (TypeError, ValueError):
                size = 0.0
            _entries[epic] = InFlightEntry(
                epic=epic,
                direction=str(item.get("direction") or "").upper(),
                size=size,
                local_created_at=ts,
                broker_deal_reference=str(item.get("broker_deal_reference") or ""),
            )
