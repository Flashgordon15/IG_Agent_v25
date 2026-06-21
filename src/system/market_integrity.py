"""
Authoritative market-open / quote-freshness gate for ingestion, hub cache, and UI.

Prevents synthetic or stale quotes from presenting as live when exchanges are
closed or packet age exceeds the execution hot-path budget.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from system.engine_log import log_engine_intermittent

# Hot-path budget — strategy pauses when hub quote age exceeds this (seconds).
LIVE_QUOTE_MAX_AGE_SEC = 0.5
LIVE_QUOTE_MAX_AGE_MS = LIVE_QUOTE_MAX_AGE_SEC * 1000.0

# Dashboard display — mark stream STALE after this when market is open.
UI_STALE_AFTER_SEC = 2.0

_lock = threading.RLock()
_last_open: dict[str, bool] = {}
_flat_interp_reset_hooks: list[Any] = []


@dataclass(frozen=True)
class QuoteIntegrityVerdict:
    allowed: bool
    reason: str = ""
    market_state: str = "OPEN"
    stream_status: str = "LIVE"
    quote_age_ms: float = 0.0


def register_flat_interpolator_reset(hook: Any) -> None:
    """MultiApiBroker registers reset callback for flat-tick synthesizer."""
    if callable(hook) and hook not in _flat_interp_reset_hooks:
        _flat_interp_reset_hooks.append(hook)


def _reset_flat_interpolators(epic: str) -> None:
    for hook in _flat_interp_reset_hooks:
        try:
            hook(epic)
        except Exception:
            pass


def epic_market_open(epic: str, *, at: datetime | None = None) -> bool:
    """True when calendar or cached REST status says the epic is tradeable."""
    key = str(epic or "").strip()
    if not key:
        return False
    try:
        from simulation.replay_clock import is_replay_active, now_datetime

        if is_replay_active():
            at = at or now_datetime()
            try:
                from system.market_watch.calendar import get_market_status

                status = get_market_status(key, at=at)
                if status is not None:
                    return bool(status.open)
            except Exception:
                pass
            return True
    except Exception:
        pass
    try:
        from system.agent_execution_mode import demo_sandbox_unblock_active

        if demo_sandbox_unblock_active():
            return True
    except Exception:
        pass
    try:
        from feeder.mock_feed_engine import mock_feed_active

        if mock_feed_active():
            # Mock sandbox may run on weekends — still honour fund calendar.
            pass
    except Exception:
        pass
    try:
        from system.market_watch.market_status_updater import cached_market_open

        cached = cached_market_open(key)
        if cached is not None:
            return bool(cached)
    except Exception:
        pass
    try:
        from system.market_watch.calendar import get_market_status

        status = get_market_status(key, at=at)
        if status is not None:
            return bool(status.open)
    except Exception:
        pass
    return False


def quote_age_seconds(
    quote: Any | None,
    *,
    hub_epic: str | None = None,
) -> float:
    """Age of quote timestamp or hub snapshot — whichever is fresher evidence of staleness."""
    ages: list[float] = []
    now_dt = _integrity_now()
    if quote is not None:
        ts = getattr(quote, "time", None)
        if isinstance(ts, datetime):
            ref = ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)
            now_ref = now_dt if now_dt.tzinfo is not None else now_dt.replace(tzinfo=timezone.utc)
            ages.append(max(0.0, (now_ref - ref).total_seconds()))
    epic = str(hub_epic or getattr(quote, "epic", "") or "").strip()
    if epic:
        try:
            from system.market_data_hub import get_market_data_hub

            snap = get_market_data_hub().get_snapshot(epic)
            if snap is not None:
                ages.append(float(snap.age_seconds()))
        except Exception:
            pass
    return max(ages) if ages else float("inf")


def _integrity_now() -> datetime:
    try:
        from simulation.replay_clock import is_replay_active, now_datetime

        if is_replay_active():
            return now_datetime()
    except Exception:
        pass
    return datetime.now(timezone.utc)


def check_quote_integrity(
    epic: str,
    quote: Any | None,
    *,
    max_age_sec: float = LIVE_QUOTE_MAX_AGE_SEC,
) -> QuoteIntegrityVerdict:
    """Gate trading + ingestion — CLOSED markets and stale packets halt execution."""
    key = str(epic or "").strip()
    if not key:
        return QuoteIntegrityVerdict(
            allowed=False,
            reason="missing epic",
            market_state="OFFLINE",
            stream_status="DISCONNECTED",
        )
    if not epic_market_open(key):
        return QuoteIntegrityVerdict(
            allowed=False,
            reason="market closed",
            market_state="CLOSED",
            stream_status="CLOSED",
        )
    age_s = quote_age_seconds(quote, hub_epic=key)
    age_ms = age_s * 1000.0
    if age_s > max_age_sec:
        return QuoteIntegrityVerdict(
            allowed=False,
            reason="DATA STALE — quote age exceeds 500ms",
            market_state="OPEN",
            stream_status="STALE",
            quote_age_ms=age_ms,
        )
    bid = float(getattr(quote, "bid", 0) or 0) if quote is not None else 0.0
    offer = float(getattr(quote, "offer", 0) or 0) if quote is not None else 0.0
    if bid <= 0 or offer <= 0:
        return QuoteIntegrityVerdict(
            allowed=False,
            reason="no quote",
            market_state="OPEN",
            stream_status="DISCONNECTED",
        )
    stream = "LIVE" if age_s <= UI_STALE_AFTER_SEC else "STALE"
    return QuoteIntegrityVerdict(
        allowed=True,
        market_state="OPEN",
        stream_status=stream,
        quote_age_ms=age_ms,
    )


def closed_slice_fields(*, reason: str = "market closed") -> dict[str, Any]:
    """Dashboard / IPC fields when exchange is closed — zeroed prices, explicit state."""
    return {
        "market_state": "CLOSED",
        "stream_status": "CLOSED",
        "bid": 0.0,
        "offer": 0.0,
        "spread": 0.0,
        "tick_age_s": 0.0,
        "closure_reason": reason,
    }


def flush_epic_quote_cache(epic: str) -> None:
    """Drop hub + interpolator state for an epic."""
    key = str(epic or "").strip()
    if not key:
        return
    try:
        from system.market_data_hub import get_market_data_hub

        get_market_data_hub().invalidate(key)
    except Exception:
        pass
    _reset_flat_interpolators(key)


def publish_closed_state_to_dashboard(epic: str, *, reason: str = "market closed") -> None:
    """Push CLOSED slice into snapshot + subscribers (IPC / HTTP poll)."""
    key = str(epic or "").strip()
    if not key:
        return
    try:
        from api.snapshot_store import apply_closed_market_slice

        apply_closed_market_slice(key, reason=reason)
    except Exception as exc:
        log_engine_intermittent(
            f"closed_publish:{key}",
            f"market_integrity: closed publish skipped epic={key}: "
            f"{type(exc).__name__}: {exc}",
            interval_sec=120.0,
        )


def on_market_closed(epic: str, *, reason: str = "market closed") -> None:
    """Transition handler — flush caches and freeze UI stream for epic."""
    key = str(epic or "").strip()
    if not key:
        return
    flush_epic_quote_cache(key)
    publish_closed_state_to_dashboard(key, reason=reason)
    log_engine_intermittent(
        f"market_closed:{key}",
        f"market_integrity: exchange CLOSED epic={key} — buffers flushed, stream halted",
        interval_sec=300.0,
        force=True,
    )


def note_market_status(epic: str, open_now: bool) -> None:
    """Detect open→closed transitions (called from status updater)."""
    key = str(epic or "").strip()
    if not key:
        return
    with _lock:
        prev = _last_open.get(key)
        _last_open[key] = bool(open_now)
    if prev is True and not open_now:
        on_market_closed(key)


def should_publish_live_quote(epic: str, *, source: str = "") -> bool:
    """Ingestion gate — block synthetic/live republish when exchange is closed."""
    if str(source or "").lower() in ("replay", "harness"):
        return True
    if os.environ.get("IG_TEST_HARNESS", "").strip() == "1":
        return True
    if epic_market_open(epic):
        return True
    log_engine_intermittent(
        f"ingest_block:{epic}",
        f"market_integrity: blocked {source or 'quote'} epic={epic} (exchange CLOSED)",
        interval_sec=120.0,
    )
    return False


def stream_status_for_hub(epic: str, snap: Any | None) -> str:
    """Resolve stream_status for dashboard merge from hub snapshot."""
    if not epic_market_open(epic):
        return "CLOSED"
    if snap is None:
        return "DISCONNECTED"
    age = float(getattr(snap, "age_seconds", lambda: 999.0)())
    if age > LIVE_QUOTE_MAX_AGE_SEC:
        return "STALE"
    if age > UI_STALE_AFTER_SEC:
        return "STALE"
    return "LIVE"
