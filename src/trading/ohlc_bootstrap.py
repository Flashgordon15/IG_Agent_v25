"""Seed SignalEngine quote history from IG REST OHLC on session open."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from data.models import Quote
from signals.signal_engine import SignalEngine
from system.engine_log import log_engine


def _parse_bar_time(raw: str) -> datetime:
    s = str(raw or "").strip()
    if not s:
        return datetime.now()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return datetime.now()
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def bootstrap_ohlc_for_session(
    rest_client: Any,
    signal_engine: SignalEngine,
    epic: str,
    market: str,
    *,
    num_points: int = 100,
    resolution: str = "MINUTE_5",
) -> int:
    """Inject historical bars into SignalEngine; returns count injected (0 on failure)."""
    try:
        if rest_client is None or signal_engine is None:
            return 0
        fetch = getattr(rest_client, "fetch_price_history", None)
        if not callable(fetch):
            log_engine("OHLC bootstrap: fetch_price_history unavailable")
            return 0
        bars = fetch(epic, resolution=resolution, num_points=num_points)
        if not bars:
            log_engine(f"OHLC bootstrap: no bars returned for {epic}")
            return 0
        count = 0
        for bar in bars:
            high = float(bar.get("high") or 0)
            low = float(bar.get("low") or 0)
            if high <= 0 or low <= 0:
                continue
            mid = (high + low) / 2.0
            bid_close = float(bar.get("bid_close") or 0)
            offer_close = float(bar.get("offer_close") or 0)
            if bid_close > 0 and offer_close > bid_close:
                spread = offer_close - bid_close
                bid = bid_close
                offer = offer_close
            else:
                spread = max(1.0, float(bar.get("close") or mid) * 0.0001)
                bid = mid - spread / 2.0
                offer = mid + spread / 2.0
            quote = Quote(time=_parse_bar_time(bar.get("time", "")), bid=bid, offer=offer)
            signal_engine.add_quote(market, quote)
            count += 1
        log_engine(f"OHLC bootstrap: injected {count} bars into SignalEngine for {epic}")
        return count
    except Exception as e:
        log_engine(f"OHLC bootstrap warning: {type(e).__name__}: {e}")
        return 0
