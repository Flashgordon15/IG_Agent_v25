"""
Yahoo Finance reference quote poller — publishes bid/offer into MarketDataHub.

Signal and display plane only; IG REST remains authoritative for order execution.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from urllib.parse import quote as url_quote

import requests

from data.ohlc_yahoo_seeder import EPIC_YAHOO_MAP, default_spread_for_yahoo_symbol
from system.engine_log import log_engine
from system.market_data_hub import QuoteSnapshot, get_market_data_hub

_YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
# Spark batch schema — one request covers every polled symbol (1 rate-limit
# token per poll cycle instead of one per epic).
_YAHOO_SPARK = "https://query1.finance.yahoo.com/v7/finance/spark"
_DEFAULT_TIMEOUT_SEC = 1.5
# Generous socket cap for the background batch poll: in-process GIL contention
# adds seconds of scheduling latency on top of the ~100ms network round trip,
# and this path never blocks the trading sweep.
_BATCH_TIMEOUT_SEC = 5.0
_USER_AGENT = "IG-Agent-Apex/30.0"
_ERROR_LOG_INTERVAL_SEC = 60.0
_last_fetch_error_log: dict[str, float] = {}
_RATE_LIMIT_BACKOFF_SEC = 0.0
_RATE_LIMIT_UNTIL_MONO = 0.0
_MAX_RATE_LIMIT_BACKOFF_SEC = 120.0

_POLLER: YahooQuotePoller | None = None
_POLLER_LOCK = threading.Lock()


@dataclass(frozen=True)
class YahooQuoteSample:
    epic: str
    symbol: str
    mid: float
    bid: float
    offer: float
    source: str = "yahoo"


def yahoo_symbol_for_epic(epic: str) -> str | None:
    mapping = EPIC_YAHOO_MAP.get(str(epic or "").strip())
    if not mapping:
        return None
    return str(mapping[0])


def yahoo_rate_limited() -> bool:
    return time.monotonic() < _RATE_LIMIT_UNTIL_MONO


def yahoo_rate_limit_backoff_sec() -> float:
    return float(_RATE_LIMIT_BACKOFF_SEC)


def _apply_yahoo_rate_limit_backoff(*, status_code: int | None = None) -> None:
    global _RATE_LIMIT_BACKOFF_SEC, _RATE_LIMIT_UNTIL_MONO
    if status_code == 429:
        _RATE_LIMIT_BACKOFF_SEC = min(
            _MAX_RATE_LIMIT_BACKOFF_SEC,
            max(5.0, _RATE_LIMIT_BACKOFF_SEC * 2.0 if _RATE_LIMIT_BACKOFF_SEC else 8.0),
        )
    else:
        _RATE_LIMIT_BACKOFF_SEC = min(
            _MAX_RATE_LIMIT_BACKOFF_SEC,
            max(3.0, _RATE_LIMIT_BACKOFF_SEC * 1.5 if _RATE_LIMIT_BACKOFF_SEC else 5.0),
        )
    _RATE_LIMIT_UNTIL_MONO = time.monotonic() + _RATE_LIMIT_BACKOFF_SEC
    log_engine(
        f"YahooQuotePoller: rate-limit backoff {_RATE_LIMIT_BACKOFF_SEC:.0f}s "
        f"(status={status_code or 'error'})"
    )


def _clear_yahoo_rate_limit_backoff() -> None:
    global _RATE_LIMIT_BACKOFF_SEC, _RATE_LIMIT_UNTIL_MONO
    if _RATE_LIMIT_BACKOFF_SEC > 0:
        _RATE_LIMIT_BACKOFF_SEC = max(0.0, _RATE_LIMIT_BACKOFF_SEC * 0.5)
    if _RATE_LIMIT_BACKOFF_SEC <= 1.0:
        _RATE_LIMIT_BACKOFF_SEC = 0.0
        _RATE_LIMIT_UNTIL_MONO = 0.0


def fetch_yahoo_mid(
    symbol: str,
    *,
    timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
    token_wait_sec: float = 5.0,
) -> float | None:
    """Fetch latest regular-market price from Yahoo chart v8 API.

    ``token_wait_sec=0`` makes the rate-limit token acquire non-blocking —
    latency-critical callers (the 500ms strategy sweep) must fail over to the
    hub snapshot instantly instead of parking a worker for 5s per epic, which
    stretched sweep iterations to 10-15s whenever the yahoo bucket ran dry.
    """
    if yahoo_rate_limited():
        return None
    try:
        from system.chaos_guardian import acquire_outbound_token

        if not acquire_outbound_token("yahoo", max_wait_sec=max(0.0, float(token_wait_sec))):
            return None
    except Exception:
        pass
    url = _YAHOO_CHART.format(symbol=url_quote(symbol, safe=""))
    try:
        response = requests.get(
            url,
            timeout=timeout_sec,
            headers={"User-Agent": _USER_AGENT},
        )
        if response.status_code == 429:
            _apply_yahoo_rate_limit_backoff(status_code=429)
            return None
        response.raise_for_status()
        payload = response.json()
        _clear_yahoo_rate_limit_backoff()
    except Exception as exc:
        now = time.monotonic()
        last = _last_fetch_error_log.get(symbol, 0.0)
        if now - last >= _ERROR_LOG_INTERVAL_SEC:
            _last_fetch_error_log[symbol] = now
            log_engine(
                f"YahooQuotePoller fetch failed symbol={symbol}: "
                f"{type(exc).__name__}: {exc}"
            )
        return None

    result = payload.get("chart", {}).get("result")
    if not result or not isinstance(result[0], dict):
        return None
    meta = result[0].get("meta") or {}
    price = meta.get("regularMarketPrice")
    if price is None:
        try:
            closes = result[0]["indicators"]["quote"][0].get("close") or []
            valid = [float(c) for c in closes if c is not None]
            if valid:
                price = valid[-1]
        except (KeyError, IndexError, TypeError, ValueError):
            price = None
    try:
        mid = float(price)
    except (TypeError, ValueError):
        return None
    return mid if mid > 0 else None


def fetch_yahoo_mids_batch(
    symbols: list[str] | tuple[str, ...],
    *,
    timeout_sec: float = _BATCH_TIMEOUT_SEC,
    token_wait_sec: float = 5.0,
) -> dict[str, float]:
    """Fetch mids for many symbols in ONE spark request (one rate-limit token).

    The per-symbol chart endpoint cost N tokens + N sequential round trips per
    poll cycle; at 7 epics every 3s that alone saturated the yahoo budget and
    left zero headroom, so every other consumer logged token exhaustion.
    """
    wanted = [str(s).strip() for s in symbols if str(s or "").strip()]
    if not wanted:
        return {}
    if yahoo_rate_limited():
        return {}
    try:
        from system.chaos_guardian import acquire_outbound_token

        if not acquire_outbound_token("yahoo", max_wait_sec=max(0.0, float(token_wait_sec))):
            return {}
    except Exception:
        pass
    try:
        response = requests.get(
            _YAHOO_SPARK,
            params={
                "symbols": ",".join(wanted),
                "range": "1d",
                "interval": "5m",
            },
            timeout=timeout_sec,
            headers={"User-Agent": _USER_AGENT},
        )
        if response.status_code == 429:
            _apply_yahoo_rate_limit_backoff(status_code=429)
            return {}
        response.raise_for_status()
        payload = response.json()
        _clear_yahoo_rate_limit_backoff()
    except Exception as exc:
        now = time.monotonic()
        last = _last_fetch_error_log.get("__spark__", 0.0)
        if now - last >= _ERROR_LOG_INTERVAL_SEC:
            _last_fetch_error_log["__spark__"] = now
            log_engine(
                f"YahooQuotePoller spark batch failed ({len(wanted)} symbols): "
                f"{type(exc).__name__}: {exc}"
            )
        return {}

    results = payload.get("spark", {}).get("result") or payload.get("result") or []
    mids: dict[str, float] = {}
    for row in results:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "")
        responses = row.get("response") or []
        meta = responses[0].get("meta") if responses and isinstance(responses[0], dict) else None
        price = (meta or {}).get("regularMarketPrice")
        try:
            mid = float(price)
        except (TypeError, ValueError):
            continue
        if sym and mid > 0:
            mids[sym] = mid
    return mids


def yahoo_quote_from_mid(epic: str, mid: float, symbol: str) -> YahooQuoteSample | None:
    """Build a Yahoo sample — returns None when mid is outside the epic band."""
    from system.quote_sanity import plausible_mid_for_epic

    try:
        m = float(mid)
    except (TypeError, ValueError):
        return None
    if not plausible_mid_for_epic(epic, m):
        log_engine(
            f"YahooQuotePoller: reject implausible mid epic={epic} "
            f"symbol={symbol} mid={m}"
        )
        return None
    spread = default_spread_for_yahoo_symbol(symbol, m)
    half = spread * 0.5
    return YahooQuoteSample(
        epic=epic,
        symbol=symbol,
        mid=m,
        bid=m - half,
        offer=m + half,
    )


def fetch_yahoo_quote(
    epic: str,
    *,
    timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
    token_wait_sec: float = 5.0,
) -> YahooQuoteSample | None:
    symbol = yahoo_symbol_for_epic(epic)
    if not symbol:
        return None
    mid = fetch_yahoo_mid(symbol, timeout_sec=timeout_sec, token_wait_sec=token_wait_sec)
    if mid is None:
        return None
    return yahoo_quote_from_mid(epic, mid, symbol)


class YahooQuotePoller:
    """Background poller publishing Yahoo reference quotes to MarketDataHub."""

    def __init__(self, *, poll_sec: float = 3.0, timeout_sec: float = _DEFAULT_TIMEOUT_SEC) -> None:
        self._poll_sec = max(1.0, float(poll_sec))
        self._timeout_sec = max(0.2, float(timeout_sec))
        self._epics: tuple[str, ...] = ()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._running = False
        self._stats = {"polls": 0, "published": 0, "errors": 0}

    @property
    def running(self) -> bool:
        return self._running

    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def _should_skip_publish(self, epic: str, hub: Any) -> bool:
        """Prefer fresher Finnhub/TwelveData hub quotes over Yahoo hammering."""
        try:
            snap = hub.get_snapshot(epic)
            if snap is None or float(getattr(snap, "bid", 0) or 0) <= 0:
                return False
            src = str(getattr(snap, "source", "") or "").lower()
            if src in ("yahoo", "synthetic", "synthetic_hydration", "replay"):
                return False
            age = float(snap.age_seconds()) if hasattr(snap, "age_seconds") else 999.0
            # Fresh non-Yahoo race winner — do not burn Yahoo / overwrite with stale.
            return age <= 4.0
        except Exception:
            return False

    def poll_epic(self, epic: str) -> QuoteSnapshot | None:
        self._stats["polls"] += 1
        hub = get_market_data_hub()
        if self._should_skip_publish(epic, hub):
            return hub.get_snapshot(epic)
        sample = fetch_yahoo_quote(epic, timeout_sec=self._timeout_sec)
        if sample is None:
            self._stats["errors"] += 1
            return None
        snap = hub.publish(
            sample.epic,
            sample.bid,
            sample.offer,
            source="yahoo",
        )
        if snap is not None:
            self._stats["published"] += 1
        return snap

    def poll_all(self) -> int:
        """Batch poll — one spark request per cycle, per-epic chart fallback."""
        symbol_by_epic = {
            epic: sym
            for epic in self._epics
            if (sym := yahoo_symbol_for_epic(epic))
        }
        self._stats["polls"] += 1
        hub = get_market_data_hub()
        # Skip Yahoo entirely when the race array already covers the stack.
        pending = {
            epic: sym
            for epic, sym in symbol_by_epic.items()
            if not self._should_skip_publish(epic, hub)
        }
        if not pending:
            return 0
        mids = fetch_yahoo_mids_batch(list(pending.values()))
        published = 0
        for epic, symbol in pending.items():
            mid = mids.get(symbol)
            if mid is None:
                self._stats["errors"] += 1
                continue
            sample = yahoo_quote_from_mid(epic, mid, symbol)
            if sample is None:
                self._stats["errors"] += 1
                continue
            snap = hub.publish(sample.epic, sample.bid, sample.offer, source="yahoo")
            if snap is not None:
                self._stats["published"] += 1
                published += 1
        if not mids:
            # Batch endpoint unavailable — fall back to sequential chart polls.
            for idx, epic in enumerate(pending):
                if self.poll_epic(epic) is not None:
                    published += 1
                if idx + 1 < len(pending) and not yahoo_rate_limited():
                    time.sleep(0.15)
        return published

    def start(self, epics: list[str] | tuple[str, ...]) -> None:
        filtered = tuple(
            e for e in (str(x).strip() for x in epics) if e and yahoo_symbol_for_epic(e)
        )
        if not filtered:
            log_engine("YahooQuotePoller: no mapped epics — not started")
            return
        if self._running:
            self._epics = filtered
            return
        self._epics = filtered
        self._stop.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._loop,
            name="yahoo-quote-poller",
            daemon=True,
        )
        self._thread.start()
        log_engine(
            f"YahooQuotePoller: started poll={self._poll_sec:.1f}s "
            f"epics={len(self._epics)}"
        )

    def stop(self) -> None:
        self._running = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_all()
            except Exception as exc:
                self._stats["errors"] += 1
                log_engine(f"YahooQuotePoller loop error: {type(exc).__name__}: {exc}")
            wait_sec = self._poll_sec
            backoff = yahoo_rate_limit_backoff_sec()
            if backoff > 0:
                wait_sec = max(wait_sec, backoff)
            if self._stop.wait(wait_sec):
                break


def get_yahoo_quote_poller() -> YahooQuotePoller | None:
    return _POLLER


def start_yahoo_quote_poller(
    epics: list[str] | tuple[str, ...],
    *,
    poll_sec: float = 3.0,
    timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
) -> YahooQuotePoller:
    global _POLLER
    with _POLLER_LOCK:
        if _POLLER is None:
            _POLLER = YahooQuotePoller(poll_sec=poll_sec, timeout_sec=timeout_sec)
        _POLLER.start(epics)
        return _POLLER


def stop_yahoo_quote_poller() -> None:
    global _POLLER
    with _POLLER_LOCK:
        if _POLLER is not None:
            _POLLER.stop()
        _POLLER = None


def reset_yahoo_quote_poller_for_tests() -> None:
    global _RATE_LIMIT_BACKOFF_SEC, _RATE_LIMIT_UNTIL_MONO
    _RATE_LIMIT_BACKOFF_SEC = 0.0
    _RATE_LIMIT_UNTIL_MONO = 0.0
    stop_yahoo_quote_poller()


def yahoo_poller_active() -> bool:
    with _POLLER_LOCK:
        return _POLLER is not None and _POLLER.running
