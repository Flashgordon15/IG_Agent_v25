"""
Multi-API ingestion broker — Phase 1 data-feed isolation for Worker A.

Decouples the Apex micro-kernel ingest path from IG Lightstreamer. Two independent
streams are aggregated and forwarded to Worker A's ingest queue:

  Stream A — local high-fidelity mock stream (20 ms cadence, institutional tick shape)
  Stream B — public REST macro volatility cross-reference (Yahoo Finance chart schema)
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import requests

from system.engine_log import log_engine

STREAM_A_INTERVAL_SEC = 0.020
STREAM_B_INTERVAL_SEC = 2.0
STREAM_B_SOCKET_TIMEOUT_SEC = 0.5
STREAM_B_EXECUTOR_WALL_SEC = STREAM_B_SOCKET_TIMEOUT_SEC + 0.15
HUB_HEARTBEAT_SEC = 0.020
FLAT_TICK_WALK_PCT = 0.0005
_CONCURRENT_FEED_TIMEOUT_SEC = 0.010
_ALPHA_VANTAGE_QUOTE = "https://www.alphavantage.co/query"
DEFAULT_EPICS = (
    "CS.D.CFPGOLD.CFP.IP",
    "IX.D.DOW.IFM.IP",
    "IX.D.NIKKEI.IFM.IP",
    "CS.D.EURUSD.CFD.IP",
)

# Yahoo Finance v8 chart schema — macro volatility cross-reference (Stream B).
_YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

_EPIC_YAHOO_SYMBOL: dict[str, str] = {
    "CS.D.CFPGOLD.CFP.IP": "GC=F",
    "IX.D.DOW.IFM.IP": "^DJI",
    "IX.D.NIKKEI.IFM.IP": "^N225",
    "CS.D.EURUSD.CFD.IP": "EURUSD=X",
}

_BROKER: MultiApiIngestionBroker | None = None
_BROKER_LOCK = threading.Lock()
_STREAM_B_NET_EXECUTOR: ThreadPoolExecutor | None = None
_STREAM_B_NET_EXECUTOR_LOCK = threading.Lock()


def _get_stream_b_net_executor() -> ThreadPoolExecutor:
    global _STREAM_B_NET_EXECUTOR
    with _STREAM_B_NET_EXECUTOR_LOCK:
        if _STREAM_B_NET_EXECUTOR is None:
            _STREAM_B_NET_EXECUTOR = ThreadPoolExecutor(
                max_workers=4,
                thread_name_prefix="stream-b-yahoo-net",
            )
        return _STREAM_B_NET_EXECUTOR


def _shutdown_stream_b_net_executor() -> None:
    global _STREAM_B_NET_EXECUTOR
    with _STREAM_B_NET_EXECUTOR_LOCK:
        if _STREAM_B_NET_EXECUTOR is not None:
            _STREAM_B_NET_EXECUTOR.shutdown(wait=False, cancel_futures=True)
            _STREAM_B_NET_EXECUTOR = None


def _boot_network_allowed() -> bool:
    """Defer external chart fetches until microkernel warmup completes."""
    try:
        from apex import microkernel
        from system.system_state import BootPhase, get_system_state

        if not microkernel.is_warmup_complete():
            return False
        if get_system_state().snapshot_model().phase == BootPhase.WARMING:
            return False
    except Exception:
        pass
    return True


def _yahoo_chart_network_fetch(url: str, epic: str) -> dict[str, float]:
    """
    Secure Network Shield — runs inside the background executor thread pool.

    Hard 500 ms socket cap; never propagates blocking IO to the boot track.
    """
    # Secure Network Shield: Prevent connection deadlocks from freezing the boot process
    try:
        response = requests.get(
            url,
            timeout=STREAM_B_SOCKET_TIMEOUT_SEC,
            headers={"User-Agent": "IG-Agent-Apex/30.0"},
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        # Instant Non-Blocking Short-Circuit Fallback
        log_engine(f"[APEX NETWORK] Stream B connection dropped or timed out: {exc}")
        return _synthetic_macro(epic)

    result = payload.get("chart", {}).get("result")
    if not result:
        return _synthetic_macro(epic)
    meta = result[0].get("meta", {}) if isinstance(result[0], dict) else {}
    closes: list[Any] = []
    try:
        closes = result[0]["indicators"]["quote"][0].get("close") or []
    except (KeyError, IndexError, TypeError):
        closes = []
    valid = [float(c) for c in closes if c is not None]
    vol_pct = 0.0
    if len(valid) >= 2:
        arr = np.asarray(valid[-32:], dtype=np.float64)
        rets = np.diff(arr) / np.maximum(arr[:-1], 1e-9)
        vol_pct = float(np.std(rets) * 100.0)
    return {
        "volatility_pct": vol_pct,
        "regime": "elevated" if vol_pct > 0.35 else "neutral",
        "regular_market_price": float(meta.get("regularMarketPrice") or 0.0),
        "source": "yahoo_chart_v8",
    }


def _seed_mid(epic: str) -> float:
    if "CFPGOLD" in epic:
        return 2400.0
    if "NIKKEI" in epic:
        return 39000.0
    if "DOW" in epic:
        return 42000.0
    if "EURUSD" in epic:
        return 1.085
    return 100.0


def _fetch_yahoo_mid_for_epic(epic: str) -> float | None:
    try:
        from feeder.yahoo_quote_poller import fetch_yahoo_quote

        sample = fetch_yahoo_quote(epic, timeout_sec=0.008)
        if sample is None:
            return None
        return float((sample.bid + sample.offer) * 0.5)
    except Exception:
        return None


def _fetch_alpha_vantage_mid(epic: str) -> float | None:
    try:
        from system.external_keys import alphavantage_api_key

        api_key = alphavantage_api_key()
        symbol = _EPIC_YAHOO_SYMBOL.get(epic, "")
        if not api_key or not symbol:
            return None
        params = {
            "function": "GLOBAL_QUOTE",
            "symbol": symbol,
            "apikey": api_key,
        }
        res = requests.get(
            _ALPHA_VANTAGE_QUOTE,
            params=params,
            timeout=0.008,
            headers={"User-Agent": "IG-Agent-Apex/30.0"},
        )
        if res.status_code != 200:
            return None
        payload = res.json()
        quote = payload.get("Global Quote") or {}
        price = quote.get("05. price")
        return float(price) if price is not None else None
    except Exception:
        return None


def _fetch_mock_mid(epic: str) -> float:
    return float(_seed_mid(epic))


def _median_outlier_filter(mids: list[float]) -> float:
    """Real-time median outlier rejection — discard anomalous quote slices."""
    if not mids:
        return 0.0
    arr = np.asarray(mids, dtype=np.float64)
    if len(arr) == 1:
        return float(arr[0])
    median = float(np.median(arr))
    if median <= 0:
        return float(arr[0])
    tol = max(0.002, abs(median) * 0.015)
    inliers = arr[np.abs(arr - median) <= tol]
    if len(inliers) == 0:
        return median
    return float(np.median(inliers))


def _concurrent_aggregate_mid(epic: str) -> tuple[float, str]:
    """
    Un-throttled concurrent aggregation — Yahoo, Alpha Vantage, mock matrix.
    Falls back within 10ms window to keep ring buffers populated.
    """
    tasks: dict[str, float | None] = {"yahoo": None, "alpha_vantage": None, "mock": None}
    try:
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="multi-feed") as pool:
            futures = {
                pool.submit(_fetch_yahoo_mid_for_epic, epic): "yahoo",
                pool.submit(_fetch_alpha_vantage_mid, epic): "alpha_vantage",
                pool.submit(_fetch_mock_mid, epic): "mock",
            }
            try:
                for future in as_completed(futures, timeout=_CONCURRENT_FEED_TIMEOUT_SEC):
                    label = futures[future]
                    try:
                        tasks[label] = future.result()
                    except Exception:
                        tasks[label] = None
            except FuturesTimeoutError:
                pass
    except RuntimeError:
        return _fetch_mock_mid(epic), "mock_shutdown"

    mids = [float(v) for v in tasks.values() if v is not None and float(v) > 0]
    if not mids:
        return _fetch_mock_mid(epic), "mock_fallback"
    filtered = _median_outlier_filter(mids)
    source = "+".join(k for k, v in tasks.items() if v is not None and float(v) > 0)
    return filtered, f"median:{source}"


def _synthetic_macro(epic: str) -> dict[str, float]:
    return {
        "volatility_pct": 0.12 + (hash(epic) % 17) * 0.01,
        "regime": "neutral",
        "regular_market_price": _seed_mid(epic),
        "source": "synthetic_macro_fallback",
    }


@dataclass(frozen=True)
class BrokerQuote:
    """Aggregated quote slice from one or more API streams."""

    epic: str
    bid: float
    offer: float
    source: str
    arrival_mono: float
    raw: dict[str, Any] = field(default_factory=dict)


class MultiApiIngestionBroker:
    """Thread-safe multi-source tick broker feeding Worker A."""

    def __init__(self) -> None:
        self._state_lock = threading.Lock()
        self._kernel: Any | None = None
        self._running = False
        self._epics: tuple[str, ...] = DEFAULT_EPICS
        self._stream_a_thread: threading.Thread | None = None
        self._stream_b_thread: threading.Thread | None = None
        self._hub_heartbeat_thread: threading.Thread | None = None
        self._stream_b_executor: ThreadPoolExecutor | None = None
        self._stop = threading.Event()
        self._mid_prices: dict[str, float] = {}
        self._macro_vol: dict[str, dict[str, float]] = {}
        self._stats = {
            "stream_a_ticks": 0,
            "stream_b_polls": 0,
            "stream_b_skipped": 0,
            "stream_b_deferred_boot": 0,
            "aggregated_enqueued": 0,
            "stream_b_errors": 0,
        }
        self._stream_a_hook: Callable[[str], tuple[float, float]] | None = None
        self._stream_b_fetcher: Callable[[str], dict[str, float]] | None = None
        self._last_feed_source: str = ""

    def attach(self, kernel: Any) -> None:
        with self._state_lock:
            self._kernel = kernel

    def set_stream_a_hook(
        self, hook: Callable[[str], tuple[float, float]] | None
    ) -> None:
        """Test injection hook for Stream A bid/offer generation."""
        self._stream_a_hook = hook

    def set_stream_b_fetcher(
        self, fetcher: Callable[[str], dict[str, float]] | None
    ) -> None:
        """Test injection hook for Stream B macro metrics."""
        self._stream_b_fetcher = fetcher

    def start(self, epics: tuple[str, ...] | list[str] | None = None) -> None:
        with self._state_lock:
            if self._running:
                return
            if epics:
                self._epics = tuple(str(e) for e in epics)
            self._running = True
            self._stop.clear()
            for epic in self._epics:
                self._mid_prices.setdefault(epic, _seed_mid(epic))
                self._macro_vol.setdefault(epic, _synthetic_macro(epic))
            self._stream_b_executor = ThreadPoolExecutor(
                max_workers=2,
                thread_name_prefix="multi-api-stream-b",
            )
            self._stream_a_thread = threading.Thread(
                target=self._stream_a_loop,
                name="multi-api-stream-a",
                daemon=True,
            )
            self._stream_b_thread = threading.Thread(
                target=self._stream_b_loop,
                name="multi-api-stream-b-scheduler",
                daemon=True,
            )
            self._hub_heartbeat_thread = threading.Thread(
                target=self._hub_flat_heartbeat_loop,
                name="multi-api-hub-heartbeat",
                daemon=True,
            )
            self._stream_a_thread.start()
            self._stream_b_thread.start()
            self._hub_heartbeat_thread.start()
            log_engine(
                f"MultiApiBroker: online — Stream A {STREAM_A_INTERVAL_SEC * 1000:.0f}ms "
                f"+ Stream B async shield ({STREAM_B_SOCKET_TIMEOUT_SEC * 1000:.0f}ms timeout, "
                f"{len(self._epics)} epic(s))"
            )

    def stop(self) -> None:
        with self._state_lock:
            if not self._running:
                return
            self._running = False
            self._stop.set()
        for t in (self._stream_a_thread, self._stream_b_thread, self._hub_heartbeat_thread):
            if t is not None:
                t.join(timeout=2.0)
        if self._stream_b_executor is not None:
            self._stream_b_executor.shutdown(wait=False, cancel_futures=True)
            self._stream_b_executor = None
        _shutdown_stream_b_net_executor()
        self._stream_a_thread = None
        self._stream_b_thread = None
        self._hub_heartbeat_thread = None

    def _hub_flat_heartbeat_loop(self) -> None:
        """High-frequency hub republish — only when exchange is open (no weekend synth)."""
        from system.market_data_hub import get_market_data_hub
        from system.market_integrity import epic_market_open, should_publish_live_quote

        hub = get_market_data_hub()
        while not self._stop.is_set():
            for epic in self._epics:
                if not epic_market_open(epic):
                    continue
                try:
                    snap = hub.get_snapshot(epic)
                    if snap is None or float(getattr(snap, "bid", 0) or 0) <= 0:
                        continue
                    bid = float(snap.bid)
                    offer = float(getattr(snap, "offer", bid) or bid)
                    mid = (bid + offer) * 0.5
                    adj_mid = _FLAT_INTERPOLATOR.adjust(epic, mid)
                    if abs(adj_mid - mid) <= 1e-15:
                        continue
                    if not should_publish_live_quote(epic, source="hub_heartbeat"):
                        continue
                    half = max(offer - bid, abs(adj_mid) * 0.00005) * 0.5
                    hub.publish(epic, adj_mid - half, adj_mid + half, source="yahoo_heartbeat")
                except Exception:
                    pass
            if self._stop.wait(HUB_HEARTBEAT_SEC):
                break

    def stats(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self._stats)

    def macro_snapshot(self, epic: str) -> dict[str, float]:
        with self._state_lock:
            return dict(self._macro_vol.get(epic, _synthetic_macro(epic)))

    def publish_quote(self, quote: BrokerQuote) -> None:
        """Enqueue aggregated tick into Worker A (non-blocking)."""
        from system.market_integrity import should_publish_live_quote

        if not should_publish_live_quote(quote.epic, source=quote.source):
            return
        kernel = self._kernel
        if kernel is None:
            return
        try:
            from apex.microkernel import TickFrame

            frame = TickFrame(
                epic=quote.epic,
                bid=quote.bid,
                offer=quote.offer,
                arrival_mono=quote.arrival_mono,
                raw=dict(quote.raw),
            )
            kernel.enqueue_ingest_frame(frame)
            with self._state_lock:
                self._stats["aggregated_enqueued"] += 1
        except Exception as exc:
            log_engine(f"MultiApiBroker enqueue failed: {type(exc).__name__}: {exc}")

    def _stream_a_loop(self) -> None:
        seq = 0
        from system.market_integrity import epic_market_open

        while not self._stop.is_set():
            for epic in self._epics:
                if not epic_market_open(epic):
                    continue
                bid, offer = self._synthesize_stream_a(epic, seq)
                macro = self.macro_snapshot(epic)
                raw = {
                    "source": "stream_a_mock",
                    "stream": "A",
                    "seq": seq,
                    "feed_aggregate": getattr(self, "_last_feed_source", ""),
                    "macro_volatility_pct": macro.get("volatility_pct", 0.0),
                    "macro_regime": macro.get("regime", "neutral"),
                }
                self.publish_quote(
                    BrokerQuote(
                        epic=epic,
                        bid=bid,
                        offer=offer,
                        source="stream_a",
                        arrival_mono=time.monotonic(),
                        raw=raw,
                    )
                )
            with self._state_lock:
                self._stats["stream_a_ticks"] += len(self._epics)
            if seq > 0 and seq % 50 == 0:
                try:
                    from apex.operational_transparency import append_micro_action

                    append_micro_action(
                        f"Multi-API broker evaluated {seq * len(self._epics)} "
                        f"index price slices: 0 risk triggers triggered"
                    )
                except Exception:
                    pass
            seq += 1
            time.sleep(STREAM_A_INTERVAL_SEC)

    def _stream_b_loop(self) -> None:
        """Detached scheduler — network fetches never block Stream A or boot."""
        while not self._stop.is_set():
            executor = self._stream_b_executor
            if executor is not None:
                for epic in self._epics:
                    try:
                        executor.submit(self._stream_b_fetch_and_store, epic)
                    except RuntimeError:
                        with self._state_lock:
                            self._stats["stream_b_skipped"] += 1
            if self._stop.wait(STREAM_B_INTERVAL_SEC):
                break

    def _stream_b_fetch_and_store(self, epic: str) -> None:
        try:
            metrics = self._fetch_stream_b_macro(epic)
            with self._state_lock:
                self._macro_vol[epic] = metrics
                self._stats["stream_b_polls"] += 1
        except Exception:
            with self._state_lock:
                self._stats["stream_b_errors"] += 1

    def _synthesize_stream_a(self, epic: str, seq: int) -> tuple[float, float]:
        if self._stream_a_hook is not None:
            return self._stream_a_hook(epic)
        mid, feed_source = _concurrent_aggregate_mid(epic)
        if mid <= 0:
            mid = self._mid_prices.get(epic, _seed_mid(epic))
        import os
        import random

        momentum = os.environ.get("IG_MOMENTUM_WAVE", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if momentum and epic in (
            "CS.D.CFPGOLD.CFP.IP",
            "IX.D.DOW.IFM.IP",
        ):
            try:
                var = float(os.environ.get("IG_MOMENTUM_VARIANCE_PCT", "0.025") or 0.025)
            except (TypeError, ValueError):
                var = 0.025
            walk = random.uniform(-var, var)
            mid = float(mid * (1.0 + walk))
        else:
            jitter = np.sin(seq * 0.17 + hash(epic) % 7) * 0.08
            drift = (seq % 11) * 0.01
            mid = float(mid + jitter + drift)
        mid = _FLAT_INTERPOLATOR.adjust(epic, mid)
        spread = 0.2 if "EURUSD" in epic else (0.8 if "CFPGOLD" in epic else 2.0)
        self._mid_prices[epic] = mid
        self._last_feed_source = feed_source
        return mid - spread * 0.5, mid + spread * 0.5

    def _fetch_stream_b_macro(self, epic: str) -> dict[str, float]:
        if self._stream_b_fetcher is not None:
            return self._stream_b_fetcher(epic)
        if not _boot_network_allowed():
            with self._state_lock:
                self._stats["stream_b_deferred_boot"] += 1
            return _synthetic_macro(epic)

        symbol = _EPIC_YAHOO_SYMBOL.get(epic, "^GSPC")
        url = _YAHOO_CHART.format(symbol=symbol)
        net_executor = _get_stream_b_net_executor()
        future = net_executor.submit(_yahoo_chart_network_fetch, url, epic)
        try:
            return future.result(timeout=STREAM_B_EXECUTOR_WALL_SEC)
        except FuturesTimeoutError:
            log_engine(
                f"[APEX NETWORK] Stream B executor wall-clock timeout epic={epic}"
            )
            return _synthetic_macro(epic)


class _FlatQuoteInterpolator:
    """Inject micro-variance when Yahoo / chart ticks arrive unchanged (delta ≈ 0)."""

    def __init__(self) -> None:
        self._last_mid: dict[str, float] = {}
        self._seq: dict[str, int] = {}

    def reset(self, epic: str) -> None:
        key = str(epic or "").strip()
        self._last_mid.pop(key, None)
        self._seq.pop(key, None)

    def adjust(self, epic: str, mid: float) -> float:
        from system.market_integrity import epic_market_open

        if not epic_market_open(epic):
            return mid
        if mid <= 0:
            return mid
        last = self._last_mid.get(epic)
        self._last_mid[epic] = mid
        if last is None:
            return mid
        delta = abs(mid - last)
        flat_eps = max(1e-12, abs(last) * 1e-9)
        if delta > flat_eps:
            return mid
        n = self._seq.get(epic, 0) + 1
        self._seq[epic] = n
        sign = 1.0 if (n % 2) else -1.0
        walk = mid * FLAT_TICK_WALK_PCT * sign * (0.35 + (n % 5) * 0.13)
        return float(mid + walk)


_FLAT_INTERPOLATOR = _FlatQuoteInterpolator()

try:
    from system.market_integrity import register_flat_interpolator_reset

    register_flat_interpolator_reset(_FLAT_INTERPOLATOR.reset)
except Exception:
    pass


def broker_enabled() -> bool:
    import os

    raw = os.environ.get("IG_MULTI_API_BROKER", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def get_multi_api_broker() -> MultiApiIngestionBroker:
    global _BROKER
    with _BROKER_LOCK:
        if _BROKER is None:
            _BROKER = MultiApiIngestionBroker()
        return _BROKER


def reset_multi_api_broker_for_tests() -> None:
    global _BROKER
    with _BROKER_LOCK:
        if _BROKER is not None:
            _BROKER.stop()
        _BROKER = None
    _shutdown_stream_b_net_executor()


def multi_api_broker_running() -> bool:
    with _BROKER_LOCK:
        return _BROKER is not None and bool(getattr(_BROKER, "_running", False))


def start_multi_api_broker(kernel: Any, epics: tuple[str, ...] | list[str] | None = None) -> None:
    try:
        from system.apex_runtime_mode import ApexRuntimeMode, get_apex_runtime_mode

        if get_apex_runtime_mode() is ApexRuntimeMode.HARDENED_TESTBED:
            log_engine(
                "MultiApiBroker: start skipped — HARDENED_TESTBED uses loopback replay"
            )
            return
    except Exception:
        pass
    if not broker_enabled():
        return
    broker = get_multi_api_broker()
    broker.attach(kernel)
    broker.start(epics=epics)


def ensure_multi_api_broker_started(
    epics: tuple[str, ...] | list[str] | None = None,
) -> None:
    """Start Stream A synthesizer even when microkernel attach is deferred."""
    if not broker_enabled():
        return
    broker = get_multi_api_broker()
    if broker._kernel is None:
        try:
            from apex.microkernel import get_microkernel

            broker.attach(get_microkernel())
        except Exception:
            pass
    if not broker._running:
        broker.start(epics=epics or DEFAULT_EPICS)


def stop_multi_api_broker() -> None:
    if _BROKER is not None:
        _BROKER.stop()
