"""
Global IG REST call spacing — keeps total requests under IG per-minute allowance.

All authenticated REST traffic should pass through :meth:`RestApiBudget.acquire`
(via :meth:`ig_api.rest_client.IGRestClient.request`).
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from system.engine_log import log_engine

ESSENTIAL_REST_CATEGORIES = frozenset({"positions", "orders"})
PREEMPTIVE_CONSECUTIVE_READINGS = 3
PREEMPTIVE_PAUSE_SEC = 30.0
PREEMPTIVE_UTILIZATION_RATIO = 0.8
FRESH_STREAM_TICK_MAX_AGE_SEC = 5.0


class RestBudgetPausedError(RuntimeError):
    """Non-essential REST deferred — rate limit or proactive throttle."""


@dataclass(frozen=True)
class RestCallRecord:
    ts: float
    label: str
    category: str


def categorize_rest_label(label: str) -> str:
    """Bucket REST paths for budget reporting."""
    s = str(label or "").upper()
    if "SESSION" in s or "LOGIN" in s or "/AUTH" in s:
        return "auth"
    if "MARKET" in s or "PRICES" in s or "SNAPSHOT" in s:
        return "market"
    if "POSITION" in s or "WORKINGORDER" in s:
        return "positions"
    if "HISTORY" in s or "TRANSACTION" in s or "ACTIVITY" in s:
        return "history"
    if "ACCOUNT" in s or "BALANCE" in s:
        return "account"
    if "CONFIRM" in s or "DEAL" in s:
        return "orders"
    return "other"


_ORDER_IN_FLIGHT_COUNT = 0
_ORDER_IN_FLIGHT_LOCK = threading.RLock()
_ORDER_IN_FLIGHT_ALLOWED_CATEGORIES = frozenset({"positions", "orders"})
_ORDER_IN_FLIGHT_PAUSED_ACTIVITIES = frozenset(
    {
        "transaction_history",
        "transaction_sync",
        "account_summary",
        "account_refresh",
        "closed_trades_refresh",
        "preview_quote",
        "verify_reconcile",
        "keepalive_sync",
        "startup_pipeline",
    }
)


def begin_order_in_flight() -> None:
    """Reserve REST budget for POST /positions/otc + GET /confirms + position sync."""
    global _ORDER_IN_FLIGHT_COUNT
    with _ORDER_IN_FLIGHT_LOCK:
        _ORDER_IN_FLIGHT_COUNT += 1
        if _ORDER_IN_FLIGHT_COUNT == 1:
            log_engine(
                "Order in flight — REST budget reserved for confirm + position sync"
            )


def end_order_in_flight() -> None:
    """Release REST reservation after terminal confirm (ACCEPTED or FAILED)."""
    global _ORDER_IN_FLIGHT_COUNT
    with _ORDER_IN_FLIGHT_LOCK:
        if _ORDER_IN_FLIGHT_COUNT <= 0:
            _ORDER_IN_FLIGHT_COUNT = 0
            return
        _ORDER_IN_FLIGHT_COUNT -= 1
        if _ORDER_IN_FLIGHT_COUNT == 0:
            log_engine("Order in flight ended — REST budget released")


def is_order_in_flight() -> bool:
    with _ORDER_IN_FLIGHT_LOCK:
        return _ORDER_IN_FLIGHT_COUNT > 0


def _primary_market_epic() -> str:
    try:
        from system.config_loader import get_config
        from trading.instrument_registry import InstrumentRegistry

        cfg = get_config(reload=False)
        reg = InstrumentRegistry(cfg.as_dict())
        enabled = reg.get_enabled()
        if enabled:
            return str(enabled[0].get("epic") or cfg.epic)
        return str(cfg.epic)
    except Exception:
        return "IX.D.NIKKEI.IFM.IP"


def hub_quote_stream_fresh(*, max_age: float = FRESH_STREAM_TICK_MAX_AGE_SEC) -> bool:
    """
    True when the hub holds recent bid/offer (Lightstreamer or stream poll).

    Fresh ticks mean market data does not need REST polling — preemptive throttle
    must not block market/category REST in that window (v24 failure register #6).
    """
    try:
        from system.market_watch.japan225_session import is_quote_stream_fresh

        return is_quote_stream_fresh(_primary_market_epic(), max_age=max_age)
    except Exception:
        return False


def order_in_flight_paused(activity: str) -> bool:
    """True when non-essential REST should defer during async order confirm."""
    if not is_order_in_flight():
        return False
    act = str(activity or "").lower()
    if act in ("position_sync", "positions", "orders"):
        return False
    if act in _ORDER_IN_FLIGHT_PAUSED_ACTIVITIES:
        return True
    return bool(act) and act not in ("position_sync", "positions", "orders")


class RestApiBudget:
    """Minimum interval between REST calls (process-wide) + rolling rate metrics."""

    def __init__(self, *, min_interval_seconds: float = 10.0, warn_per_minute: int = 6) -> None:
        self._min_interval = max(0.5, float(min_interval_seconds))
        self._warn_per_minute = max(1, int(warn_per_minute))
        self._lock = threading.RLock()
        self._last_ts = 0.0
        self._total_waits = 0
        self._total_calls = 0
        self._recent: deque[RestCallRecord] = deque(maxlen=600)
        self._last_warn_ts = 0.0
        self._last_log_ts = 0.0
        self._preemptive_pause_until = 0.0
        self._consecutive_high_readings = 0
        self._rate_limit_skip_logged = False
        self._preemptive_skip_logged = False
        self._connecting_market_rescue_armed = False
        self._connecting_market_rescue_consumed = False

    def set_min_interval(self, seconds: float) -> None:
        with self._lock:
            self._min_interval = max(0.5, float(seconds))

    def set_warn_per_minute(self, value: int) -> None:
        with self._lock:
            self._warn_per_minute = max(1, int(value))

    def acquire(self, *, label: str = "") -> None:
        """Block until the next REST slot is available."""
        from system.rate_limit_manager import get_rate_limit_manager

        cat = categorize_rest_label(label)
        if cat not in ESSENTIAL_REST_CATEGORIES:
            self._raise_if_non_essential_paused(cat, label=label)

        get_rate_limit_manager().check_rest_allowed()

        while is_order_in_flight() and cat not in _ORDER_IN_FLIGHT_ALLOWED_CATEGORIES:
            time.sleep(0.05)

        with self._lock:
            now = time.time()
            elapsed = now - self._last_ts
            if elapsed < self._min_interval:
                wait = self._min_interval - elapsed
                self._total_waits += 1
                time.sleep(wait)
                now = time.time()
            self._last_ts = now
            self._total_calls += 1
            cat = categorize_rest_label(label)
            self._recent.append(RestCallRecord(now, label, cat))
            self._track_preemptive_locked(now)
            self._maybe_warn_locked(now)
            self._maybe_periodic_log_locked(now)

    def _rate_limit_rest_active(self) -> bool:
        from system.rate_limit_manager import get_rate_limit_manager

        return get_rate_limit_manager().is_rest_blocked()

    def _preemptive_pause_active(self) -> bool:
        return time.time() < self._preemptive_pause_until

    def _preemptive_throttle_blocks_rest(self) -> bool:
        """Preemptive pause applies only when the live stream is down/stale."""
        if hub_quote_stream_fresh():
            return False
        return self._preemptive_pause_active()

    def _preemptive_utilization_high_locked(self, now: float) -> bool:
        count = len(self._prune_locked(now))
        threshold = max(1, math.ceil(self._warn_per_minute * PREEMPTIVE_UTILIZATION_RATIO))
        return count >= threshold

    def _raise_if_non_essential_paused(self, category: str, *, label: str = "") -> None:
        if category in ESSENTIAL_REST_CATEGORIES:
            return
        if self._rate_limit_rest_active():
            if not self._rate_limit_skip_logged:
                self._rate_limit_skip_logged = True
                log_engine("Rate limit active — non-essential REST skipped")
            raise RestBudgetPausedError("rate_limit_active")
        history_reconcile = bool(label) and "history/transactions" in label.lower()
        connecting_rescue = self._consume_connecting_market_rescue(label)
        if (
            self._preemptive_throttle_blocks_rest()
            and not history_reconcile
            and not connecting_rescue
        ):
            if not self._preemptive_skip_logged:
                self._preemptive_skip_logged = True
            raise RestBudgetPausedError("preemptive_throttle")

    def arm_connecting_market_rescue_once(self) -> bool:
        """Arm one preemptive-throttle bypass for the next market REST call."""
        with self._lock:
            if self._connecting_market_rescue_consumed:
                return False
            self._connecting_market_rescue_armed = True
            return True

    def _consume_connecting_market_rescue(self, label: str) -> bool:
        with self._lock:
            if not self._connecting_market_rescue_armed:
                return False
            if categorize_rest_label(label) != "market":
                return False
            self._connecting_market_rescue_armed = False
            self._connecting_market_rescue_consumed = True
            log_engine(
                "CONNECTING market rescue — preemptive throttle bypass (one-shot)"
            )
            return True

    def _track_preemptive_locked(self, now: float) -> None:
        if hub_quote_stream_fresh():
            self._consecutive_high_readings = 0
            if self._preemptive_pause_active():
                self._preemptive_pause_until = 0.0
                log_engine(
                    "REST preemptive throttle cleared — fresh Lightstreamer/stream ticks"
                )
            if not self._rate_limit_rest_active():
                self._rate_limit_skip_logged = False
                self._preemptive_skip_logged = False
            return

        if not self._preemptive_pause_active() and not self._rate_limit_rest_active():
            self._rate_limit_skip_logged = False
            self._preemptive_skip_logged = False

        if not self._preemptive_utilization_high_locked(now):
            self._consecutive_high_readings = 0
            return

        self._consecutive_high_readings += 1
        if self._consecutive_high_readings >= PREEMPTIVE_CONSECUTIVE_READINGS:
            self._preemptive_pause_until = now + PREEMPTIVE_PAUSE_SEC
            self._consecutive_high_readings = 0
            self._preemptive_skip_logged = False
            log_engine(
                "REST approaching limit — throttling 30s "
                f"(stream stale, >={int(PREEMPTIVE_UTILIZATION_RATIO * 100)}% budget)"
            )

    def _prune_locked(self, now: float) -> list[RestCallRecord]:
        cutoff = now - 60.0
        while self._recent and self._recent[0].ts < cutoff:
            self._recent.popleft()
        return list(self._recent)

    def calls_last_minute(self) -> int:
        with self._lock:
            return len(self._prune_locked(time.time()))

    def _maybe_warn_locked(self, now: float) -> None:
        count = len(self._prune_locked(now))
        if count < self._warn_per_minute:
            return
        if now - self._last_warn_ts < 30.0:
            return
        self._last_warn_ts = now
        by_cat = self._by_category_locked()
        detail = ", ".join(f"{k}={v}" for k, v in sorted(by_cat.items()))
        log_engine(
            f"REST budget WARN: {count} calls/min (limit advisory {self._warn_per_minute}) — {detail}"
        )

    def _maybe_periodic_log_locked(self, now: float) -> None:
        if now - self._last_log_ts < 300.0:
            return
        if self._total_calls <= 0:
            return
        self._last_log_ts = now
        count = len(self._prune_locked(now))
        by_cat = self._by_category_locked()
        detail = ", ".join(f"{k}={v}" for k, v in sorted(by_cat.items())) or "none"
        log_engine(
            f"REST budget: {count}/min ({detail}) | total={self._total_calls} throttled={self._total_waits}"
        )

    def _by_category_locked(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for rec in self._recent:
            out[rec.category] = out.get(rec.category, 0) + 1
        return out

    def status_label(self) -> str:
        count = self.calls_last_minute()
        if count >= self._warn_per_minute:
            return f"HIGH ({count}/min)"
        if count >= max(1, self._warn_per_minute - 2):
            return f"OK ({count}/min)"
        return f"OK ({count}/min)" if count else "OK (idle)"

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            recent = self._prune_locked(now)
            by_cat = self._by_category_locked()
            return {
                "min_interval_sec": self._min_interval,
                "warn_per_minute": self._warn_per_minute,
                "total_calls": self._total_calls,
                "throttled_waits": self._total_waits,
                "calls_last_minute": len(recent),
                "by_category_last_minute": by_cat,
                "status_label": self.status_label(),
                "last_labels": [r.label for r in recent[-5:]],
            }

    def snapshot(self) -> dict[str, Any]:
        return self.metrics()


_budget: RestApiBudget | None = None
_budget_lock = threading.Lock()


def get_rest_api_budget() -> RestApiBudget:
    global _budget
    with _budget_lock:
        if _budget is None:
            try:
                from system.config_loader import get_config

                cfg = get_config(reload=False)
                sec = float(cfg.rest_min_interval_seconds)
                warn = int(getattr(cfg, "rest_budget_warn_per_minute", 6))
            except Exception:
                sec = 10.0
                warn = 6
            _budget = RestApiBudget(min_interval_seconds=sec, warn_per_minute=warn)
        return _budget


def configure_rest_api_budget(*, min_interval_seconds: float | None = None) -> RestApiBudget:
    budget = get_rest_api_budget()
    if min_interval_seconds is not None:
        budget.set_min_interval(min_interval_seconds)
        log_engine(f"REST API budget: min interval {min_interval_seconds:.1f}s")
    try:
        from system.config_loader import get_config

        warn = int(getattr(get_config(reload=False), "rest_budget_warn_per_minute", 6))
        budget.set_warn_per_minute(warn)
    except Exception:
        pass
    return budget


def reset_connecting_market_rescue() -> None:
    """Reset one-shot CONNECTING market rescue (new Lightstreamer connect)."""
    budget = get_rest_api_budget()
    with budget._lock:
        budget._connecting_market_rescue_armed = False
        budget._connecting_market_rescue_consumed = False


def non_essential_rest_paused() -> bool:
    """True when non-essential REST should defer (403 pause or proactive throttle)."""
    budget = get_rest_api_budget()
    return budget._rate_limit_rest_active() or budget._preemptive_throttle_blocks_rest()
