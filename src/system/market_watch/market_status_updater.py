"""
Background IG market-status cache — never block the tick thread on REST/calendar scans.

``MarketStatusUpdater`` polls ``/markets/{epic}`` (when a REST client is available)
or the local fund calendar in a detached daemon thread every 60 seconds. Gate 1
reads ``_cached_market_statuses`` only.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any

from system.engine_log import log_engine
from system.market_watch.calendar import MarketStatus, get_market_status

_cached_market_statuses: dict[str, MarketStatus] = {}
_cache_lock = threading.RLock()
_updater_thread: threading.Thread | None = None
_updater_stop = threading.Event()
_registered_epics: set[str] = set()
_rest_client_ref: Any | None = None
_poll_interval_sec = 60.0
_started = False


def _tradeable_market_status(raw: str) -> bool:
    status = str(raw or "").upper()
    return status in ("TRADEABLE", "EDITS_ONLY", "OPEN")


def _status_from_rest(epic: str, client: Any) -> MarketStatus | None:
    if client is None or not hasattr(client, "fetch_market_constraints"):
        return None
    try:
        data = client.fetch_market_constraints(epic)
    except Exception as e:
        log_engine(
            f"MarketStatusUpdater REST skip epic={epic}: {type(e).__name__}: {e}"
        )
        return None
    if not isinstance(data, dict):
        return None
    open_ok = _tradeable_market_status(str(data.get("market_status") or ""))
    cal = get_market_status(epic)
    return MarketStatus(
        fund_id=str(cal.fund_id if cal else epic),
        display_name=str(cal.display_name if cal else epic),
        epic=epic,
        open=open_ok,
        reason=str(data.get("market_status") or "rest"),
        next_open_at=cal.next_open_at if cal else None,
        timezone=str(cal.timezone if cal else "UTC"),
    )


def _status_from_calendar(epic: str) -> MarketStatus | None:
    try:
        return get_market_status(epic)
    except Exception:
        return None


def _refresh_epic(epic: str) -> None:
    key = str(epic or "").strip()
    if not key:
        return
    status: MarketStatus | None = None
    client = _rest_client_ref
    if client is not None:
        status = _status_from_rest(key, client)
    if status is None:
        status = _status_from_calendar(key)
    if status is None:
        return
    with _cache_lock:
        _cached_market_statuses[key] = status


def _poll_loop() -> None:
    while not _updater_stop.is_set():
        try:
            with _cache_lock:
                epics = sorted(_registered_epics)
            for epic in epics:
                if _updater_stop.is_set():
                    break
                _refresh_epic(epic)
        except Exception as e:
            log_engine(f"MarketStatusUpdater poll failed: {type(e).__name__}: {e}")
        if _updater_stop.wait(_poll_interval_sec):
            break


def ensure_market_status_updater_started(
    *,
    epics: list[str] | None = None,
    rest_client: Any | None = None,
    poll_interval_sec: float = 60.0,
) -> None:
    """Idempotent — safe from orchestrator start and gate hot path."""
    global _updater_thread, _rest_client_ref, _poll_interval_sec, _started
    if rest_client is not None:
        _rest_client_ref = rest_client
    if epics:
        with _cache_lock:
            for epic in epics:
                key = str(epic or "").strip()
                if key:
                    _registered_epics.add(key)
    _poll_interval_sec = max(5.0, float(poll_interval_sec))
    with _cache_lock:
        if _started and _updater_thread is not None and _updater_thread.is_alive():
            return
        _updater_stop.clear()
        _updater_thread = threading.Thread(
            target=_poll_loop,
            name="ig-market-status-updater",
            daemon=True,
        )
        _updater_thread.start()
        _started = True
        epics_now = list(_registered_epics)
    for epic in epics_now:
        _refresh_epic(epic)


def stop_market_status_updater_for_tests() -> None:
    global _updater_thread, _started
    _updater_stop.set()
    thread = _updater_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)
    with _cache_lock:
        _cached_market_statuses.clear()
        _registered_epics.clear()
    _rest_client_ref = None
    _updater_thread = None
    _started = False
    _updater_stop.clear()


def get_cached_market_status(epic: str) -> MarketStatus | None:
    key = str(epic or "").strip()
    if not key:
        return None
    with _cache_lock:
        return _cached_market_statuses.get(key)


def cached_market_open(epic: str) -> bool | None:
    """``True``/``False`` when cached; ``None`` when cache is cold."""
    status = get_cached_market_status(epic)
    if status is None:
        return None
    return bool(status.open)


def register_epics(epics: list[str]) -> None:
    with _cache_lock:
        for epic in epics:
            key = str(epic or "").strip()
            if key:
                _registered_epics.add(key)
