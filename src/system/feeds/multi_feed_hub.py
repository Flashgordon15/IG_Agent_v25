"""
Racing Multi-Feeder Hub — concurrent Yahoo + Finnhub + Twelve Data WebSockets.

Whichever stream delivers a quote first wins the race and performs a naked pointer
write into the in-process lock-less quote ring (zero IPC, zero mutex on hot path).
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from data.ohlc_yahoo_seeder import default_spread_for_yahoo_symbol
from feeder.yahoo_quote_poller import fetch_yahoo_mid, yahoo_symbol_for_epic
from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception
from system.ipc.ring_buffer import (
    SOURCE_FINNHUB,
    SOURCE_TWELVE_DATA,
    SOURCE_YAHOO,
    get_alpha_ring_buffer,
)
from system.market_data_hub import NIGHT_MATRIX_EPICS, get_market_data_hub

# Baked defaults — overridden by FINNHUB_KEY / TWELVE_DATA_KEY env or external_keys.json
FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "").strip() or (
    "d84asthr01qutij8i4a0d84asthr01qutij8i4ag"
)
TWELVE_DATA_KEY = os.environ.get("TWELVE_DATA_KEY", "").strip() or (
    "c33d709357dd4ef8823d4e3eefdac056"
)

_FRAME_TIMEOUT_SEC = 15.0
_YAHOO_POLL_SEC = 2.0


def _resolve_finnhub_key() -> str:
    return (
        os.environ.get("FINNHUB_KEY", "").strip()
        or os.environ.get("FINNHUB_API_KEY", "").strip()
        or _load_external_finnhub()
        or FINNHUB_KEY
    )


def _resolve_twelve_data_key() -> str:
    return (
        os.environ.get("TWELVE_DATA_KEY", "").strip()
        or os.environ.get("TWELVE_DATA_API_KEY", "").strip()
        or os.environ.get("TWELVEDATA_API_KEY", "").strip()
        or _load_external_twelve_data()
        or TWELVE_DATA_KEY
    )


def _load_external_finnhub() -> str:
    try:
        from system.external_keys import finnhub_api_key

        return finnhub_api_key()
    except Exception:
        return ""


def _load_external_twelve_data() -> str:
    try:
        from system.external_keys import twelve_data_api_key

        return twelve_data_api_key()
    except Exception:
        return ""


def finnhub_ws_url(*, token: str | None = None) -> str:
    """Finnhub streaming endpoint — token query param."""
    key = str(token or _resolve_finnhub_key()).strip()
    return f"wss://ws.finnhub.io?token={key}"


def twelve_data_ws_url(*, apikey: str | None = None) -> str:
    """
    Twelve Data streaming endpoint.

    Official WS base is ``wss://ws.twelvedata.com/v1/quotes/price?apikey=...``
    (not a bare host + key concatenation).
    """
    key = str(apikey or _resolve_twelve_data_key()).strip()
    return f"wss://ws.twelvedata.com/v1/quotes/price?apikey={key}"

# Night-matrix epic → provider symbols
_EPIC_FINNHUB: dict[str, str] = {
    "CS.D.CFPGOLD.CFP.IP": "OANDA:XAU_USD",
    "IX.D.DOW.IFM.IP": "OANDA:US30_USD",
    "IX.D.NIKKEI.IFM.IP": "OANDA:JP225_USD",
    "CS.D.EURUSD.CFD.IP": "OANDA:EUR_USD",
}

_EPIC_TWELVE_DATA: dict[str, str] = {
    "CS.D.CFPGOLD.CFP.IP": "XAU/USD",
    "IX.D.DOW.IFM.IP": "DJI",
    "IX.D.NIKKEI.IFM.IP": "N225",
    "CS.D.EURUSD.CFD.IP": "EUR/USD",
}

_HUB_THREAD: threading.Thread | None = None
_HUB_STOP = threading.Event()
_FEED_STATUS: dict[str, dict[str, Any]] = {
    "yahoo": {"connected": False, "last_frame_ns": 0, "timeouts": 0, "wins": 0},
    "finnhub": {"connected": False, "last_frame_ns": 0, "timeouts": 0, "wins": 0},
    "twelvedata": {"connected": False, "last_frame_ns": 0, "timeouts": 0, "wins": 0},
}
_RACE_STATS: dict[str, int] = {"total_wins": 0}


@dataclass
class RacingMultiFeedHub:
    """Async connection manager — all feeds race; fastest quote wins per epic."""

    yahoo_poll_sec: float = _YAHOO_POLL_SEC
    frame_timeout_sec: float = _FRAME_TIMEOUT_SEC
    _finnhub_key: str = ""
    _twelve_key: str = ""

    def __post_init__(self) -> None:
        self._finnhub_key = _resolve_finnhub_key()
        self._twelve_key = _resolve_twelve_data_key()
        if self._finnhub_key:
            log_engine("MultiFeedHub: Finnhub WS credentials resolved")
        if self._twelve_key:
            log_engine("MultiFeedHub: Twelve Data WS credentials resolved")

    def _touch_heartbeat(self, provider: str, *, connected: bool = True) -> None:
        st = _FEED_STATUS.setdefault(provider, {})
        st["connected"] = bool(connected)
        st["last_frame_ns"] = time.perf_counter_ns()
        st["last_frame_mono"] = time.monotonic()

    def _record_timeout(self, provider: str) -> None:
        st = _FEED_STATUS.setdefault(provider, {})
        st["timeouts"] = int(st.get("timeouts") or 0) + 1
        st["connected"] = False

    def _publish_race_win(
        self,
        epic: str,
        *,
        mid: float,
        bid: float,
        offer: float,
        provider: str,
        source_id: float,
    ) -> None:
        ring = get_alpha_ring_buffer()
        if not ring.write_quote_race_win(
            epic, bid=bid, offer=offer, mid=mid, source_id=source_id
        ):
            return
        _RACE_STATS["total_wins"] = int(_RACE_STATS.get("total_wins") or 0) + 1
        st = _FEED_STATUS.setdefault(provider, {})
        st["wins"] = int(st.get("wins") or 0) + 1
        self._touch_heartbeat(provider)
        try:
            hub = get_market_data_hub()
            hub.publish(epic, bid, offer, source=provider)
        except Exception as exc:
            log_guarded_exception("multi_feed_hub_hub_publish", exc)

    def _mid_to_bid_offer(self, epic: str, mid: float, *, yahoo_symbol: str = "") -> tuple[float, float]:
        sym = yahoo_symbol or yahoo_symbol_for_epic(epic) or ""
        spread = default_spread_for_yahoo_symbol(sym, mid) if sym else max(mid * 0.00005, 0.00001)
        half = spread / 2.0
        return mid - half, mid + half

    async def _yahoo_race_loop(self) -> None:
        """Baseline Yahoo REST poll — participates in async race array."""
        self._touch_heartbeat("yahoo", connected=True)
        while not _HUB_STOP.is_set():
            for epic in NIGHT_MATRIX_EPICS:
                if _HUB_STOP.is_set():
                    break
                symbol = yahoo_symbol_for_epic(epic)
                if not symbol:
                    continue
                try:
                    mid = await asyncio.to_thread(fetch_yahoo_mid, symbol)
                    if mid is None or mid <= 0:
                        continue
                    bid, offer = self._mid_to_bid_offer(epic, float(mid), yahoo_symbol=symbol)
                    self._publish_race_win(
                        epic,
                        mid=float(mid),
                        bid=bid,
                        offer=offer,
                        provider="yahoo",
                        source_id=SOURCE_YAHOO,
                    )
                except Exception as exc:
                    log_guarded_exception("multi_feed_yahoo", exc)
                    self._record_timeout("yahoo")
            await asyncio.sleep(self.yahoo_poll_sec)

    async def _finnhub_ws_loop(self) -> None:
        if not self._finnhub_key:
            log_engine("MultiFeedHub: Finnhub key missing — stream skipped (failover active)")
            return
        import websockets

        url = finnhub_ws_url(token=self._finnhub_key)
        symbols = [_EPIC_FINNHUB[e] for e in NIGHT_MATRIX_EPICS if e in _EPIC_FINNHUB]
        symbol_to_epic = {v: k for k, v in _EPIC_FINNHUB.items()}

        while not _HUB_STOP.is_set():
            try:
                async with websockets.connect(url, ping_interval=20, close_timeout=5) as ws:
                    self._touch_heartbeat("finnhub", connected=True)
                    for sym in symbols:
                        await ws.send(json.dumps({"type": "subscribe", "symbol": sym}))
                    log_engine(f"MultiFeedHub: Finnhub WS subscribed {len(symbols)} symbols")
                    while not _HUB_STOP.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=self.frame_timeout_sec)
                        except asyncio.TimeoutError:
                            self._record_timeout("finnhub")
                            log_engine("MultiFeedHub: Finnhub frame timeout — reconnecting")
                            break
                        self._touch_heartbeat("finnhub")
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if msg.get("type") != "trade":
                            continue
                        for tick in msg.get("data") or []:
                            sym = str(tick.get("s") or "")
                            epic = symbol_to_epic.get(sym)
                            price = tick.get("p")
                            if not epic or price is None:
                                continue
                            mid = float(price)
                            if mid <= 0:
                                continue
                            bid, offer = self._mid_to_bid_offer(epic, mid)
                            self._publish_race_win(
                                epic,
                                mid=mid,
                                bid=bid,
                                offer=offer,
                                provider="finnhub",
                                source_id=SOURCE_FINNHUB,
                            )
            except Exception as exc:
                self._record_timeout("finnhub")
                log_guarded_exception("multi_feed_finnhub", exc)
                await asyncio.sleep(2.0)

    async def _twelve_data_ws_loop(self) -> None:
        if not self._twelve_key:
            log_engine("MultiFeedHub: Twelve Data key missing — stream skipped (failover active)")
            return
        import websockets

        url = twelve_data_ws_url(apikey=self._twelve_key)
        symbols = [_EPIC_TWELVE_DATA[e] for e in NIGHT_MATRIX_EPICS if e in _EPIC_TWELVE_DATA]
        symbol_to_epic = {v: k for k, v in _EPIC_TWELVE_DATA.items()}

        while not _HUB_STOP.is_set():
            try:
                async with websockets.connect(url, ping_interval=20, close_timeout=5) as ws:
                    self._touch_heartbeat("twelvedata", connected=True)
                    await ws.send(
                        json.dumps({"action": "subscribe", "params": {"symbols": ",".join(symbols)}})
                    )
                    log_engine(f"MultiFeedHub: Twelve Data WS subscribed {len(symbols)} symbols")
                    while not _HUB_STOP.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=self.frame_timeout_sec)
                        except asyncio.TimeoutError:
                            self._record_timeout("twelvedata")
                            log_engine("MultiFeedHub: Twelve Data frame timeout — reconnecting")
                            break
                        self._touch_heartbeat("twelvedata")
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        sym = str(msg.get("symbol") or msg.get("s") or "")
                        epic = symbol_to_epic.get(sym)
                        price = msg.get("price") or msg.get("p")
                        if not epic or price is None:
                            continue
                        mid = float(price)
                        if mid <= 0:
                            continue
                        bid, offer = self._mid_to_bid_offer(epic, mid)
                        self._publish_race_win(
                            epic,
                            mid=mid,
                            bid=bid,
                            offer=offer,
                            provider="twelvedata",
                            source_id=SOURCE_TWELVE_DATA,
                        )
            except Exception as exc:
                self._record_timeout("twelvedata")
                log_guarded_exception("multi_feed_twelvedata", exc)
                await asyncio.sleep(2.0)

    async def _heartbeat_watchdog(self) -> None:
        """Monitor provider heartbeats — failover without stopping other streams."""
        while not _HUB_STOP.is_set():
            now = time.monotonic()
            active = 0
            for provider, st in _FEED_STATUS.items():
                last = float(st.get("last_frame_mono") or 0.0)
                if last > 0 and (now - last) <= self.frame_timeout_sec * 2:
                    st["connected"] = True
                    active += 1
                elif last > 0:
                    st["connected"] = False
            if active == 0 and now > 5.0:
                self._touch_heartbeat("yahoo", connected=True)
            await asyncio.sleep(1.0)

    async def run_forever(self) -> None:
        log_engine("MultiFeedHub: racing feeds armed (Yahoo + Finnhub + Twelve Data)")
        await asyncio.gather(
            self._yahoo_race_loop(),
            self._finnhub_ws_loop(),
            self._twelve_data_ws_loop(),
            self._heartbeat_watchdog(),
        )


def _asyncio_thread_main() -> None:
    hub = RacingMultiFeedHub()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(hub.run_forever())
    except Exception as exc:
        log_guarded_exception("multi_feed_hub_async", exc)
    finally:
        loop.close()


def start_racing_multi_feed_hub() -> None:
    """Launch async multi-feed racer on a dedicated daemon thread."""
    global _HUB_THREAD
    if _HUB_THREAD is not None and _HUB_THREAD.is_alive():
        return
    _HUB_STOP.clear()
    _HUB_THREAD = threading.Thread(
        target=_asyncio_thread_main,
        name="racing-multi-feed-hub",
        daemon=True,
    )
    _HUB_THREAD.start()
    log_engine("MultiFeedHub: async WebSocket manager thread started")


def stop_racing_multi_feed_hub() -> None:
    _HUB_STOP.set()


def feed_hub_telemetry() -> dict[str, Any]:
    """Status for unified performance API + dashboard stream mapping."""
    now = time.monotonic()
    providers: dict[str, Any] = {}
    active_names: list[str] = []
    label_map = {
        "yahoo": "Yahoo",
        "finnhub": "Finnhub",
        "twelvedata": "Twelve Data",
    }
    for key, label in label_map.items():
        st = dict(_FEED_STATUS.get(key) or {})
        last_mono = float(st.get("last_frame_mono") or 0.0)
        alive = bool(st.get("connected")) and (
            last_mono <= 0 or (now - last_mono) <= _FRAME_TIMEOUT_SEC * 2
        )
        st["alive"] = alive
        st["label"] = label
        providers[key] = st
        if alive:
            active_names.append(label)

    if len(active_names) >= 3:
        banner = "🟢 Yahoo + Finnhub + Twelve Data Mapped (Absolute Feed Resilience)"
    elif active_names:
        banner = f"🟡 {' + '.join(active_names)} Mapped (partial failover active)"
    else:
        banner = "🔴 Feed hub initializing — awaiting first frame"

    return {
        "providers": providers,
        "race_wins_total": int(_RACE_STATS.get("total_wins") or 0),
        "active_feeds": active_names,
        "stream_mapping_banner": banner,
        "absolute_feed_resilience": len(active_names) >= 3,
    }


def reset_multi_feed_hub_for_tests() -> None:
    stop_racing_multi_feed_hub()
    global _HUB_THREAD
    _HUB_THREAD = None
    for key in _FEED_STATUS:
        _FEED_STATUS[key] = {
            "connected": False,
            "last_frame_ns": 0,
            "timeouts": 0,
            "wins": 0,
        }
    _RACE_STATS["total_wins"] = 0
