"""
Shared live market quotes — one coordinated fetch path for stream, trading loop, and UI.

Reduces duplicate GET /markets calls and keeps bid/offer timestamps aligned.
"""

from __future__ import annotations

import statistics
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np

from data.models import Quote
from ig_api.exceptions import IGAPIError, RateLimitError
from system.engine_log import log_engine
from system.guard.runtime_guard import guard_call, log_guarded_exception
from system.latency_trace import record_stage as _record_latency_stage
from system.packet_validator import (
    REASON_OK,
    reject_packet_code,
    validate_quote_packet_fast,
)

# Passive read-only ECN wholesale baseline observer (RAM-only, no order authority).
_ECN_BASELINE_CACHE: dict[str, dict[str, float | bool]] = {}
_ECN_BASELINE_LOCK = threading.Lock()
_LIQUIDITY_SHIELD_MAX_RATIO = 3.5

# Zero-copy stream tick ring — primitive fields only (no per-tick dict/tuple alloc).
_STREAM_TICK_DTYPE = np.dtype(
    [
        ("epic_id", np.uint16),
        ("bid", np.float64),
        ("offer", np.float64),
        ("qtime", np.float64),
        ("source_id", np.uint8),
    ]
)
_STREAM_RING_CAPACITY = 4096
_SOURCE_IDS: dict[str, int] = {"websocket": 0, "ig_rest": 1, "yahoo": 2, "synthetic": 3}
_SOURCE_ID_REV = {v: k for k, v in _SOURCE_IDS.items()}


class _ZeroCopyStreamRing:
    """SPSC ring buffer — producer enqueues primitive views; consumer batch-drains."""

    __slots__ = ("_buf", "_epic_index", "_write", "_read", "_lock")

    def __init__(self, capacity: int = _STREAM_RING_CAPACITY) -> None:
        self._buf = np.zeros(capacity, dtype=_STREAM_TICK_DTYPE)
        self._epic_index: dict[str, int] = {}
        self._write = 0
        self._read = 0
        self._lock = threading.Lock()

    def _epic_id(self, epic: str) -> int:
        key = str(epic or "").strip()
        if key not in self._epic_index:
            self._epic_index[key] = len(self._epic_index) + 1
        return self._epic_index[key]

    def epic_for_id(self, epic_id: int) -> str:
        for name, idx in self._epic_index.items():
            if idx == int(epic_id):
                return name
        return ""

    def push(
        self,
        epic: str,
        bid: float,
        offer: float,
        *,
        source: str = "websocket",
        quote_time: float | None = None,
    ) -> bool:
        with self._lock:
            idx = self._write % len(self._buf)
            row = self._buf[idx]
            row["epic_id"] = self._epic_id(epic)
            row["bid"] = float(bid)
            row["offer"] = float(offer)
            row["qtime"] = float(quote_time if quote_time is not None else time.time())
            src = str(source or "websocket").lower()
            row["source_id"] = np.uint8(_SOURCE_IDS.get(src, 3))
            self._write += 1
            return True

    def drain_batch(self, max_items: int = 256) -> np.ndarray:
        with self._lock:
            pending = self._write - self._read
            if pending <= 0:
                return self._buf[:0]
            take = min(pending, max_items, len(self._buf))
            start = self._read % len(self._buf)
            self._read += take
            if start + take <= len(self._buf):
                return self._buf[start : start + take].copy()
            part_a = len(self._buf) - start
            out = np.empty(take, dtype=_STREAM_TICK_DTYPE)
            out[:part_a] = self._buf[start:]
            out[part_a:] = self._buf[: take - part_a]
            return out

    def depth_approx(self) -> int:
        with self._lock:
            return max(0, self._write - self._read)


def get_zero_copy_pipeline_snapshot() -> dict[str, Any]:
    try:
        hub = get_market_data_hub()
        ring = hub._zero_copy_ring
        return {
            "ok": True,
            "ring_capacity": int(len(ring._buf)),
            "queue_depth_approx": ring.depth_approx(),
            "epic_slots": len(ring._epic_index),
            "frames_ingested": int(hub._stream_frames_ingested),
            "frames_dropped": int(hub._stream_frames_dropped),
        }
    except Exception:
        return {"ok": False}


def write_binary_ohlc_cache(
    epic: str,
    *,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    spread: np.ndarray,
) -> bool:
    """Persist 288-bar window as contiguous float64 blocks for sub-100ms cold hydration."""
    from trading.ohlc_cache_paths import ohlc_cache_path

    path = ohlc_cache_path(epic).with_suffix(".bin")
    n = int(min(len(high), len(low), len(close), len(spread), 288))
    if n <= 0:
        return False
    try:
        payload = b"IGRINGv1" + int(n).to_bytes(4, "little")
        for arr in (high[:n], low[:n], close[:n], spread[:n]):
            payload += np.asarray(arr, dtype=np.float64).tobytes()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return True
    except OSError:
        return False


def load_binary_ohlc_cache(epic: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int]:
    """mmap-free read of binary OHLC blocks into caller-provided views."""
    from trading.ohlc_cache_paths import ohlc_cache_path

    path = ohlc_cache_path(epic).with_suffix(".bin")
    empty = (
        np.zeros(0, dtype=np.float64),
        np.zeros(0, dtype=np.float64),
        np.zeros(0, dtype=np.float64),
        np.zeros(0, dtype=np.float64),
        0,
    )
    if not path.is_file():
        return empty
    try:
        raw = path.read_bytes()
    except OSError:
        return empty
    if len(raw) < 12 or raw[:8] != b"IGRINGv1":
        return empty
    n = int.from_bytes(raw[8:12], "little")
    if n <= 0 or n > 288:
        return empty
    need = 12 + n * 4 * 8
    if len(raw) < need:
        return empty
    block = np.frombuffer(raw[12 : 12 + n * 32], dtype=np.float64).reshape(4, n)
    high = block[0].copy()
    low = block[1].copy()
    close = block[2].copy()
    spread = block[3].copy()
    return high, low, close, spread, n



@dataclass
class QuoteSnapshot:
    epic: str
    bid: float
    offer: float
    updated_at: float
    source: str = "ig"
    quote_time: float | None = None

    def refresh(
        self,
        *,
        bid: float,
        offer: float,
        updated_at: float,
        source: str,
        quote_time: float | None,
    ) -> None:
        """In-place tick update — avoids per-tick QuoteSnapshot allocation."""
        self.bid = bid
        self.offer = offer
        self.updated_at = updated_at
        self.source = source
        self.quote_time = quote_time

    def _reference_epoch(self) -> float:
        return float(self.quote_time if self.quote_time is not None else self.updated_at)

    def age_seconds(self) -> float:
        ref = self._reference_epoch()
        try:
            from simulation.replay_clock import is_replay_active, now

            if is_replay_active():
                return max(0.0, now() - ref)
        except Exception as exc:
            log_guarded_exception("hub_quote_age", exc, epic=self.epic)
        return max(0.0, time.time() - ref)

    def to_quote(self) -> Quote:
        ref = self._reference_epoch()
        return Quote(
            time=datetime.fromtimestamp(ref), bid=self.bid, offer=self.offer
        )


NIGHT_MATRIX_EPICS: tuple[str, ...] = (
    "CS.D.CFPGOLD.CFP.IP",
    "IX.D.DOW.IFM.IP",
    "IX.D.NIKKEI.IFM.IP",
    "CS.D.EURUSD.CFD.IP",
    "CS.D.CRUDE.CFD.IP",
    "IX.D.FTSE.IFM.IP",
    "IX.D.DAX.IFM.IP",
)

COCKPIT_CORE_EPICS: tuple[str, ...] = NIGHT_MATRIX_EPICS[:4]

# Last-resort cold-boot seeds only. These decay as markets move — the packet
# validator re-anchors after 3 consecutive jump rejects, so a stale seed can
# no longer permanently poison the out-of-order check (July 2026 incident:
# 2024-era seeds vs live prices ~30-80% higher rejected 100% of real quotes).
_HUB_SEED_DEFAULTS: dict[str, tuple[float, float]] = {
    "CS.D.CFPGOLD.CFP.IP": (4187.0, 4187.5),
    "IX.D.DOW.IFM.IP": (52899.0, 52901.0),
    "IX.D.NIKKEI.IFM.IP": (69740.0, 69748.0),
    "CS.D.EURUSD.CFD.IP": (1.1442, 1.1444),
}


def normalize_hub_quote_source(source: str) -> str:
    """Map internal hub publish sources to institutional telemetry labels."""
    raw = str(source or "").strip().lower()
    if raw in ("ig_execution", "execution"):
        return "ig_execution"
    if raw in ("yahoo", "yahoo_reference", "yahoo_poll", "yahoo_heartbeat"):
        return "yahoo"
    if raw in ("synthetic", "mock", "stream_b", "macro_synthetic", "stream_a"):
        return "synthetic"
    if raw in ("rest", "stream", "ig", "ig_rest", "rest_poll", "lightstreamer"):
        return "ig_rest"
    if raw in ("replay", "mock_replay"):
        return "synthetic"
    return "ig_rest" if raw else "synthetic"


def _sync_hub_quote_source_metric(epic: str, source: str, staleness_seconds: float) -> None:
    """Publish per-epic quote provenance into ``ig_agent_v30_live_state`` shared memory."""
    epic_key = str(epic or "").strip()
    if not epic_key:
        return
    label = normalize_hub_quote_source(source)
    staleness = max(0, int(round(float(staleness_seconds))))
    try:
        from system.identity.state_cache import get_live_state_cache

        cache = get_live_state_cache()
        cache.update_hub_quote_source(
            epic=epic_key,
            source=label,
            staleness_seconds=staleness,
        )
    except Exception as exc:
        log_guarded_exception("hub_quote_source_shm_sync", exc, epic=epic_key)


def _is_execution_only_ig_source(source: str) -> bool:
    s = str(source or "").strip().lower()
    return s in ("ig_execution", "execution")


def _is_ig_rest_quote_source(source: str) -> bool:
    return normalize_hub_quote_source(source) == "ig_rest"


def _should_block_ig_signal_publish(
    epic: str,
    existing: QuoteSnapshot | None,
    incoming_source: str,
) -> bool:
    """Block IG REST from seeding night-matrix signal quotes when Yahoo-primary is active."""
    if epic not in NIGHT_MATRIX_EPICS:
        return False
    if not _is_ig_rest_quote_source(incoming_source):
        return False
    if _is_execution_only_ig_source(incoming_source):
        return False
    try:
        from feeder.pricing_transport import reference_transport_is_yahoo

        if not reference_transport_is_yahoo():
            return False
    except Exception:
        return False
    if existing is not None and normalize_hub_quote_source(existing.source) != "ig_rest":
        return False
    return True


def _should_block_ig_overwrite_primary(
    existing: QuoteSnapshot | None,
    incoming_source: str,
) -> bool:
    """Yahoo-first: reject IG REST publishes that would clobber a fresh primary quote."""
    if existing is None or not _is_ig_rest_quote_source(incoming_source):
        return False
    if _is_execution_only_ig_source(incoming_source):
        return False
    try:
        from feeder.pricing_transport import reference_transport_is_yahoo

        if not reference_transport_is_yahoo():
            return False
    except Exception:
        return False
    if normalize_hub_quote_source(existing.source) == "ig_rest":
        return False
    return existing.age_seconds() <= 45.0


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
        self._spread_history: dict[str, deque[float]] = {}
        self._spread_history_max = 200
        self._stream_frame_queue: queue.SimpleQueue[
            tuple[str, float, float, str, float | None]
        ] = queue.SimpleQueue()
        self._zero_copy_ring = _ZeroCopyStreamRing()
        self._stream_consumer_stop = threading.Event()
        self._stream_consumer_thread: threading.Thread | None = None
        self._stream_frames_ingested = 0
        self._stream_frames_dropped = 0

    def attach_rest(self, rest_client: Any) -> None:
        try:
            from system.apex_runtime_mode import ApexRuntimeMode, get_apex_runtime_mode

            if get_apex_runtime_mode() is ApexRuntimeMode.HARDENED_TESTBED:
                from system.engine_log import log_engine

                log_engine(
                    "MarketDataHub: REST attach blocked — HARDENED_TESTBED uses "
                    "loopback replay only"
                )
                return
        except Exception as exc:
            log_guarded_exception("hub_rest_attach", exc)

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
        epic = str(getattr(snap, "epic", "") or "")
        for cb in listeners:
            guard_call("hub_quote_listener", cb, snap, epic=epic)

    def enter_maintenance(self, epic: str) -> None:
        """IG sends blank BID/OFFER during Japan 225 daily maintenance — pause REST/stale paths."""
        epic_key = str(epic)
        with self._lock:
            self._maintenance_epics.add(epic_key)
            first = epic_key not in self._maintenance_logged
            if first:
                self._maintenance_logged.add(epic_key)
        if first:
            log_engine("Japan 225 maintenance window — pausing until prices resume")

    def exit_maintenance(self, epic: str) -> None:
        epic_key = str(epic)
        with self._lock:
            self._maintenance_epics.discard(epic_key)
            self._maintenance_logged.discard(epic_key)

    def is_in_maintenance(self, epic: str) -> bool:
        with self._lock:
            return str(epic) in self._maintenance_epics

    def record_spread(self, epic: str, spread_pts: float) -> None:
        """Track rolling spreads for dynamic normal median (gate 5)."""
        if spread_pts <= 0:
            return
        key = str(epic)
        with self._lock:
            hist = self._spread_history.setdefault(
                key, deque(maxlen=self._spread_history_max)
            )
            hist.append(float(spread_pts))

    def normal_spread(self, epic: str, *, fallback: float) -> float:
        with self._lock:
            hist = self._spread_history.get(str(epic))
        if not hist or len(hist) < 5:
            return float(fallback)
        try:
            return float(statistics.median(hist))
        except statistics.StatisticsError:
            return float(fallback)

    def spread_percentile(self, epic: str, current_spread: float) -> float:
        """Fraction of rolling history >= current spread (lower = tighter liquidity)."""
        with self._lock:
            hist = list(self._spread_history.get(str(epic), []))
        if len(hist) < 5 or current_spread <= 0:
            return 0.5
        rank = sum(1 for s in hist if s >= current_spread)
        return rank / len(hist)

    def spread_stats(self, epic: str, *, fallback: float) -> dict[str, float]:
        current_snap = self.get_snapshot(epic)
        current = 0.0
        if current_snap and current_snap.bid > 0 and current_snap.offer > 0:
            current = max(0.0, current_snap.offer - current_snap.bid)
        normal = self.normal_spread(epic, fallback=fallback)
        return {
            "current": current,
            "normal": normal,
            "samples": float(len(self._spread_history.get(str(epic), []))),
        }

    def verify_liquidity_shield_delta(
        self, epic: str, primary_spread: float
    ) -> tuple[bool, float]:
        """
        Compares primary spread vs wholesale institutional ECN baseline.
        Returns (is_shield_safe, calculated_ratio).

        Passive read-only — no orders, no config mutation. Fail-safe: if ECN
        reference is disconnected or initializing, pass through (True, 1.0).
        """
        epic_key = str(epic or "")
        with _ECN_BASELINE_LOCK:
            cache_entry = _ECN_BASELINE_CACHE.get(epic_key)

        if cache_entry is not None and not bool(cache_entry.get("connected", True)):
            log_engine(
                f"liquidity_shield: ECN baseline unavailable epic={epic_key} "
                f"— pass (fail-safe)"
            )
            return True, 1.0

        ecn_baselines = {
            "CS.D.CFPGOLD.CFP.IP": 0.5,
            "IX.D.NASDAQ.IFM.IP": 1.0,
            "IX.D.DOW.IFM.IP": 2.0,
        }
        if epic_key not in ecn_baselines:
            return True, 1.0

        ecn_base = float(ecn_baselines[epic_key])

        if primary_spread <= 0.0:
            return True, 1.0

        spread_multiplier_ratio = primary_spread / ecn_base

        with _ECN_BASELINE_LOCK:
            _ECN_BASELINE_CACHE[epic_key] = {
                "ecn_base": ecn_base,
                "primary_spread": float(primary_spread),
                "ratio": spread_multiplier_ratio,
                "connected": True,
            }

        if spread_multiplier_ratio > _LIQUIDITY_SHIELD_MAX_RATIO:
            return False, spread_multiplier_ratio
        return True, spread_multiplier_ratio

    def enqueue_stream_frame(
        self,
        epic: str,
        bid: float,
        offer: float,
        *,
        source: str = "websocket",
        quote_time: float | None = None,
    ) -> bool:
        """
        Non-blocking websocket/stream tick enqueue — decouples 500ms poll loop.

        High-frequency producer threads push here; the stream consumer thread
        drains in parallel and publishes to listeners + microkernel ingest.
        """
        epic_key = str(epic or "").strip()
        if not epic_key or bid <= 0 or offer <= bid:
            return False
        try:
            self._zero_copy_ring.push(
                epic_key,
                float(bid),
                float(offer),
                source=str(source or "websocket"),
                quote_time=quote_time,
            )
            self._ensure_stream_consumer()
            return True
        except Exception:
            self._stream_frames_dropped += 1
            return False

    def _ensure_stream_consumer(self) -> None:
        if self._stream_consumer_thread is not None and self._stream_consumer_thread.is_alive():
            return
        self._stream_consumer_stop.clear()
        self._stream_consumer_thread = threading.Thread(
            target=self._stream_consumer_loop,
            name="HubStreamFrameConsumer",
            daemon=True,
        )
        self._stream_consumer_thread.start()

    def _stream_consumer_loop(self) -> None:
        """Drain zero-copy ring with 50ms batch coalescing — last epic tick wins per batch."""
        while not self._stream_consumer_stop.is_set():
            deadline = time.monotonic() + 0.05
            batch: dict[str, tuple[float, float, str, float | None]] = {}
            while time.monotonic() < deadline:
                view = self._zero_copy_ring.drain_batch(max_items=512)
                if view.size == 0:
                    time.sleep(0.002)
                    continue
                for row in view:
                    epic_key = self._zero_copy_ring.epic_for_id(int(row["epic_id"]))
                    if not epic_key:
                        continue
                    src_id = int(row["source_id"])
                    source = _SOURCE_ID_REV.get(src_id, "synthetic")
                    qtime = float(row["qtime"]) if float(row["qtime"]) > 0 else None
                    batch[epic_key] = (
                        float(row["bid"]),
                        float(row["offer"]),
                        source,
                        qtime,
                    )
            if not batch:
                time.sleep(0.02)
                continue
            for epic_key, item in batch.items():
                bid, offer, source, quote_time = item
                try:
                    spread_pts = max(0.0, float(offer) - float(bid))
                    self.record_spread(epic_key, spread_pts)
                    try:
                        from runtime.portfolio_exploration_engine import record_spread_fuse_sample

                        record_spread_fuse_sample(epic_key, spread_pts)
                    except Exception:
                        pass
                    self.publish(
                        epic_key,
                        bid,
                        offer,
                        source=source,
                        quote_time=quote_time,
                    )
                    try:
                        from apex.microkernel import get_microkernel

                        get_microkernel().on_tick_ingest(
                            epic_key,
                            {"bid": bid, "offer": offer, "source": source},
                        )
                    except Exception as exc:
                        log_guarded_exception("hub_stream_microkernel", exc, epic=epic_key)
                    self._stream_frames_ingested += 1
                except Exception as exc:
                    log_guarded_exception("hub_stream_publish", exc, epic=epic_key)
                    self._stream_frames_dropped += 1

    def start_stream_frame_consumer(self) -> None:
        """Start background drain thread for websocket frame queue."""
        self._ensure_stream_consumer()

    def stop_stream_frame_consumer(self) -> None:
        self._stream_consumer_stop.set()
        if self._stream_consumer_thread is not None:
            self._stream_consumer_thread.join(timeout=1.0)
            self._stream_consumer_thread = None

    def stream_frame_metrics(self) -> dict[str, Any]:
        return {
            "queue_depth_approx": int(self._zero_copy_ring.depth_approx()),
            "frames_ingested": int(self._stream_frames_ingested),
            "frames_dropped": int(self._stream_frames_dropped),
            "consumer_alive": bool(
                self._stream_consumer_thread is not None
                and self._stream_consumer_thread.is_alive()
            ),
        }

    def publish(
        self,
        epic: str,
        bid: float,
        offer: float,
        *,
        source: str = "stream",
        quote_time: float | None = None,
    ) -> QuoteSnapshot | None:
        from system.market_integrity import should_publish_live_quote

        epic_key = epic.strip() if epic else ""
        if not epic_key:
            return None
        if bid > 0.0 and offer > 0.0:
            try:
                _record_latency_stage(epic=epic_key, stage="feed_hub")
            except Exception:
                pass
            vcode = validate_quote_packet_fast(epic=epic_key, bid=bid, offer=offer)
            if vcode != REASON_OK:
                reject_packet_code(vcode)
                return None
        if not should_publish_live_quote(epic_key, source=source):
            existing = self.get_snapshot(epic_key)
            if existing is not None:
                return existing
            return None
        epoch = float(quote_time if quote_time is not None else time.time())
        if bid > 0.0 and offer > 0.0:
            self.exit_maintenance(epic_key)
            self.record_spread(epic_key, offer - bid)
            src_norm = normalize_hub_quote_source(source)
            if src_norm in ("ig_rest",) and source not in ("websocket", "stream", "lightstreamer"):
                pass
            else:
                try:
                    from system.calendar_gate import news_proximity_features

                    news_proximity_features(epic_key, use_cache=True)
                except Exception:
                    pass
        with self._lock:
            existing = self._quotes.get(epic_key)
            if _should_block_ig_signal_publish(epic_key, existing, source):
                return existing
            if _should_block_ig_overwrite_primary(existing, source):
                return existing
            if existing is not None and existing.epic == epic_key:
                existing.refresh(
                    bid=bid,
                    offer=offer,
                    updated_at=epoch,
                    source=source,
                    quote_time=quote_time,
                )
                snap = existing
            else:
                snap = QuoteSnapshot(
                    epic=epic_key,
                    bid=bid,
                    offer=offer,
                    updated_at=epoch,
                    quote_time=quote_time,
                    source=source,
                )
                self._quotes[epic_key] = snap
            rest = self._rest
        if rest is not None and hasattr(rest, "touch_stream_activity"):
            rest.touch_stream_activity()
        if bid > 0 and offer > 0:
            try:
                from system.stream_ready import is_stream_ready, signal_stream_ready

                if not is_stream_ready():
                    signal_stream_ready(source=f"hub_publish:{epic}")
            except Exception as exc:
                log_guarded_exception("hub_stream_ready", exc, epic=epic)
        self._emit_quote(snap)
        if epic_key in NIGHT_MATRIX_EPICS:
            _sync_hub_quote_source_metric(epic_key, source, snap.age_seconds())
        return snap

    def publish_replay_tick(
        self,
        epic: str,
        bid: float,
        offer: float,
        *,
        quote_time: float,
        source: str = "replay",
    ) -> QuoteSnapshot | None:
        """Ingest deterministic replay packet — virtual clock synced to *quote_time*."""
        from simulation.replay_clock import set_replay_time

        epoch = float(quote_time)
        set_replay_time(epoch)
        epic_key = str(epic or "").strip()
        try:
            from simulation.replay_telemetry import record_tick

            record_tick(epic=epic_key)
        except Exception as exc:
            log_guarded_exception("hub_replay_telemetry", exc, epic=epic_key)
        return self.publish(
            epic,
            bid,
            offer,
            source=source,
            quote_time=epoch,
        )

    def get_snapshot(self, epic: str) -> QuoteSnapshot | None:
        with self._lock:
            return self._quotes.get(epic)

    def get_quote(self, epic: str) -> QuoteSnapshot | None:
        """Alias for get_snapshot — used by exploration/sweep hot paths."""
        return self.get_snapshot(epic)

    def list_epics(self) -> list[str]:
        with self._lock:
            return list(self._quotes.keys())

    def invalidate(self, epic: str) -> None:
        """Drop cached quote timestamps for an epic (session transition reset)."""
        epic_key = str(epic or "").strip()
        with self._lock:
            self._quotes.pop(epic_key, None)
            self._last_fetch_ts.pop(epic_key, None)
            self._spread_history.pop(epic_key, None)

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
        propagate_transient_errors: bool = False,
    ) -> QuoteSnapshot | None:
        """
        Return cached quote if fresh enough; otherwise fetch from IG REST.
        min_interval: minimum seconds between API calls for this epic.
        max_age: if set, return cache without fetch when younger than this.
        """
        try:
            from intelligence.telemetry_daemon import gasket_fetch_if_stale

            gasket_snap = gasket_fetch_if_stale(epic, max_age=max_age)
            if gasket_snap is not None:
                return gasket_snap
        except Exception as exc:
            log_guarded_exception("telemetry_gasket_fetch", exc, epic=epic)

        if self.is_in_maintenance(epic):
            cached = self.get_snapshot(epic)
            rescue_age = 90.0
            if cached and cached.bid > 0 and cached.age_seconds() <= rescue_age:
                return cached
            try:
                from system.market_watch.japan225_session import (
                    is_japan225_epic,
                    is_scheduled_daily_maintenance,
                )

                if is_japan225_epic(epic) and not is_scheduled_daily_maintenance(epic):
                    age_s = cached.age_seconds() if cached else None
                    log_engine(
                        f"Hub maintenance rescue: attempting REST fetch for {epic} "
                        f"(quote age={age_s}s)"
                    )
                else:
                    return cached
            except Exception:
                return cached

        interval = self._fetch_interval_sec if min_interval is None else min_interval
        with self._lock:
            rest = self._rest
            cached = self._quotes.get(epic)
            last_fetch = self._last_fetch_ts.get(epic, 0.0)

        try:
            from system.rest_api_budget import hub_quote_stream_fresh

            if hub_quote_stream_fresh(epic=epic) and cached and cached.bid > 0:
                return cached
        except Exception as exc:
            log_guarded_exception("hub_quote_stream_fresh", exc, epic=epic)
            age = cached.age_seconds()
            if max_age is not None and age <= max_age:
                return cached
            if time.time() - last_fetch < interval:
                return cached

        if rest is None:
            return cached

        try:
            from feeder.pricing_transport import reference_transport_is_yahoo

            if reference_transport_is_yahoo():
                return cached
        except Exception as exc:
            log_guarded_exception("hub_fetch_yahoo_only", exc, epic=epic)

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
        except RateLimitError:
            if propagate_transient_errors:
                raise
            log_engine("MarketDataHub fetch rate limited — using cache")
            return cached
        except IGAPIError as e:
            if propagate_transient_errors and getattr(e, "status_code", None) == 429:
                raise
            log_engine(f"MarketDataHub fetch failed: {type(e).__name__}: {e}")
            return cached
        except Exception as e:
            if propagate_transient_errors:
                from ig_api.rest_poll_backoff import is_connection_timeout

                if is_connection_timeout(e):
                    raise
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
                "stream_frames": self.stream_frame_metrics(),
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


def night_matrix_fresh_count(
    *,
    max_age_sec: float = 5.0,
    epics: tuple[str, ...] | list[str] | None = None,
) -> tuple[int, int]:
    """Return (fresh_count, total) for hub quotes younger than max_age_sec."""
    universe = tuple(epics or COCKPIT_CORE_EPICS)
    hub = get_market_data_hub()
    fresh = sum(1 for epic in universe if hub.is_fresh(epic, max_age=max_age_sec))
    return fresh, len(universe)


def night_matrix_signal_fresh_count(
    *,
    max_age_sec: float = 45.0,
    epics: tuple[str, ...] | list[str] | None = None,
) -> tuple[int, int]:
    """Hub quote OR dual-core ingest pulse within max_age counts as signal-fresh."""
    import time

    universe = tuple(epics or NIGHT_MATRIX_EPICS)
    hub = get_market_data_hub()
    now = time.time()
    pulse_at: dict[str, float] = {}
    try:
        from runtime.dual_core_execution import get_socket_heartbeat_state

        pulse_at = (get_socket_heartbeat_state() or {}).get("last_fresh_tick_at") or {}
    except Exception:
        pulse_at = {}
    fresh = 0
    for epic in universe:
        if hub.is_fresh(epic, max_age=max_age_sec):
            fresh += 1
            continue
        ts = pulse_at.get(epic)
        if ts and (now - float(ts)) <= max_age_sec:
            fresh += 1
    return fresh, len(universe)


def flush_hub_streaming_session_cache() -> dict[str, Any]:
    """Drain pending stream frame queue and reset frame drop counters."""
    hub = get_market_data_hub()
    drained = 0
    while True:
        try:
            hub._stream_frame_queue.get_nowait()
            drained += 1
        except Exception:
            break
    hub._stream_frames_dropped = 0
    return {
        "drained": drained,
        "frames_dropped_reset": True,
        "queue_depth_approx": int(hub._stream_frame_queue.qsize()),
    }


def execute_hub_seed_flush_for_night_matrix(
    *,
    epics: tuple[str, ...] | list[str] | None = None,
    source: str = "hub_seed",
) -> dict[str, Any]:
    """Publish seed ticks for stale cockpit epics and reset regime rings."""
    from runtime.regime_switch_engine import reset_epic_regime_ring_with_hub_seed

    universe = tuple(epics or COCKPIT_CORE_EPICS)
    hub = get_market_data_hub()
    seeded: list[str] = []
    ring_resets: list[dict[str, Any]] = []
    for epic in universe:
        stale = not hub.is_fresh(epic, max_age=5.0)
        if stale:
            defaults = _HUB_SEED_DEFAULTS.get(epic)
            if defaults:
                bid, offer = defaults
                hub.publish(epic, bid, offer, source=source)
                hub.enqueue_stream_frame(epic, bid, offer, source=source)
                seeded.append(epic)
        ring_resets.append(reset_epic_regime_ring_with_hub_seed(epic))
    return {"seeded": seeded, "seeded_count": len(seeded), "ring_resets": ring_resets}


_SYNTHETIC_HYDRATION_ACTIVE = False
_SYNTHETIC_INJECTION_META: dict[str, Any] = {}
_FALLBACK_TRANSPORT_TIER = ""


def synthetic_hydration_active() -> bool:
    return bool(_SYNTHETIC_HYDRATION_ACTIVE)


def get_fallback_transport_tier() -> str:
    return str(_FALLBACK_TRANSPORT_TIER or "")


def get_synthetic_injection_meta() -> dict[str, Any]:
    return dict(_SYNTHETIC_INJECTION_META)


def set_fallback_transport_tier(tier: str) -> None:
    global _FALLBACK_TRANSPORT_TIER
    _FALLBACK_TRANSPORT_TIER = str(tier or "").strip()


def reset_synthetic_hydration_for_tests() -> None:
    global _SYNTHETIC_HYDRATION_ACTIVE, _SYNTHETIC_INJECTION_META, _FALLBACK_TRANSPORT_TIER
    _SYNTHETIC_HYDRATION_ACTIVE = False
    _SYNTHETIC_INJECTION_META = {}
    _FALLBACK_TRANSPORT_TIER = ""


def run_synthetic_tick_injector(
    *,
    epics: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    """
    NumPy synthetic tick injector — maintains 288-bar ring continuity when live feeds are sparse.
    """
    global _SYNTHETIC_HYDRATION_ACTIVE, _SYNTHETIC_INJECTION_META
    from runtime.regime_switch_engine import inject_synthetic_ring_continuity

    universe = tuple(epics or COCKPIT_CORE_EPICS)
    hub = get_market_data_hub()
    ring_results: list[dict[str, Any]] = []
    published: list[str] = []
    for epic in universe:
        meta = inject_synthetic_ring_continuity(epic, micro_ticks=48)
        ring_results.append(meta)
        if not meta.get("ok"):
            continue
        vol = float(meta.get("vol_envelope") or 0.0001)
        snap = hub.get_snapshot(epic)
        if snap is not None and float(snap.bid) > 0:
            mid = (float(snap.bid) + float(snap.offer)) * 0.5
        else:
            defaults = _HUB_SEED_DEFAULTS.get(epic)
            mid = (defaults[0] + defaults[1]) * 0.5 if defaults else 100.0
        half = max(vol, 0.0001) * 0.5
        bid, offer = mid - half, mid + half
        hub.publish(epic, bid, offer, source="synthetic_hydration")
        hub.enqueue_stream_frame(epic, bid, offer, source="synthetic_hydration")
        published.append(epic)

    try:
        from trading.probability_engine import enable_synthetic_alpha_gate

        enable_synthetic_alpha_gate(True)
    except Exception:
        pass

    _SYNTHETIC_HYDRATION_ACTIVE = True
    _SYNTHETIC_INJECTION_META = {
        "active": True,
        "epics": list(universe),
        "published": published,
        "ring_results": ring_results,
        "ts": time.time(),
    }
    log_engine(
        f"MarketDataHub: synthetic tick injector armed epics={len(published)}/{len(universe)}"
    )
    return dict(_SYNTHETIC_INJECTION_META)


def _feed_indicator_state(
    *,
    connected: bool,
    last_frame_age_sec: float | None,
    frame_timeout: float = 30.0,
    auth_error: bool = False,
) -> str:
    """Map heartbeat telemetry to cockpit ingest grid states."""
    if auth_error:
        return "broken"
    if connected and last_frame_age_sec is not None and last_frame_age_sec <= frame_timeout:
        return "active"
    if connected or (last_frame_age_sec is not None and last_frame_age_sec <= frame_timeout * 2):
        return "warming"
    return "broken"


# --- Predictive headline sentiment urgency (slots 105–111) ---
_HEADLINE_URGENCY_LOCK = threading.Lock()
_HEADLINE_VECTORS: dict[str, dict[str, Any]] = {}
_HEADLINE_RECENT: deque[dict[str, Any]] = deque(maxlen=128)
_HEADLINE_KEYWORDS: tuple[tuple[str, float, str], ...] = (
    ("rate cut", 0.92, "monetary_easing"),
    ("rate hike", -0.88, "monetary_tightening"),
    ("tariff", -0.78, "trade_shock"),
    ("tariffs", -0.78, "trade_shock"),
    ("earnings beat", 0.85, "equity_positive"),
    ("beats estimates", 0.82, "equity_positive"),
    ("inflation spike", -0.80, "inflation_shock"),
    ("cpi surge", -0.76, "inflation_shock"),
    ("recession", -0.90, "macro_risk"),
    ("stimulus", 0.74, "fiscal_easing"),
    ("default", -0.86, "credit_stress"),
    ("hawkish", -0.70, "monetary_tightening"),
    ("dovish", 0.72, "monetary_easing"),
)


def parse_live_headline_sentiment_urgency(
    headline: str,
    *,
    epic: str = "",
) -> dict[str, Any]:
    """
    Thread-safe headline urgency parse — target <50ms for ML slots 105–111.

    Returns acceleration vector + momentum_breakout hint for front-run routing.
    """
    t0 = time.perf_counter()
    text = str(headline or "").strip().lower()
    key = str(epic or "GLOBAL").strip() or "GLOBAL"
    if not text:
        return {
            "ok": False,
            "epic": key,
            "urgency": 0.0,
            "acceleration": 0.0,
            "matched": [],
            "slots": [0.0] * 7,
            "momentum_breakout_hint": False,
            "parse_us": 0.0,
        }

    score = 0.0
    matched: list[str] = []
    for phrase, impact, tag in _HEADLINE_KEYWORDS:
        if phrase in text:
            score += impact
            matched.append(tag)
    score = float(max(-1.0, min(1.0, score)))
    urgency = float(min(1.0, abs(score)))
    acceleration = float(score * (1.0 + 0.35 * len(matched)))

    with _HEADLINE_URGENCY_LOCK:
        prior = _HEADLINE_VECTORS.get(key) or {}
        prev_accel = float(prior.get("acceleration") or 0.0)
        dt = max(0.05, time.time() - float(prior.get("ts") or time.time()))
        jerk = (acceleration - prev_accel) / dt
        velocity = float(0.55 * acceleration + 0.45 * prev_accel)
        news_velocity = float(min(1.0, abs(velocity) * (1.0 + 0.2 * len(matched))))
        block = float(1.0 if any(t in matched for t in ("monetary_tightening", "inflation_shock")) else 0.0)
        slots = [
            float(min(1.0, urgency)),
            news_velocity,
            block,
            float(np.tanh(jerk * 0.15)),
            float(max(0.0, score)),
            float(max(0.0, -score)),
            float(min(1.0, abs(jerk) * 0.02)),
        ]
        body = {
            "ok": True,
            "epic": key,
            "headline": text[:240],
            "urgency": round(urgency, 4),
            "acceleration": round(acceleration, 4),
            "velocity": round(velocity, 4),
            "jerk": round(jerk, 4),
            "matched": matched,
            "slots": [round(s, 4) for s in slots],
            "momentum_breakout_hint": acceleration >= 0.55 or acceleration <= -0.55,
            "ts": time.time(),
            "parse_us": round((time.perf_counter() - t0) * 1e6, 1),
        }
        _HEADLINE_VECTORS[key] = body
        _HEADLINE_RECENT.appendleft(dict(body))
    return body


def ingest_live_headline(
    headline: str,
    *,
    epic: str = "",
    source: str = "multi_feed_hub",
) -> dict[str, Any]:
    """Async ingestion entry — headline text into urgency vector cache."""
    result = parse_live_headline_sentiment_urgency(headline, epic=epic)
    result["source"] = str(source or "unknown")
    if result.get("momentum_breakout_hint"):
        try:
            from system.chaos_guardian import enqueue_fast_pass_token

            enqueue_fast_pass_token(
                epic=str(epic or "CS.D.EURUSD.CFD.IP"),
                direction="BUY" if float(result.get("acceleration") or 0) > 0 else "SELL",
                score=float(result.get("urgency") or 0.65),
                reason="headline_momentum_breakout",
            )
        except Exception:
            pass
    return result


def get_news_velocity_feature_slots(epic: str = "") -> list[float]:
    """Feature slots 105–111 for ML matrix injection."""
    key = str(epic or "GLOBAL").strip() or "GLOBAL"
    with _HEADLINE_URGENCY_LOCK:
        row = _HEADLINE_VECTORS.get(key) or _HEADLINE_VECTORS.get("GLOBAL")
        if not row:
            return []
        slots = row.get("slots") or []
        return [float(s) for s in slots[:7]]


def get_headline_urgency_snapshot() -> dict[str, Any]:
    with _HEADLINE_URGENCY_LOCK:
        return {
            "ok": True,
            "epics": {k: dict(v) for k, v in list(_HEADLINE_VECTORS.items())[:16]},
            "recent": list(_HEADLINE_RECENT)[:12],
        }


def reset_headline_urgency_for_tests() -> None:
    with _HEADLINE_URGENCY_LOCK:
        _HEADLINE_VECTORS.clear()
        _HEADLINE_RECENT.clear()


def get_external_api_health_matrix() -> dict[str, Any]:
    """
    Outbound ingestion heartbeats — Yahoo, Finnhub, calendars, sentiment, broker surface.
    """
    now = time.time()
    feeds: list[dict[str, Any]] = []

    with _HEADLINE_URGENCY_LOCK:
        headline_live = len(_HEADLINE_RECENT) > 0
    feeds.append(
        {
            "id": "headline_sentiment",
            "label": "Headline Sentiment Parser",
            "state": "active" if headline_live else "warming",
            "connected": headline_live,
        }
    )

    try:
        from feeder.yahoo_quote_poller import yahoo_poller_active, yahoo_rate_limited

        yahoo_active = bool(yahoo_poller_active())
        yahoo_state = "broken" if yahoo_rate_limited() else (
            "active" if yahoo_active else "warming"
        )
        feeds.append(
            {
                "id": "yahoo_poller",
                "label": "Yahoo Ingest",
                "state": yahoo_state,
                "connected": yahoo_active,
            }
        )
    except Exception as exc:
        feeds.append(
            {
                "id": "yahoo_poller",
                "label": "Yahoo Finance Poller",
                "state": "broken",
                "detail": type(exc).__name__,
            }
        )

    try:
        from system.feeds.multi_feed_hub import feed_hub_telemetry

        hub_tel = feed_hub_telemetry()
        for key, row in (hub_tel.get("providers") or {}).items():
            if not isinstance(row, dict):
                continue
            last_mono = float(row.get("last_frame_mono") or 0.0)
            age = (time.monotonic() - last_mono) if last_mono > 0 else None
            feeds.append(
                {
                    "id": f"multi_feed_{key}",
                    "label": (
                        "Finnhub WS"
                        if "finnhub" in str(key).lower()
                        else "IG Stream"
                        if "ig" in str(key).lower()
                        else str(row.get("label") or key)
                    ),
                    "state": _feed_indicator_state(
                        connected=bool(row.get("connected")),
                        last_frame_age_sec=age,
                    ),
                    "connected": bool(row.get("alive")),
                    "wins": int(row.get("wins") or 0),
                }
            )
    except Exception:
        pass

    try:
        from system.calendar_gate import news_proximity_features

        probe = news_proximity_features("CS.D.EURUSD.CFD.IP")
        has_calendar = bool(probe) and (
            float(probe.get("seconds_to_next") or 0) > 0
            or float(probe.get("countdown_norm") or 0) >= 0
        )
        feeds.append(
            {
                "id": "economic_calendar",
                "label": "Global Economic Calendar",
                "state": "active" if has_calendar else "warming",
                "connected": has_calendar,
            }
        )
    except Exception as exc:
        feeds.append(
            {
                "id": "economic_calendar",
                "label": "Global Economic Calendar",
                "state": "broken",
                "detail": type(exc).__name__,
            }
        )

    try:
        from trading.sentiment_momentum import sentiment_momentum_features

        sent = sentiment_momentum_features("CS.D.EURUSD.CFD.IP")
        live = any(
            abs(float(sent.get(k) or 0)) > 0.001
            for k in ("delta_5m", "delta_30m", "long_pct")
        )
        feeds.append(
            {
                "id": "broker_sentiment",
                "label": "Sentiment Surface",
                "state": "active" if live else "warming",
                "connected": live,
            }
        )
    except Exception as exc:
        feeds.append(
            {
                "id": "broker_sentiment",
                "label": "Broker Sentiment Surface",
                "state": "broken",
                "detail": type(exc).__name__,
            }
        )

    try:
        from signals.feature_state import compile_current_feature_state

        compiled = compile_current_feature_state(epic="CS.D.EURUSD.CFD.IP")
        vec = compiled.get("vector")
        slots_live = False
        if vec is not None and len(vec) >= 112:
            import numpy as np

            tail = np.asarray(vec[98:112], dtype=np.float64)
            slots_live = bool(np.any(np.abs(tail) > 1e-6))
        feeds.append(
            {
                "id": "ml_feature_slots_98_111",
                "label": "ML Sentiment/News Vectors (98–111)",
                "state": "active" if slots_live else "warming",
                "connected": slots_live,
            }
        )
    except Exception:
        feeds.append(
            {
                "id": "ml_feature_slots_98_111",
                "label": "ML Sentiment/News Vectors (98–111)",
                "state": "warming",
                "connected": False,
            }
        )

    active_n = sum(1 for f in feeds if f.get("state") == "active")
    broken_n = sum(1 for f in feeds if f.get("state") == "broken")
    return {
        "ok": broken_n == 0,
        "feeds": feeds,
        "active_count": active_n,
        "broken_count": broken_n,
        "warming_count": max(0, len(feeds) - active_n - broken_n),
        "ts": now,
    }
