"""
Background intelligence compute worker — non-blocking hub tick ingestion.

Hub/Lightstreamer callbacks only enqueue O(1) tick records; heavy numpy work
runs on a dedicated daemon thread so the network loop never stalls.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable

from intelligence.alpha_trail import AlphaOptimisedTrailEngine
from intelligence.microstructure import MicrostructureClassifier
from intelligence.spread_forecast import SpreadWideningForecast
from intelligence.types import IntelligenceSnapshot
from system.engine_log import log_engine
from system.sync_task_guard import SyncTaskGuard

COMPUTE_INTERVAL_SEC = 0.25
_QUEUE_MAX = 4096

_worker_guard = SyncTaskGuard("IntelligenceComputeWorker")


class IntelligenceComputeWorker:
    """Drain tick queue and refresh spread + microstructure verdict caches."""

    def __init__(
        self,
        *,
        spread_model: SpreadWideningForecast | None = None,
        micro_model: MicrostructureClassifier | None = None,
        trail_engine: AlphaOptimisedTrailEngine | None = None,
        interval_sec: float = COMPUTE_INTERVAL_SEC,
    ) -> None:
        self._spread = spread_model or SpreadWideningForecast()
        self._micro = micro_model or MicrostructureClassifier()
        self._trail = trail_engine or AlphaOptimisedTrailEngine()
        self._interval = max(0.05, float(interval_sec))
        self._queue: queue.Queue[tuple[str, float, float, float]] = queue.Queue(
            maxsize=_QUEUE_MAX
        )
        self._snapshot = IntelligenceSnapshot()
        self._snapshot_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending_epics: set[str] = set()
        self._pending_lock = threading.Lock()

    @property
    def spread_model(self) -> SpreadWideningForecast:
        return self._spread

    @property
    def micro_model(self) -> MicrostructureClassifier:
        return self._micro

    @property
    def trail_engine(self) -> AlphaOptimisedTrailEngine:
        return self._trail

    def enqueue_tick(
        self,
        epic: str,
        *,
        bid: float,
        offer: float,
        ts: float | None = None,
    ) -> None:
        """O(1) hub callback — never blocks on numpy."""
        key = str(epic or "").strip()
        if not key or bid <= 0 or offer <= 0:
            return
        spread = float(offer) - float(bid)
        tick_ts = float(ts or time.time())
        try:
            self._spread.record(key, spread)
            self._micro.record_tick(key, bid=bid, offer=offer, ts=tick_ts)
            self._queue.put_nowait((key, bid, offer, tick_ts))
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait((key, bid, offer, tick_ts))
            except queue.Full:
                pass
            with self._pending_lock:
                self._pending_epics.add(key)

    def tick_once(self) -> int:
        """Run one compute pass (tests / manual). Returns epics updated."""
        with _worker_guard.guarded_run() as active:
            if not active:
                return 0
            return self._compute_pending()

    def _compute_pending(self) -> int:
        epics: set[str] = set()
        with self._pending_lock:
            epics.update(self._pending_epics)
            self._pending_epics.clear()
        while True:
            try:
                key, _bid, _offer, _ts = self._queue.get_nowait()
                epics.add(key)
            except queue.Empty:
                break
        if not epics:
            return 0
        now = time.time()
        spread_out: dict[str, Any] = {}
        micro_out: dict[str, Any] = {}
        try:
            for epic in epics:
                spread_out[epic] = self._spread.compute(epic)
                micro_out[epic] = self._micro.classify(epic, now=now)
        except Exception as e:
            log_engine(
                f"IntelligenceComputeWorker compute failed: {type(e).__name__}: {e}"
            )
            return 0
        with self._snapshot_lock:
            self._snapshot.spread.update(spread_out)
            self._snapshot.microstructure.update(micro_out)
            for epic in epics:
                self._snapshot.updated_at[epic] = now
        return len(epics)

    def publish_microstructure_verdict(
        self, epic: str, verdict: Any, *, spread_verdict: Any | None = None
    ) -> None:
        """Publish a freshly computed verdict (boot warmup / hub tick flush)."""
        key = str(epic or "").strip()
        if not key:
            return
        now = time.time()
        with self._snapshot_lock:
            self._snapshot.microstructure[key] = verdict
            if spread_verdict is not None:
                self._snapshot.spread[key] = spread_verdict
            self._snapshot.updated_at[key] = now

    def refresh_epic(
        self,
        epic: str,
        *,
        bid: float | None = None,
        offer: float | None = None,
        ts: float | None = None,
    ) -> None:
        """Flush stale micro cache frame and recompute from live tick buffer."""
        key = str(epic or "").strip()
        if not key:
            return
        if bid is not None and offer is not None and bid > 0 and offer > bid:
            self._micro.record_tick(key, bid=bid, offer=offer, ts=ts)
        ts_now = float(ts or time.time())
        with self._snapshot_lock:
            self._snapshot.microstructure.pop(key, None)
            self._snapshot.updated_at.pop(key, None)
        try:
            micro = self._micro.classify(key, now=ts_now)
            spread = self._spread.compute(key)
        except Exception as e:
            log_engine(
                f"IntelligenceComputeWorker refresh_epic failed: {type(e).__name__}: {e}"
            )
            return
        with self._snapshot_lock:
            self._snapshot.microstructure[key] = micro
            self._snapshot.spread[key] = spread
            self._snapshot.updated_at[key] = ts_now

    def get_snapshot(self) -> IntelligenceSnapshot:
        with self._snapshot_lock:
            return IntelligenceSnapshot(
                spread=dict(self._snapshot.spread),
                microstructure=dict(self._snapshot.microstructure),
                updated_at=dict(self._snapshot.updated_at),
            )

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        try:
            from intelligence.macro_radar import start_macro_radar

            start_macro_radar()
        except Exception:
            pass
        try:
            from system.thread_affinity import spawn_priority_thread

            self._thread = spawn_priority_thread(
                self._loop,
                name="IntelligenceComputeWorker",
                role="intelligence_compute",
                daemon=True,
            )
        except Exception:
            self._thread = threading.Thread(
                target=self._loop,
                daemon=True,
                name="IntelligenceComputeWorker",
            )
            self._thread.start()
        log_engine(
            f"IntelligenceComputeWorker started (interval={self._interval:.2f}s)"
        )

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self._interval + 1.0)
        self._thread = None

    def _loop(self) -> None:
        try:
            from system.thread_affinity import pin_current_thread

            pin_current_thread(role="intelligence_compute")
        except Exception:
            pass
        while not self._stop.wait(self._interval):
            try:
                self.tick_once()
            except Exception as e:
                log_engine(
                    f"IntelligenceComputeWorker loop error: {type(e).__name__}: {e}"
                )


_layer_lock = threading.Lock()
_worker: IntelligenceComputeWorker | None = None


def get_intelligence_worker() -> IntelligenceComputeWorker:
    global _worker
    with _layer_lock:
        if _worker is None:
            _worker = IntelligenceComputeWorker()
        return _worker


def start_intelligence_worker() -> IntelligenceComputeWorker:
    worker = get_intelligence_worker()
    worker.start()
    return worker


def stop_intelligence_worker() -> None:
    global _worker
    with _layer_lock:
        if _worker is not None:
            _worker.stop()


def reset_intelligence_worker_for_tests() -> None:
    global _worker
    with _layer_lock:
        if _worker is not None:
            _worker.stop()
            _worker.spread_model.reset_for_tests()
            _worker.micro_model.reset_for_tests()
        _worker = None


def wire_intelligence_to_hub() -> Callable[[], None]:
    """
    Optional hub bind — does not alter boot coordinator sequence.

    Call from gate3_runner or post_ready_services when intelligence is enabled.
    """
    from system.market_data_hub import on_hub_quote

    worker = get_intelligence_worker()

    def _on_hub(snap: Any) -> None:
        try:
            epic = str(getattr(snap, "epic", "") or "")
            bid = float(getattr(snap, "bid", 0) or 0)
            offer = float(getattr(snap, "offer", 0) or 0)
            ts = float(getattr(snap, "updated_at", 0) or 0)
            worker.enqueue_tick(epic, bid=bid, offer=offer, ts=ts or None)
        except Exception:
            pass

    return on_hub_quote(_on_hub)
