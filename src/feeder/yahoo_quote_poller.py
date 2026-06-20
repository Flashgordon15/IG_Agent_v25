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
_DEFAULT_TIMEOUT_SEC = 0.75
_USER_AGENT = "IG-Agent-Apex/30.0"
_ERROR_LOG_INTERVAL_SEC = 60.0
_last_fetch_error_log: dict[str, float] = {}

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


def fetch_yahoo_mid(symbol: str, *, timeout_sec: float = _DEFAULT_TIMEOUT_SEC) -> float | None:
    """Fetch latest regular-market price from Yahoo chart v8 API."""
    url = _YAHOO_CHART.format(symbol=url_quote(symbol, safe=""))
    try:
        response = requests.get(
            url,
            timeout=timeout_sec,
            headers={"User-Agent": _USER_AGENT},
        )
        response.raise_for_status()
        payload = response.json()
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


def yahoo_quote_from_mid(epic: str, mid: float, symbol: str) -> YahooQuoteSample:
    spread = default_spread_for_yahoo_symbol(symbol, mid)
    half = spread * 0.5
    return YahooQuoteSample(
        epic=epic,
        symbol=symbol,
        mid=mid,
        bid=mid - half,
        offer=mid + half,
    )


def fetch_yahoo_quote(epic: str, *, timeout_sec: float = _DEFAULT_TIMEOUT_SEC) -> YahooQuoteSample | None:
    symbol = yahoo_symbol_for_epic(epic)
    if not symbol:
        return None
    mid = fetch_yahoo_mid(symbol, timeout_sec=timeout_sec)
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

    def poll_epic(self, epic: str) -> QuoteSnapshot | None:
        sample = fetch_yahoo_quote(epic, timeout_sec=self._timeout_sec)
        self._stats["polls"] += 1
        if sample is None:
            self._stats["errors"] += 1
            return None
        snap = get_market_data_hub().publish(
            sample.epic,
            sample.bid,
            sample.offer,
            source="yahoo",
        )
        self._stats["published"] += 1
        return snap

    def poll_all(self) -> int:
        published = 0
        for epic in self._epics:
            if self.poll_epic(epic) is not None:
                published += 1
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
            if self._stop.wait(self._poll_sec):
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
    stop_yahoo_quote_poller()
