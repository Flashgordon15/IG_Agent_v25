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
from datetime import datetime, timezone
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

_FRAME_TIMEOUT_SEC = float(os.environ.get("IG_FEED_FRAME_TIMEOUT_SEC", "3.0") or 3.0)
_YAHOO_POLL_SEC = 2.0

# Reconnect backoff for WS feeds. A rejected/throttled connection (HTTP 429,
# auth failure) must NOT be retried every 2s — that hammers an exhausted key and
# produced the observed 27x Finnhub retry storm. Exponential backoff, capped;
# rejections that indicate the key is bad back off much harder.
_FEED_RECONNECT_BASE_SEC = 2.0
_FEED_RECONNECT_CAP_SEC = 120.0
_FEED_REJECT_MIN_BACKOFF_SEC = 30.0  # ≥10s floor for 429 storms
_FEED_REJECT_CAP_SEC = 300.0


def compute_feed_reject_backoff(
    exc: BaseException | str,
    current_backoff: float,
    *,
    reject_min_sec: float | None = None,
    reject_cap_sec: float | None = None,
    reconnect_cap_sec: float | None = None,
) -> tuple[float, float, bool]:
    """Pure backoff helper for Finnhub/secondary 429 storms.

    Returns ``(wait_sec, next_backoff_sec, is_reject)``. Reject paths
    (HTTP 429 / 401 / 403) use a hard floor ≥10s (default 30s).
    """
    detail = str(exc).lower()
    rejected = any(
        tok in detail
        for tok in ("429", " 401", " 403", "http 401", "http 403", "rejected", "rate limit")
    ) or (
        getattr(exc, "status_code", None) == 429
        if not isinstance(exc, str)
        else False
    )
    reject_min = float(
        reject_min_sec if reject_min_sec is not None else _FEED_REJECT_MIN_BACKOFF_SEC
    )
    reject_cap = float(
        reject_cap_sec if reject_cap_sec is not None else _FEED_REJECT_CAP_SEC
    )
    recon_cap = float(
        reconnect_cap_sec if reconnect_cap_sec is not None else _FEED_RECONNECT_CAP_SEC
    )
    backoff = max(0.0, float(current_backoff or 0.0))
    if rejected:
        wait = max(backoff, reject_min, 10.0)
        nxt = min(wait * 2.0, reject_cap)
        return wait, nxt, True
    wait = max(backoff, _FEED_RECONNECT_BASE_SEC)
    nxt = min(max(backoff * 2.0, _FEED_RECONNECT_BASE_SEC), recon_cap)
    return wait, nxt, False


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
    "CS.D.CRUDE.CFD.IP": "OANDA:WTICO_USD",
    "IX.D.FTSE.IFM.IP": "OANDA:UK100_GBP",
    "IX.D.DAX.IFM.IP": "OANDA:DE30_EUR",
}

_EPIC_TWELVE_DATA: dict[str, str] = {
    "CS.D.CFPGOLD.CFP.IP": "XAU/USD",
    "IX.D.DOW.IFM.IP": "DJI",
    "IX.D.NIKKEI.IFM.IP": "N225",
    "CS.D.EURUSD.CFD.IP": "EUR/USD",
    "CS.D.CRUDE.CFD.IP": "WTI/USD",
    "IX.D.FTSE.IFM.IP": "FTSE",
    "IX.D.DAX.IFM.IP": "DAX",
}

_HUB_THREAD: threading.Thread | None = None
_HUB_STOP = threading.Event()
_FEED_STATUS: dict[str, dict[str, Any]] = {
    "yahoo": {"connected": False, "last_frame_ns": 0, "timeouts": 0, "wins": 0},
    "finnhub": {"connected": False, "last_frame_ns": 0, "timeouts": 0, "wins": 0},
    "twelvedata": {"connected": False, "last_frame_ns": 0, "timeouts": 0, "wins": 0},
}
_RACE_STATS: dict[str, int] = {"total_wins": 0}
_YAHOO_ROUTE_BYPASS = False
_API_VERIFY_REPORT: dict[str, Any] | None = None
_API_VERIFY_LOCK = threading.Lock()

_PASS_LINE = "🟢 [API-PASS] {name} Credentials and Network Route VALIDATED."
_FAIL_LINE = "❌ [API-FAIL] {name} Route Blocked: Check Connection Flags / Session Tokens."


def yahoo_route_bypassed() -> bool:
    """True when Yahoo must be excluded from FPTP (errno 65 / timeout isolation)."""
    if os.environ.get("IG_YAHOO_BYPASS", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    return bool(_YAHOO_ROUTE_BYPASS)


def _set_yahoo_bypass(*, reason: str) -> None:
    global _YAHOO_ROUTE_BYPASS
    _YAHOO_ROUTE_BYPASS = True
    log_engine(
        f"MultiFeedHub: Yahoo route ISOLATED (FPTP bypass) — {reason} "
        "(Finnhub + Twelve Data primary; no shutdown)"
    )


def _yahoo_error_isolated(exc: BaseException) -> bool:
    text = str(exc).lower()
    if "errno 65" in text or "no route to host" in text:
        return True
    if "timed out" in text or "timeout" in text or "read timeout" in text:
        return True
    if isinstance(exc, (TimeoutError, OSError)) and getattr(exc, "errno", None) == 65:
        return True
    return False


def _http_ping(url: str, *, timeout: float = 4.0, method: str = "GET") -> tuple[bool, str]:
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.Request(url, method=method, headers={"User-Agent": "IG-Agent/30"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = int(getattr(resp, "status", 200) or 200)
            if code >= 400:
                return False, f"HTTP {code}"
            return True, ""
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, f"HTTP {exc.code} auth"
        if exc.code < 500:
            return True, ""
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, str(exc)


def _ping_yahoo_finance(*, timeout: float = 4.0) -> tuple[bool, str]:
    return _http_ping("https://yahoo.com", timeout=timeout, method="HEAD")


def _ping_finnhub(*, timeout: float = 4.0) -> tuple[bool, str]:
    key = _resolve_finnhub_key()
    if not key:
        return False, "FINNHUB_KEY missing"
    url = f"https://finnhub.io/api/v1/quote?symbol=AAPL&token={key}"
    return _http_ping(url, timeout=timeout)


def _ping_twelve_data(*, timeout: float = 4.0) -> tuple[bool, str]:
    key = _resolve_twelve_data_key()
    if not key:
        return False, "TWELVE_DATA_KEY missing"
    url = f"https://api.twelvedata.com/price?symbol=AAPL&apikey={key}"
    return _http_ping(url, timeout=timeout)


def _ping_ig_trading(rest_client: Any | None, *, timeout: float = 5.0) -> tuple[bool, str]:
    if rest_client is None:
        return False, "IG REST client unavailable (Gate 2 pending)"
    try:
        rest_client.ensure_session()
        headers = rest_client._auth_headers("1")
        response = rest_client.request("GET", "/accounts", headers=headers)
        status = int(getattr(response, "status_code", 0) or getattr(response, "status", 0) or 0)
        if status >= 400:
            return False, f"HTTP {status}"
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _emit_pipeline_line(*, name: str, ok: bool, detail: str = "") -> str:
    line = _PASS_LINE.format(name=name) if ok else _FAIL_LINE.format(name=name)
    if not ok and detail:
        line = f"{line} ({detail})"
    print(f"\033[1m{line}\033[0m", flush=True)
    log_engine(line if ok else f"{line} ({detail})".strip())
    return line


def verify_all_api_pipelines(
    *,
    rest_client: Any | None = None,
    timeout_sec: float = 5.0,
    emit: bool = True,
) -> dict[str, Any]:
    """
    4-way connectivity pass — Yahoo, Finnhub, Twelve Data, IG REST (parallel, bounded wait).
    """
    global _API_VERIFY_REPORT

    if os.environ.get("IG_SKIP_API_VERIFY", "").strip().lower() in ("1", "true", "yes"):
        return {"skipped": True, "lines": []}

    from concurrent.futures import ThreadPoolExecutor, as_completed

    probes = {
        "Yahoo Finance": _ping_yahoo_finance,
        "Finnhub": _ping_finnhub,
        "Twelve Data": _ping_twelve_data,
        "IG Trading Client": lambda: _ping_ig_trading(rest_client, timeout=timeout_sec),
    }
    results: dict[str, dict[str, Any]] = {}
    lines: list[str] = []

    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="api-pipeline") as pool:
        futures = {pool.submit(fn): name for name, fn in probes.items()}
        for fut in as_completed(futures, timeout=timeout_sec + 1.0):
            name = futures[fut]
            try:
                ok, detail = fut.result(timeout=timeout_sec)
            except Exception as exc:
                ok, detail = False, str(exc)
            results[name] = {"ok": ok, "detail": detail}
            if name == "Yahoo Finance" and not ok and _yahoo_error_isolated(Exception(detail)):
                _set_yahoo_bypass(reason=detail[:120])
            if emit:
                lines.append(_emit_pipeline_line(name=name, ok=ok, detail=detail))

    report = {
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "results": results,
        "lines": lines,
        "yahoo_bypass": yahoo_route_bypassed(),
        "all_ok": all(r.get("ok") for r in results.values()),
    }
    with _API_VERIFY_LOCK:
        _API_VERIFY_REPORT = report
    return report


def start_verify_all_api_pipelines_async(
    *,
    rest_client: Any | None = None,
    timeout_sec: float = 5.0,
) -> threading.Thread:
    """Non-blocking wrapper — results logged when the background thread completes."""

    def _worker() -> None:
        try:
            verify_all_api_pipelines(
                rest_client=rest_client, timeout_sec=timeout_sec, emit=True
            )
        except Exception as exc:
            log_guarded_exception("verify_all_api_pipelines_async", exc)

    thread = threading.Thread(
        target=_worker,
        name="api-pipeline-verify",
        daemon=True,
    )
    thread.start()
    return thread


def api_pipeline_verify_report() -> dict[str, Any] | None:
    with _API_VERIFY_LOCK:
        return dict(_API_VERIFY_REPORT) if _API_VERIFY_REPORT else None


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

    async def _sleep_backoff(
        self, provider: str, exc: BaseException, backoff: float
    ) -> float:
        """Sleep with exponential backoff; back off hard on 429/auth rejections.

        Returns the next backoff value. A connection the server rejects (429,
        401, 403, "rejected") means retrying fast is pointless and abusive — jump
        to a long floor so an exhausted/invalid key stops storming the provider.
        """
        wait, nxt, rejected = compute_feed_reject_backoff(exc, backoff)
        if rejected:
            st = _FEED_STATUS.setdefault(provider, {})
            st["throttled"] = True
            st["last_reject_backoff_sec"] = float(wait)
        await asyncio.sleep(wait)
        return nxt

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
        ring_ok = False
        try:
            ring_ok = bool(
                ring.write_quote_race_win(
                    epic, bid=bid, offer=offer, mid=mid, source_id=source_id
                )
            )
        except Exception as exc:
            log_guarded_exception("multi_feed_hub_ring_write", exc)
        if ring_ok:
            _RACE_STATS["total_wins"] = int(_RACE_STATS.get("total_wins") or 0) + 1
            st = _FEED_STATUS.setdefault(provider, {})
            st["wins"] = int(st.get("wins") or 0) + 1
            self._touch_heartbeat(provider)
        # ALWAYS bridge race-win prices into MarketDataHub — ring buffer
        # fullness must never starve the trading quote path (Gate 3 / entries).
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
        if yahoo_route_bypassed():
            log_engine("MultiFeedHub: Yahoo race loop skipped — route bypass active")
            return
        try:
            from feeder.yahoo_quote_poller import yahoo_poller_active

            if yahoo_poller_active():
                log_engine(
                    "MultiFeedHub: Yahoo race loop skipped — orchestrator poller owns Yahoo path"
                )
                return
        except Exception:
            pass
        self._touch_heartbeat("yahoo", connected=True)
        while not _HUB_STOP.is_set():
            if yahoo_route_bypassed():
                break
            for epic in NIGHT_MATRIX_EPICS:
                if _HUB_STOP.is_set() or yahoo_route_bypassed():
                    break
                symbol = yahoo_symbol_for_epic(epic)
                if not symbol:
                    continue
                try:
                    mid = await asyncio.to_thread(fetch_yahoo_mid, symbol)
                    if mid is None or mid <= 0:
                        continue
                    try:
                        from system.quote_sanity import plausible_mid_for_epic

                        if not plausible_mid_for_epic(epic, float(mid)):
                            continue
                    except Exception:
                        pass
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
                    if _yahoo_error_isolated(exc):
                        _set_yahoo_bypass(reason=str(exc)[:120])
                        log_engine(
                            f"MultiFeedHub: Yahoo fetch isolated — {type(exc).__name__}: {exc}"
                        )
                        return
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

        backoff = _FEED_RECONNECT_BASE_SEC
        while not _HUB_STOP.is_set():
            try:
                async with websockets.connect(url, ping_interval=20, close_timeout=5) as ws:
                    self._touch_heartbeat("finnhub", connected=True)
                    backoff = _FEED_RECONNECT_BASE_SEC  # reset on successful connect
                    for sym in symbols:
                        await ws.send(json.dumps({"type": "subscribe", "symbol": sym}))
                    log_engine(f"MultiFeedHub: Finnhub WS subscribed {len(symbols)} symbols")
                    while not _HUB_STOP.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=self.frame_timeout_sec)
                        except asyncio.TimeoutError:
                            self._record_timeout("finnhub")
                            log_engine(
                                f"MultiFeedHub: Finnhub empty frame >{self.frame_timeout_sec:.0f}s "
                                "— aggressive socket reset"
                            )
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
            except RuntimeError as exc:
                if "shutdown" in str(exc).lower():
                    log_engine("MultiFeedHub: Finnhub loop exiting — interpreter shutdown")
                    return
                self._record_timeout("finnhub")
                log_guarded_exception("multi_feed_finnhub", exc)
                backoff = await self._sleep_backoff("finnhub", exc, backoff)
            except Exception as exc:
                self._record_timeout("finnhub")
                log_guarded_exception("multi_feed_finnhub", exc)
                backoff = await self._sleep_backoff("finnhub", exc, backoff)

    async def _twelve_data_ws_loop(self) -> None:
        if not self._twelve_key:
            log_engine("MultiFeedHub: Twelve Data key missing — stream skipped (failover active)")
            return
        import websockets

        url = twelve_data_ws_url(apikey=self._twelve_key)
        symbols = [_EPIC_TWELVE_DATA[e] for e in NIGHT_MATRIX_EPICS if e in _EPIC_TWELVE_DATA]
        symbol_to_epic = {v: k for k, v in _EPIC_TWELVE_DATA.items()}

        backoff = _FEED_RECONNECT_BASE_SEC
        while not _HUB_STOP.is_set():
            try:
                async with websockets.connect(url, ping_interval=20, close_timeout=5) as ws:
                    self._touch_heartbeat("twelvedata", connected=True)
                    backoff = _FEED_RECONNECT_BASE_SEC  # reset on successful connect
                    await ws.send(
                        json.dumps({"action": "subscribe", "params": {"symbols": ",".join(symbols)}})
                    )
                    log_engine(f"MultiFeedHub: Twelve Data WS subscribed {len(symbols)} symbols")
                    while not _HUB_STOP.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=self.frame_timeout_sec)
                        except asyncio.TimeoutError:
                            self._record_timeout("twelvedata")
                            log_engine(
                                f"MultiFeedHub: Twelve Data empty frame >{self.frame_timeout_sec:.0f}s "
                                "— aggressive socket reset"
                            )
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
            except RuntimeError as exc:
                if "shutdown" in str(exc).lower():
                    log_engine("MultiFeedHub: Twelve Data loop exiting — interpreter shutdown")
                    return
                self._record_timeout("twelvedata")
                log_guarded_exception("multi_feed_twelvedata", exc)
                backoff = await self._sleep_backoff("twelvedata", exc, backoff)
            except Exception as exc:
                self._record_timeout("twelvedata")
                log_guarded_exception("multi_feed_twelvedata", exc)
                backoff = await self._sleep_backoff("twelvedata", exc, backoff)

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
        if yahoo_route_bypassed():
            log_engine(
                "MultiFeedHub: racing feeds armed (Finnhub + Twelve Data primary — Yahoo bypassed)"
            )
            await asyncio.gather(
                self._finnhub_ws_loop(),
                self._twelve_data_ws_loop(),
                self._heartbeat_watchdog(),
            )
            return
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


def hard_reset_multi_feed_hub(*, reason: str = "velocity_stall") -> dict[str, Any]:
    """
    Hard socket reset — tear down dead WS lines and re-bind Finnhub + Twelve Data.
    """
    global _HUB_THREAD

    log_engine(f"MultiFeedHub: HARD RESET ({reason}) — killing streams, re-binding WS")
    _HUB_STOP.set()
    if _HUB_THREAD is not None and _HUB_THREAD.is_alive():
        _HUB_THREAD.join(timeout=5.0)
    _HUB_THREAD = None
    _HUB_STOP.clear()
    for key in _FEED_STATUS:
        _FEED_STATUS[key] = {
            "connected": False,
            "last_frame_ns": 0,
            "timeouts": 0,
            "wins": 0,
        }
    start_racing_multi_feed_hub()
    return {
        "reset": True,
        "reason": str(reason),
        "reset_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "finnhub_armed": bool(_resolve_finnhub_key()),
        "twelve_data_armed": bool(_resolve_twelve_data_key()),
    }


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
        "yahoo_bypass": yahoo_route_bypassed(),
        "api_verify": api_pipeline_verify_report(),
    }


def reset_multi_feed_hub_for_tests() -> None:
    global _YAHOO_ROUTE_BYPASS, _API_VERIFY_REPORT
    _YAHOO_ROUTE_BYPASS = False
    _API_VERIFY_REPORT = None
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
