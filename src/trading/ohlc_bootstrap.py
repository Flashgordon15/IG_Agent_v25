"""Seed SignalEngine quote history from IG REST OHLC on session open."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

from data.models import Quote
from signals.signal_engine import SignalEngine
from system.engine_log import log_engine

if TYPE_CHECKING:
    from trading.environment_scorer import EnvironmentScorer

# IG snapshotTime / snapshotTimeUTC, e.g. 2026/05/28:14:30:00 or 2026-05-28T14:30:00
_IG_SNAPSHOT_TIME = re.compile(
    r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})[T:\s](\d{1,2}):(\d{2})(?::(\d{2}))?"
)


def _parse_bar_time(raw: str) -> datetime:
    s = str(raw or "").strip()
    if not s:
        return datetime.now()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if "." in s and "+" not in s and "Z" not in s.upper():
        head, _, tail = s.partition(".")
        if tail.isdigit() or (len(tail) >= 3 and tail[:3].isdigit()):
            s = head
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        m = _IG_SNAPSHOT_TIME.match(s)
        if not m:
            return datetime.now()
        y, mo, d, h, mi, sec = m.groups()
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(sec or 0))
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
    environment_scorer: EnvironmentScorer | None = None,
) -> int:
    """Inject historical bars into SignalEngine; returns count injected (0 on failure)."""
    try:
        if rest_client is None or signal_engine is None:
            return 0
        fetch = getattr(rest_client, "fetch_price_history", None)
        if not callable(fetch):
            log_engine("OHLC bootstrap: fetch_price_history unavailable")
            return 0
        from system.rest_api_budget import ohlc_bootstrap_rest_window

        with ohlc_bootstrap_rest_window():
            bars = fetch(epic, resolution=resolution, num_points=num_points)
        if not bars:
            log_engine(f"OHLC bootstrap: no bars returned for {epic}")
            return 0
        seeded: list[Quote] = []
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
            seeded.append(
                Quote(time=_parse_bar_time(bar.get("time", "")), bid=bid, offer=offer)
            )
        count = signal_engine.seed_ohlc_history(market, seeded, aliases=[epic])
        if count <= 0:
            log_engine(f"OHLC bootstrap: no valid bars for {epic}")
            return 0
        if environment_scorer is not None:
            environment_scorer.on_ohlc_bootstrapped(market)
        log_engine(
            f"OHLC bootstrap: injected {count} bars into SignalEngine for {epic} "
            f"(market={market})"
        )
        return count
    except Exception as e:
        log_engine(f"OHLC bootstrap warning: {type(e).__name__}: {e}")
        return 0
