"""
Cross-process dashboard snapshot — atomic JSON file + in-process subscribers.

Trading loop (separate process) calls publish_tick(); FastAPI reads the file
and broadcasts to WebSocket clients. Trading continues if the API process fails.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from api.snapshot import build_default_tick, normalize_tick
from system.paths import data_dir
from system.state_manager import atomic_write_json, read_json_file

_SNAPSHOT_FILENAME = "dashboard_snapshot.json"
_lock = threading.RLock()
_cached: dict[str, Any] = build_default_tick()
_cached_mtime: float = 0.0
_path_override: Path | None = None
_subscribers: list[Callable[[dict[str, Any]], None]] = []


def snapshot_path() -> Path:
    if _path_override is not None:
        return _path_override
    state_dir = data_dir() / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / _SNAPSHOT_FILENAME


def set_snapshot_path_for_tests(path: Path | str | None) -> None:
    global _path_override, _cached, _cached_mtime
    with _lock:
        _path_override = Path(path) if path else None
        _cached = build_default_tick()
        _cached_mtime = 0.0


def reset_snapshot_store_for_tests() -> None:
    global _cached, _cached_mtime, _subscribers
    with _lock:
        _path_override = None
        _cached = build_default_tick()
        _cached_mtime = 0.0
        _subscribers.clear()


def subscribe(callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
    """Register a tick listener; returns unsubscribe function."""
    with _lock:
        _subscribers.append(callback)

    def _unsub() -> None:
        with _lock:
            if callback in _subscribers:
                _subscribers.remove(callback)

    return _unsub


def _notify_locked(tick: dict[str, Any]) -> None:
    for cb in list(_subscribers):
        try:
            cb(tick)
        except Exception:
            pass


def write_tick_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Persist tick snapshot to disk (trading loop — separate process).

    Does not notify WebSocket subscribers; the API process picks up changes
    via watch_snapshot_file().
    """
    global _cached, _cached_mtime
    tick = normalize_tick(payload)
    path = snapshot_path()
    atomic_write_json(path, tick)
    with _lock:
        _cached = tick
        try:
            _cached_mtime = path.stat().st_mtime
        except OSError:
            _cached_mtime = time.time()
    return tick


def publish_tick(payload: dict[str, Any], *, notify: bool = True) -> dict[str, Any]:
    """Write snapshot and optionally notify in-process subscribers (tests)."""
    tick = write_tick_snapshot(payload)
    if notify:
        with _lock:
            _notify_locked(tick)
    return tick


def get_tick() -> dict[str, Any]:
    """Return latest snapshot (memory cache, refreshed from disk if newer)."""
    global _cached, _cached_mtime
    path = snapshot_path()
    with _lock:
        if not path.exists():
            return dict(_cached)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return dict(_cached)
        if mtime <= _cached_mtime:
            return dict(_cached)
    data = read_json_file(path)
    if not isinstance(data, dict):
        return dict(_cached)
    tick = normalize_tick(data)
    with _lock:
        _cached = tick
        _cached_mtime = mtime
    return dict(tick)


def snapshot_age_s() -> float | None:
    """Seconds since last published tick, or None if never written."""
    tick = get_tick()
    ts = tick.get("ts")
    if not ts:
        return None
    try:
        from datetime import datetime

        if str(ts).endswith("Z"):
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(str(ts))
        return max(0.0, time.time() - dt.timestamp())
    except Exception:
        return None


async def watch_snapshot_file(poll_interval: float = 0.25) -> None:
    """Poll snapshot file for cross-process updates (API-only process)."""
    global _cached, _cached_mtime
    path = snapshot_path()
    while True:
        await asyncio.sleep(poll_interval)
        if not path.exists():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        with _lock:
            if mtime <= _cached_mtime:
                continue
        data = read_json_file(path)
        if not isinstance(data, dict):
            continue
        tick = normalize_tick(data)
        with _lock:
            _cached = tick
            _cached_mtime = mtime
            _notify_locked(tick)
