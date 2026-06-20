"""
v30 Apex — async event-driven micro-kernel (Workers A–D).

Decoupled queues isolate ingestion, NumPy math, risk guard, and async ledger I/O
from network threads. Target hot-path budget: <250µs math, <15µs queue handoff.

Worker B reads tick frames from Worker A via ``SimpleQueue`` (non-blocking put),
computes float64 indicator matrices, and passes zero-copy ndarray evidence to
Worker C through shared per-epic ring buffers.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from system.engine_log import log_engine

from apex.hardening import (
    PER_ASSET_RISK_CAP_GBP as _PER_ASSET_CAP_GBP,
    PORTFOLIO_RISK_CEILING_GBP as _GLOBAL_CEILING_GBP,
    floor_contract_size,
    is_execution_frozen,
)
_RING_CAPACITY = 256
_FLOAT64 = np.float64

# Pillar 2 & 5 — bounded warmup defer heap (US-open flood protection).
# TickFrame ≈192 B → 5120 frames ≈ 0.98 MiB hard ceiling.
DEFERRED_QUEUE_MAX_FRAMES = 5120
DEFERRED_FRAME_EST_BYTES = 192
DEFERRED_HEAP_BUDGET_BYTES = DEFERRED_QUEUE_MAX_FRAMES * DEFERRED_FRAME_EST_BYTES
DEFERRED_FLUSH_SLA_MS = 2.0
DEFERRED_FLUSH_CHUNK = 500  # max ring writes per 1 ms wall-clock slice

# Ironclad mutex — historical seed thread vs Worker A live ingest (ring buffers).
_RING_WARMUP_MUTEX = threading.Lock()


def is_warmup_gate_active() -> bool:
    """True while BootPhase.WARMING / array compile is in flight."""
    try:
        from apex.warmup_progress import is_warmup_active

        if is_warmup_active():
            return True
    except Exception:
        pass
    try:
        from system.system_state import BootPhase, get_system_state

        return get_system_state().snapshot_model().phase == BootPhase.WARMING
    except Exception:
        return False


def is_warmup_complete() -> bool:
    """True when ring compile finished or warmup was never required (idle/ready)."""
    try:
        from apex.warmup_progress import get_warmup_snapshot, is_warmup_active

        if is_warmup_active():
            return False
        status = str(get_warmup_snapshot().get("status") or "idle")
        if status == "failed":
            return False
        return True
    except Exception:
        return True


def warmup_execution_blocked() -> bool:
    """Project Apex Monolith Core Circuit Breaker — block orders during compile."""
    if not is_warmup_complete():
        return True
    return is_warmup_gate_active()


def ring_warmup_mutex() -> threading.Lock:
    return _RING_WARMUP_MUTEX


def deferred_queue_footprint_bytes(frame_count: int) -> int:
    """Estimated heap for deferred TickFrame queue (audit / QA)."""
    return max(0, int(frame_count)) * DEFERRED_FRAME_EST_BYTES


@dataclass
class TickFrame:
    epic: str
    bid: float
    offer: float
    arrival_mono: float
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class MathEvidence:
    """Zero-copy evidence bundle — ndarray views shared into Worker C."""

    epic: str
    arrival_mono: float
    close: np.ndarray
    indicator_matrix: np.ndarray
    validation_mask: np.ndarray
    ml_pass: bool
    ml_veto_floor: float
    latency_us: float


@dataclass
class MathFrame:
    epic: str
    arrival_mono: float
    rsi: float
    ema_fast: float
    ema_slow: float
    atr: float
    atr_upper: float
    atr_lower: float
    ml_pass: bool
    latency_us: float
    evidence: MathEvidence | None = None


@dataclass
class RiskVerdict:
    epic: str
    allowed: bool
    detail: str
    size_int: int
    risk_gbp: float


class _EpicRingBuffer:
    """Fixed-capacity float64 OHLC ring — shared memory between Workers A and B."""

    __slots__ = ("close", "high", "low", "_head", "_count", "_lock")

    def __init__(self) -> None:
        self.close = np.zeros(_RING_CAPACITY, dtype=_FLOAT64)
        self.high = np.zeros(_RING_CAPACITY, dtype=_FLOAT64)
        self.low = np.zeros(_RING_CAPACITY, dtype=_FLOAT64)
        self._head = 0
        self._count = 0
        self._lock = threading.Lock()

    def append(self, mid: float, high: float, low: float) -> None:
        with self._lock:
            i = self._head
            self.close[i] = mid
            self.high[i] = high
            self.low[i] = low
            self._head = (i + 1) % _RING_CAPACITY
            self._count = min(self._count + 1, _RING_CAPACITY)

    def ordered_views(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Return chronological float64 views for Worker B (minimal copy)."""
        with self._lock:
            n = self._count
            if n == 0:
                empty = np.array([], dtype=_FLOAT64)
                return empty, empty, empty
            if n < _RING_CAPACITY:
                return (
                    self.close[:n].copy(),
                    self.high[:n].copy(),
                    self.low[:n].copy(),
                )
            h = self._head
            order = (np.arange(h, h + _RING_CAPACITY, dtype=np.intp) % _RING_CAPACITY)
            return self.close[order], self.high[order], self.low[order]


class ApexMicroKernel:
    """Four-worker pipeline: ingest → math → risk → ledger."""

    def __init__(self) -> None:
        self._ingest_q: queue.SimpleQueue[TickFrame | None] = queue.SimpleQueue()
        self._math_q: queue.SimpleQueue[TickFrame | None] = queue.SimpleQueue()
        self._risk_q: queue.SimpleQueue[tuple[MathFrame, RiskVerdict] | None] = (
            queue.SimpleQueue()
        )
        self._close_q: queue.SimpleQueue[MathFrame | None] = queue.SimpleQueue()
        self._threads: list[threading.Thread] = []
        self._running = False
        self._lock = threading.RLock()
        self._rings: dict[str, _EpicRingBuffer] = {}
        self._indicator_bufs: dict[str, np.ndarray] = {}
        self._concurrent_risk_gbp = 0.0
        self._stats = {
            "ingested": 0,
            "ingested_deferred": 0,
            "deferred_evicted": 0,
            "deferred_flush_ms": 0.0,
            "math_done": 0,
            "math_dropped": 0,
            "risk_pass": 0,
            "ledger_rows": 0,
            "triage_dropped": 0,
            "math_latency_us_p50": 0.0,
        }
        self._latency_ring: list[float] = []
        self._ledger_snapshot_interval = 64
        self._micro_trend_cache: dict[str, dict[str, Any]] = {}
        self._deferred_frames: list[TickFrame] = []
        self._deferred_lock = threading.Lock()
        self._ingest_coalesce: dict[str, TickFrame] = {}

    def _ring_for(self, epic: str) -> _EpicRingBuffer:
        buf = self._rings.get(epic)
        if buf is None:
            buf = _EpicRingBuffer()
            self._rings[epic] = buf
        return buf

    def _indicator_buf_for(self, epic: str) -> np.ndarray:
        buf = self._indicator_bufs.get(epic)
        if buf is None:
            buf = np.zeros((_RING_CAPACITY, 4), dtype=_FLOAT64)
            self._indicator_bufs[epic] = buf
        return buf

    def _append_ring(self, epic: str, mid: float, high: float, low: float) -> None:
        with _RING_WARMUP_MUTEX:
            self._ring_for(epic).append(mid, high, low)

    def _defer_live_frame(self, frame: TickFrame) -> None:
        with self._deferred_lock:
            if len(self._deferred_frames) >= DEFERRED_QUEUE_MAX_FRAMES:
                self._deferred_frames.pop(0)
                self._stats["deferred_evicted"] = (
                    int(self._stats.get("deferred_evicted", 0)) + 1
                )
            self._deferred_frames.append(frame)
        self._stats["ingested_deferred"] = int(self._stats.get("ingested_deferred", 0)) + 1

    def deferred_queue_depth(self) -> int:
        with self._deferred_lock:
            return len(self._deferred_frames)

    def flush_deferred_live_ticks(self) -> int:
        """Flush buffered live ticks into rings after mark_warmup_ready().

        Swaps the defer list under lock (microseconds), then drains on the warmup
        thread so the API / Worker A path never blocks on ring writes.
        """
        t0 = time.perf_counter()
        with self._deferred_lock:
            pending = self._deferred_frames
            self._deferred_frames = []
        if not pending:
            return 0

        flushed = 0
        offset = 0
        total = len(pending)
        while offset < total:
            chunk_start = time.perf_counter()
            chunk = pending[offset : offset + DEFERRED_FLUSH_CHUNK]
            with _RING_WARMUP_MUTEX:
                for frame in chunk:
                    epic = frame.epic
                    ring = self._ring_for(epic)
                    mid = (frame.bid + frame.offer) * 0.5
                    ring.append(mid, frame.offer, frame.bid)
                    flushed += 1
            for frame in chunk:
                try:
                    self._math_q.put_nowait(frame)
                except queue.Full:
                    self._stats["math_dropped"] += 1
            offset += len(chunk)
            if offset < total:
                elapsed = time.perf_counter() - chunk_start
                if elapsed < 0.001:
                    time.sleep(0.001 - elapsed)

        self._stats["ingested"] += flushed

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self._stats["deferred_flush_ms"] = elapsed_ms
        if flushed:
            log_engine(
                f"Apex micro-kernel: flushed {flushed} deferred live tick(s) "
                f"post-warmup in {elapsed_ms:.3f}ms"
            )
            if elapsed_ms > DEFERRED_FLUSH_SLA_MS and flushed <= _RING_CAPACITY:
                log_engine(
                    f"Apex micro-kernel: deferred flush exceeded "
                    f"{DEFERRED_FLUSH_SLA_MS}ms SLA ({elapsed_ms:.3f}ms)"
                )
        return flushed

    def micro_trend_for(self, epic: str) -> dict[str, Any]:
        """Latest Worker B micro-trend alpha snapshot for gate promotion."""
        return dict(self._micro_trend_cache.get(str(epic or ""), {}))

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            try:
                from system.testbed_firewall import ensure_testbed_firewall_armed

                ensure_testbed_firewall_armed()
            except Exception as exc:
                log_engine(f"Apex micro-kernel: testbed firewall check skipped: {exc}")
            self._running = True
            try:
                from analytics.triage_logger import get_triage_logger

                get_triage_logger().start()
            except Exception as exc:
                log_engine(f"Apex micro-kernel: triage logger start skipped: {exc}")
            try:
                from trading.continuous_optimization_worker import (
                    get_continuous_optimization_worker,
                )

                get_continuous_optimization_worker().start()
            except Exception as exc:
                log_engine(
                    f"Apex micro-kernel: continuous optimization start skipped: {exc}"
                )
            try:
                from signals.regime_sentinel import start_macro_regime_sentinel

                start_macro_regime_sentinel()
            except Exception as exc:
                log_engine(
                    f"Apex micro-kernel: MacroRegimeSentinel start skipped: {exc}"
                )
            try:
                import os

                # Desktop / :9090 shadow sidecar is supervised by Electron — starting
                # WatchdogSelfHealer inside microkernel races uvicorn bind and deletes
                # apex_ipc.sock during boot (IPC suicide loop).
                port = int(os.environ.get("IG_API_PORT", "8080"))
                desktop = os.environ.get("IG_APEX_DESKTOP", "").strip() == "1"
                if port != 9090 and not desktop:
                    from system.watchdog_sentinel import start_watchdog_self_healer

                    start_watchdog_self_healer()
            except Exception as exc:
                log_engine(
                    f"Apex micro-kernel: WatchdogSelfHealer start skipped: {exc}"
                )
            specs = (
                ("apex-worker-ingest", self._worker_a_ingest),
                ("apex-worker-math", self._worker_b_math),
                ("apex-worker-risk", self._worker_c_risk),
                ("apex-worker-ledger", self._worker_d_ledger),
            )
            for name, target in specs:
                t = threading.Thread(target=target, name=name, daemon=True)
                t.start()
                self._threads.append(t)
            try:
                from trading.multi_api_broker import start_multi_api_broker

                start_multi_api_broker(self)
            except Exception as exc:
                log_engine(f"Apex micro-kernel: multi-api broker start skipped: {exc}")
            log_engine("Apex micro-kernel: workers A–D online")

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            for _ in range(4):
                try:
                    self._ingest_q.put_nowait(None)
                except queue.Full:
                    self._ingest_q.put(None)
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads.clear()
        try:
            from trading.multi_api_broker import stop_multi_api_broker

            stop_multi_api_broker()
        except Exception:
            pass

    def enqueue_ingest_frame(self, frame: TickFrame) -> None:
        """Non-blocking enqueue from MultiApiIngestionBroker (Worker A queue).

        Coalesces to the latest frame per epic — older pending ticks are dropped
        under backpressure (LIFO per instrument).
        """
        if not self._running:
            self.start()
        epic = str(getattr(frame, "epic", "") or "")
        with self._lock:
            self._ingest_coalesce[epic] = frame
        try:
            self._ingest_q.put_nowait(frame)
        except queue.Full:
            self._stats["math_dropped"] += 1

    def on_tick_ingest(self, epic: str, quote: Any) -> None:
        if not self._running:
            self.start()
        try:
            bid = float(getattr(quote, "bid", 0) or 0)
            offer = float(getattr(quote, "offer", 0) or 0)
        except (TypeError, ValueError):
            return
        raw: dict[str, Any] = {}
        if isinstance(quote, dict):
            raw = quote
        elif hasattr(quote, "__dict__"):
            raw = {k: v for k, v in vars(quote).items() if not k.startswith("_")}
        frame = TickFrame(
            epic=str(epic or ""),
            bid=bid,
            offer=offer,
            arrival_mono=time.monotonic(),
            raw=raw,
        )
        self.enqueue_ingest_frame(frame)

    def publish_risk_context(
        self,
        *,
        epic: str,
        size: float,
        stop_pts: float,
        spread_pts: float,
        point_value_gbp: float,
        concurrent_risk_gbp: float,
        ml_pass: bool = True,
    ) -> RiskVerdict:
        """Synchronous risk snapshot for gate integration (Worker C rules)."""
        size_int, under_min = floor_contract_size(size)
        min_lot = 1
        risk_gbp = (stop_pts + spread_pts) * max(size_int, 0) * point_value_gbp
        concurrent = float(concurrent_risk_gbp or self._concurrent_risk_gbp)
        allowed = (
            not is_execution_frozen()
            and ml_pass
            and not under_min
            and size_int >= min_lot
            and risk_gbp <= _PER_ASSET_CAP_GBP
            and (concurrent + risk_gbp) <= _GLOBAL_CEILING_GBP
        )
        detail = (
            f"OK size={size_int} risk=£{risk_gbp:.0f} concurrent=£{concurrent:.0f}"
            if allowed
            else f"block size={size_int} risk=£{risk_gbp:.0f} cap=£{_PER_ASSET_CAP_GBP:.0f}"
        )
        return RiskVerdict(
            epic=epic,
            allowed=allowed,
            detail=detail,
            size_int=size_int,
            risk_gbp=risk_gbp,
        )

    def _worker_a_ingest(self) -> None:
        while True:
            frame = self._ingest_q.get()
            if frame is None:
                try:
                    self._math_q.put_nowait(None)
                except queue.Full:
                    self._math_q.put(None)
                break
            with self._lock:
                batch = list(self._ingest_coalesce.values())
                self._ingest_coalesce.clear()
            if not batch:
                batch = [frame]
            for live in batch:
                if is_warmup_gate_active():
                    self._defer_live_frame(live)
                    continue
                mid = (live.bid + live.offer) * 0.5
                self._append_ring(live.epic, mid, live.offer, live.bid)
                self._stats["ingested"] += 1
                try:
                    self._math_q.put_nowait(live)
                except queue.Full:
                    self._stats["math_dropped"] += 1

    def _worker_b_math(self) -> None:
        from signals.indicators import (
            compute_math_matrix,
            evaluate_micro_trend_alpha,
            resolve_ml_veto_floor,
        )

        while True:
            frame = self._math_q.get()
            if frame is None:
                try:
                    self._risk_q.put_nowait(None)
                except queue.Full:
                    self._risk_q.put(None)
                break

            t0 = time.perf_counter()
            ring = self._ring_for(frame.epic)
            close, high, low = ring.ordered_views()
            if len(close) < 3:
                close = np.asarray(
                    [frame.bid, frame.offer, (frame.bid + frame.offer) * 0.5],
                    dtype=_FLOAT64,
                )
                high = np.asarray([frame.offer, frame.offer, frame.offer], dtype=_FLOAT64)
                low = np.asarray([frame.bid, frame.bid, frame.bid], dtype=_FLOAT64)

            ml_prob = None
            raw = frame.raw or {}
            for key in ("ml_probability", "p_win", "probability"):
                if raw.get(key) is not None:
                    try:
                        ml_prob = float(raw[key])
                        break
                    except (TypeError, ValueError):
                        pass

            ind_buf = self._indicator_buf_for(frame.epic)
            if is_execution_frozen():
                self._stats["math_dropped"] += 1
                continue

            snap = compute_math_matrix(
                close,
                high,
                low,
                ml_probability=ml_prob,
                ml_veto_floor=resolve_ml_veto_floor(epic=frame.epic),
                out_indicator_matrix=ind_buf,
            )
            latency_us = (time.perf_counter() - t0) * 1_000_000.0

            micro = evaluate_micro_trend_alpha(close)
            self._micro_trend_cache[frame.epic] = micro
            if micro.get("promote"):
                frame.raw = dict(frame.raw or {})
                frame.raw["micro_trend_score_pct"] = float(micro.get("score_pct") or 0.0)
                frame.raw["micro_trend_direction"] = str(micro.get("direction") or "")
                frame.raw["micro_trend_promote"] = True

            n = len(close)
            mat = snap["indicator_matrix"]
            evidence = MathEvidence(
                epic=frame.epic,
                arrival_mono=frame.arrival_mono,
                close=close,
                indicator_matrix=mat if n <= _RING_CAPACITY else mat[:n],
                validation_mask=snap["validation_mask"],
                ml_pass=bool(snap["ml_pass"]),
                ml_veto_floor=float(snap["ml_veto_floor"]),
                latency_us=latency_us,
            )

            mf = MathFrame(
                epic=frame.epic,
                arrival_mono=frame.arrival_mono,
                rsi=float(snap["rsi"][-1]) if n else 50.0,
                ema_fast=float(snap["fast_ema"]),
                ema_slow=float(snap["slow_ema"]),
                atr=float(snap["atr"]),
                atr_upper=float(snap["atr_upper"]),
                atr_lower=float(snap["atr_lower"]),
                ml_pass=evidence.ml_pass,
                latency_us=latency_us,
                evidence=evidence,
            )

            self._stats["math_done"] += 1
            self._latency_ring.append(latency_us)
            if len(self._latency_ring) > 128:
                self._latency_ring = self._latency_ring[-128:]
            self._stats["math_latency_us_p50"] = float(
                np.median(np.asarray(self._latency_ring, dtype=_FLOAT64))
            )

            try:
                self._risk_q.put_nowait(
                    (mf, RiskVerdict(frame.epic, True, "queued", 1, 0.0))
                )
            except queue.Full:
                self._stats["math_dropped"] += 1

    def _worker_c_risk(self) -> None:
        while True:
            item = self._risk_q.get()
            if item is None:
                try:
                    self._close_q.put_nowait(None)
                except queue.Full:
                    self._close_q.put(None)
                break
            mf, _ = item
            spread = max(0.0, mf.atr * 0.05)
            verdict = self.publish_risk_context(
                epic=mf.epic,
                size=1.0,
                stop_pts=max(1.0, mf.atr),
                spread_pts=spread,
                point_value_gbp=1.0,
                concurrent_risk_gbp=self._concurrent_risk_gbp,
                ml_pass=mf.ml_pass,
            )
            if verdict.allowed:
                self._stats["risk_pass"] += 1
            try:
                self._close_q.put_nowait(mf)
            except queue.Full:
                pass

    def dispatch_triage(self, payload: dict[str, Any]) -> bool:
        """Non-blocking triage dispatch for execution loops (Worker D queue)."""
        try:
            from analytics.triage_logger import dispatch_triage_event

            ok = dispatch_triage_event(payload)
            if not ok:
                self._stats["triage_dropped"] += 1
            return ok
        except Exception:
            self._stats["triage_dropped"] += 1
            return False

    def _worker_d_ledger(self) -> None:
        from analytics.triage_logger import get_triage_logger, log_tick_latency

        try:
            from signals.indicators import session_name
        except Exception:
            session_name = lambda: ""  # noqa: E731

        logger = get_triage_logger()
        log_engine(
            f"Apex Worker D: async ledger online → {logger.db_path} (WAL + BEGIN IMMEDIATE)"
        )
        while True:
            mf = self._close_q.get()
            if mf is None:
                break
            try:
                arrival_ts = time.time()
                spread_penalty = max(0.0, mf.atr * 0.05)
                log_tick_latency(
                    epic=mf.epic,
                    tick_arrival_ts=arrival_ts,
                    processing_latency_us=mf.latency_us,
                    spread_penalty_pts=spread_penalty,
                    session_window=session_name(),
                )
                self._stats["ledger_rows"] += 1
                if self._stats["ledger_rows"] % self._ledger_snapshot_interval == 0:
                    logger.log_session_snapshot()
            except Exception:
                pass

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._stats)

    def seed_historical_bars_from_engine(
        self,
        epic: str,
        signal_engine: Any,
        market: str,
        *,
        max_bars: int = _RING_CAPACITY,
        on_bar: Any | None = None,
    ) -> int:
        """
        Back-fill the per-epic float64 ring from SignalEngine OHLC history.

        Invoked by the detached array-warmup thread — not on the API bind path.
        """
        try:
            df = signal_engine.quote_df(market)
        except Exception:
            return 0
        if df is None or getattr(df, "empty", True):
            return 0

        try:
            tail = df.tail(max(1, int(max_bars)))
        except Exception:
            return 0

        ring = self._ring_for(epic)
        seeded = 0
        with _RING_WARMUP_MUTEX:
            for row in tail.itertuples(index=False):
                try:
                    bid = float(getattr(row, "bid", 0) or 0)
                    offer = float(getattr(row, "offer", 0) or 0)
                except (TypeError, ValueError):
                    continue
                mid = (bid + offer) * 0.5
                ring.append(mid, offer, bid)
                seeded += 1
                if on_bar is not None:
                    try:
                        on_bar(seeded)
                    except Exception:
                        pass
        return seeded


_KERNEL: ApexMicroKernel | None = None
_KERNEL_LOCK = threading.Lock()


def get_microkernel() -> ApexMicroKernel:
    global _KERNEL
    with _KERNEL_LOCK:
        if _KERNEL is None:
            _KERNEL = ApexMicroKernel()
        return _KERNEL


def start_microkernel(*, workers_only: bool = False) -> ApexMicroKernel:
    """
    Start Workers A–D.

    When ``workers_only`` is True, historical ring compilation is deferred to
    ``apex.array_warmup.schedule_background_array_warmup``.
    """
    kernel = get_microkernel()
    kernel.start()
    return kernel


def schedule_array_warmup(
    rest_client: Any,
    loops: list[Any],
    cfg: Any | None = None,
    *,
    on_complete: Any | None = None,
) -> None:
    """Detached OHLC + 256-bar ring compilation (non-blocking API bind)."""
    from apex.array_warmup import schedule_background_array_warmup

    schedule_background_array_warmup(
        rest_client,
        loops,
        cfg,
        on_complete=on_complete,
    )


def reset_microkernel_for_tests() -> None:
    global _KERNEL
    with _KERNEL_LOCK:
        if _KERNEL is not None:
            _KERNEL.stop()
        _KERNEL = None


# Re-export circuit breaker for trading / execution modules.
__all__ = [
    "ApexMicroKernel",
    "DEFERRED_QUEUE_MAX_FRAMES",
    "DEFERRED_HEAP_BUDGET_BYTES",
    "DEFERRED_FLUSH_CHUNK",
    "DEFERRED_FLUSH_SLA_MS",
    "deferred_queue_footprint_bytes",
    "get_microkernel",
    "is_warmup_complete",
    "is_warmup_gate_active",
    "warmup_execution_blocked",
    "ring_warmup_mutex",
    "reset_microkernel_for_tests",
    "schedule_array_warmup",
    "start_microkernel",
]
