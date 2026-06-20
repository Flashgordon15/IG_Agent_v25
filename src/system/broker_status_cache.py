"""
Weekend / market-closure REST delta cache — 300s TTL, zero outbound retries.

Intercept account sync and broker polling when CLOSED or sync exceptions occur.
"""

from __future__ import annotations

import threading
import time
from typing import Any

_CACHE_TTL_SEC = 300.0
_lock = threading.RLock()
_entries: dict[str, dict[str, Any]] = {}


def _is_market_closed_signal(exc: BaseException | None = None, *, status: str = "") -> bool:
    text = f"{status} {exc or ''}".upper()
    tokens = ("CLOSED", "MARKET CLOSED", "WEEKEND", "BLACKOUT", "NOT OPEN", "SESSION")
    return any(t in text for t in tokens)


def write_broker_status_cache(
    endpoint: str,
    status: str,
    *,
    detail: str = "",
    ttl_sec: float = _CACHE_TTL_SEC,
) -> None:
    key = str(endpoint or "default").strip().lower()
    with _lock:
        _entries[key] = {
            "status": str(status or "CLOSED").upper(),
            "detail": str(detail or ""),
            "cached_at": time.time(),
            "expires_at": time.time() + max(1.0, float(ttl_sec)),
        }


def should_serve_from_cache(endpoint: str) -> bool:
    key = str(endpoint or "default").strip().lower()
    with _lock:
        row = _entries.get(key)
        if not row:
            return False
        if time.time() >= float(row.get("expires_at") or 0):
            _entries.pop(key, None)
            return False
        return True


def get_cached_broker_status(endpoint: str) -> dict[str, Any] | None:
    key = str(endpoint or "default").strip().lower()
    with _lock:
        row = _entries.get(key)
        if not row:
            return None
        if time.time() >= float(row.get("expires_at") or 0):
            _entries.pop(key, None)
            return None
        return dict(row)


def poll_broker_account_state(
    client: Any,
    *,
    endpoint: str = "account_summary",
    min_interval: float = 60.0,
) -> dict[str, float | None]:
    """
    Account sync with closure cache — downstream checks hit memory for 300s on CLOSED.
    """
    if should_serve_from_cache(endpoint):
        cached = get_cached_broker_status(endpoint) or {}
        if hasattr(client, "get_cached_account_summary"):
            summary = client.get_cached_account_summary()
            if isinstance(summary, dict):
                summary = dict(summary)
                summary["_broker_cache"] = cached
                return summary
        return {"balance": None, "available": None, "profit_loss": None, "_broker_cache": cached}

    try:
        if hasattr(client, "maybe_refresh_account_summary"):
            return client.maybe_refresh_account_summary(min_interval=min_interval)
        if hasattr(client, "refresh_account_summary"):
            return client.refresh_account_summary()
    except Exception as exc:
        if _is_market_closed_signal(exc):
            write_broker_status_cache(endpoint, "CLOSED", detail=str(exc))
        raise

    if hasattr(client, "get_cached_account_summary"):
        return client.get_cached_account_summary()
    return {"balance": None, "available": None, "profit_loss": None}


def record_broker_sync_failure(
    endpoint: str,
    exc: BaseException | None = None,
    *,
    http_status: int | None = None,
) -> None:
    status = "CLOSED" if _is_market_closed_signal(exc) or http_status in (403, 503) else "SYNC_ERROR"
    write_broker_status_cache(endpoint, status, detail=str(exc or http_status or ""))


def reset_broker_status_cache_for_tests() -> None:
    with _lock:
        _entries.clear()
