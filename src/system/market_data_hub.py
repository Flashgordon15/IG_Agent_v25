"""
Shared live market quotes — one coordinated fetch path for stream, trading loop, and UI.

Reduces duplicate GET /markets calls and keeps bid/offer timestamps aligned.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from collections.abc import Callable
from typing import Any

from data.models import Quote
from system.engine_log import log_engine


@dataclass
class QuoteSnapshot:
    epic: str
    bid: float
    offer: float
    updated_at: float
    source: str = "ig"

    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.updated_at)

    def to_quote(self) -> Quote:
        return Quote(time=datetime.fromtimestamp(self.updated_at), bid=self.bid, offer=self.offer)


class MarketDataHub:
    """Thread-safe cache of latest IG prices per epic."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._quotes: dict[str, QuoteSnapshot] = {}
        self._rest: Any | None = None
        self._fetch_interval_sec = 5.0
        self._last_fetch_ts: dict[str, float] = {}
        self._total_fetches = 0
        self._listeners: list[Callable[[QuoteSnapshot], None]] = []
        self._maintenance_epics: set[str] = set()
        self._maintenance_logged: set[str] = set()

    def attach_rest(self, rest_client: Any) -> None:
        with self._lock:
            self._rest = rest_client

    def set_min_fetch_interval(self, seconds: float) -> None:
        self._fetch_interval_sec = max(0.5, float(seconds))

    def on_quote(self, callback: Callable[[QuoteSnapshot], None]) -> Callable[[], None]:
        """Register for hub price updates (Lightstreamer / REST poll)."""

        with self._lock:
            self._listeners.append(callback)

        def _unsub() -> None:
            with self._lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)

        return _unsub

    def _emit_quote(self, snap: QuoteSnapshot) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(snap)
            except Exception:
                pass

    def enter_maintenance(self, epic: str) -> None:
        """IG sends blank BID/OFFER during Japan 225 daily maintenance — pause REST/stale paths."""
        epic_key = str(epic)
        with self._lock:
            self._maintenance_epics.add(epic_key)
            first = epic_key not in self._maintenance_logged
            if first:
                self._maintenance_logged.add(epic_key)
        if first:
            log_engine(
                "Japan 225 maintenance window — pausing until prices resume"
            )

    def exit_maintenance(self, epic: str) -> None:
        epic_key = str(epic)
        with self._lock:
            self._maintenance_epics.discard(epic_key)
            self._maintenance_logged.discard(epic_key)

    def is_in_maintenance(self, epic: str) -> bool:
        with self._lock:
            return str(epic) in self._maintenance_epics

    def publish(self, epic: str, bid: float, offer: float, *, source: str = "stream") -> QuoteSnapshot:
        if bid > 0 and offer > 0:
            self.exit_maintenance(epic)
        snap = QuoteSnapshot(epic=epic, bid=bid, offer=offer, updated_at=time.time(), source=source)
        with self._lock:
            self._quotes[epic] = snap
            rest = self._rest
        if rest is not None and hasattr(rest, "touch_stream_activity"):
            rest.touch_stream_activity()
        self._emit_quote(snap)
        return snap

    def get_snapshot(self, epic: str) -> QuoteSnapshot | None:
        with self._lock:
            return self._quotes.get(epic)

    def invalidate(self, epic: str) -> None:
        """Drop cached quote timestamps for an epic (session transition reset)."""
        with self._lock:
            self._quotes.pop(epic, None)
            self._last_fetch_ts.pop(epic, None)

    def is_fresh(self, epic: str, *, max_age: float = 10.0) -> bool:
        snap = self.get_snapshot(epic)
        if not snap or snap.bid <= 0 or snap.offer <= 0:
            return False
        return snap.age_seconds() <= max_age

    def fetch_if_stale(
        self,
        epic: str,
        *,
        min_interval: float | None = None,
        max_age: float | None = None,
        stream_connecting: bool = False,
        connecting_grace_seconds: float = 90.0,
    ) -> QuoteSnapshot | None:
        """
        Return cached quote if fresh enough; otherwise fetch from IG REST.
        min_interval: minimum seconds between API calls for this epic.
        max_age: if set, return cache without fetch when younger than this.
        """
        if self.is_in_maintenance(epic):
            return self.get_snapshot(epic)

        interval = self._fetch_interval_sec if min_interval is None else min_interval
        with self._lock:
            rest = self._rest
            cached = self._quotes.get(epic)
            last_fetch = self._last_fetch_ts.get(epic, 0.0)

        if cached and cached.bid > 0:
            age = cached.age_seconds()
            if max_age is not None and age <= max_age:
                return cached
            if time.time() - last_fetch < interval:
                return cached

        if rest is None:
            return cached

        if (
            stream_connecting
            and cached
            and cached.bid > 0
            and cached.age_seconds() > connecting_grace_seconds
        ):
            from system.rest_api_budget import get_rest_api_budget

            budget = get_rest_api_budget()
            if budget._preemptive_pause_active():
                budget.arm_connecting_market_rescue_once()

        try:
            from system.rate_limit_manager import get_rate_limit_manager

            get_rate_limit_manager().check_rest_allowed()
        except Exception:
            return cached

        try:
            if hasattr(rest, "fetch_live_prices"):
                result = rest.fetch_live_prices(epic)
                if not result or len(result) < 2:
                    return cached
                bid, offer = float(result[0]), float(result[1])
            else:
                snap = rest.fetch_market_snapshot(epic, live=True)
                bid, offer = float(snap["bid"]), float(snap["offer"])
            with self._lock:
                self._last_fetch_ts[epic] = time.time()
                self._total_fetches += 1
            if hasattr(rest, "record_rest_success"):
                rest.record_rest_success(f"/markets/{epic[:32]}")
            return self.publish(epic, bid, offer, source="rest")
        except Exception as e:
            log_engine(f"MarketDataHub fetch failed: {type(e).__name__}: {e}")
            return cached

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            epic_snaps = {
                e: {"bid": s.bid, "offer": s.offer, "age_s": round(s.age_seconds(), 1)}
                for e, s in self._quotes.items()
            }
            return {
                "total_fetches": self._total_fetches,
                "min_interval_sec": self._fetch_interval_sec,
                "epics": epic_snaps,
            }


_hub: MarketDataHub | None = None
_hub_lock = threading.Lock()


def get_market_data_hub() -> MarketDataHub:
    global _hub
    with _hub_lock:
        if _hub is None:
            _hub = MarketDataHub()
        return _hub


def on_hub_quote(callback: Callable[[QuoteSnapshot], None]) -> Callable[[], None]:
    """Subscribe to live hub publishes (dashboard bridge, diagnostics)."""
    return get_market_data_hub().on_quote(callback)
