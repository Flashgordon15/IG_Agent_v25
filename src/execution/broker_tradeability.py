"""Live IG broker tradeability — gate new deals on REST market_status + min size."""

from __future__ import annotations

import threading
import time
from typing import Any

from system.engine_log import log_engine

_TRADEABLE_STATUSES = frozenset({"TRADEABLE", "OPEN"})
_STATUS_CACHE_TTL_SEC = 45.0
# Failed probes (REST down / radio silence) are cached briefly so the 500ms
# strategy sweep doesn't re-issue a blocking /markets round trip per signal.
_STATUS_FAILURE_TTL_SEC = 10.0
_status_cache: dict[str, tuple[float, str]] = {}
_status_cache_lock = threading.Lock()


def _cached_market_status(key: str) -> str | None:
    now = time.time()
    with _status_cache_lock:
        row = _status_cache.get(key)
        if row is None:
            return None
        ts, status = row
        ttl = _STATUS_CACHE_TTL_SEC if status else _STATUS_FAILURE_TTL_SEC
        if now - ts > ttl:
            _status_cache.pop(key, None)
            return None
        return status


def _store_market_status(key: str, status: str) -> None:
    with _status_cache_lock:
        _status_cache[key] = (time.time(), str(status or "").upper())


def clear_broker_status_cache() -> None:
    """Shutdown / test hook — drop cached IG market_status probes."""
    with _status_cache_lock:
        _status_cache.clear()


def broker_market_status(rest_client: Any, epic: str, *, cfg: Any | None = None) -> str:
    """Return upper-case IG market_status for *epic* (empty on failure)."""
    if rest_client is None:
        return ""
    key = str(epic or "").strip()
    if not key:
        return ""
    cached = _cached_market_status(key)
    if cached is not None:
        return cached
    try:
        from execution.broker_epic_resolver import resolve_order_epic_safe

        broker_epic = resolve_order_epic_safe(
            rest_client,
            key,
            cfg=cfg,
        )
        data = rest_client.fetch_market_constraints(broker_epic, budget_priority=True)
        status = str((data or {}).get("market_status") or "").upper()
        _store_market_status(key, status)
        return status
    except Exception as exc:
        log_engine(
            f"BrokerTradeability: status fetch failed epic={key} "
            f"{type(exc).__name__}: {exc}"
        )
        _store_market_status(key, "")
        return ""


def broker_new_deal_allowed(
    rest_client: Any,
    epic: str,
    *,
    cfg: Any | None = None,
) -> tuple[bool, str]:
    """
    True only when IG reports TRADEABLE/OPEN — rejects EDITS_ONLY/CLOSED.
    """
    key = str(epic or "").strip()
    if not key:
        return False, "missing_epic"
    try:
        if cfg is not None and hasattr(cfg, "get"):
            dual = cfg.get("dual_core") or {}
            if isinstance(dual, dict):
                excluded = {str(e).strip() for e in (dual.get("exclude_from_hot_path") or [])}
                if key in excluded:
                    return False, f"epic_excluded:{key}"
    except Exception:
        pass
    try:
        from runtime.broker_reject_guard import epic_post_blocked

        if epic_post_blocked(key):
            return False, f"post_blocked:{key}"
    except Exception:
        pass
    status = broker_market_status(rest_client, epic, cfg=cfg)
    if not status:
        return False, "market_status_unavailable"
    if status not in _TRADEABLE_STATUSES:
        return False, f"market_not_tradeable:{status}"
    return True, ""


def broker_min_deal_size(rest_client: Any, epic: str, *, cfg: Any | None = None) -> float:
    """IG min deal size for epic (0.0 if unknown)."""
    if rest_client is None:
        return 0.0
    try:
        from execution.broker_epic_resolver import resolve_order_epic_safe

        broker_epic = resolve_order_epic_safe(
            rest_client,
            epic,
            cfg=cfg,
        )
        data = rest_client.fetch_market_constraints(broker_epic, budget_priority=True)
        return max(0.0, float((data or {}).get("min_deal_size") or 0.0))
    except Exception:
        return 0.0
