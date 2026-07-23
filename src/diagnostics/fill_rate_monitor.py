"""Non-blocking live fill-rate telemetry for IG OTC MARKET routing.

Hot path only does ``queue.put_nowait`` (or a one-instruction drop on full).
All counter math, slip-multiplier decisions, and PERF_DIAGNOSTICS emission run
on a daemon worker — zero work on the event-driven tick lane.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Literal

from system.engine_log import log_engine

Outcome = Literal["fill", "slippage_reject", "auth_veto", "other_fail", "attempt"]

WINDOW_SIZE = 100
SHORT_WINDOW = 20
FILL_RATE_RELAX_THRESHOLD = 0.40
BASE_SLIP_MULT = 0.5
RELAXED_SLIP_MULT = 1.0
PERF_INTERVAL_SEC = 15 * 60
HIGH_CONVICTION_OBI_ABS = 0.40
_QUEUE_MAX = 4096


@dataclass(slots=True)
class _Event:
    kind: str
    ts: float
    detail: str = ""


class FillRateMonitor:
    """Rolling-window fill telemetry + dynamic slip multiplier cache."""

    __slots__ = (
        "_q",
        "_stop",
        "_thread",
        "_lock",
        "_outcomes",
        "_timed_outcomes",
        "_attempts",
        "_fills",
        "_slippage_rejects",
        "_auth_vetoes",
        "_slip_mult",
        "_last_perf_emit",
        "_started",
        "_sync_mode",
    )

    def __init__(self, *, sync_mode: bool = False) -> None:
        self._q: queue.SimpleQueue[_Event | None] = queue.SimpleQueue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._outcomes: deque[str] = deque(maxlen=WINDOW_SIZE)
        # Time-indexed outcomes for 15m fill-rate instrumentation (non-blocking)
        self._timed_outcomes: deque[tuple[float, str]] = deque(maxlen=2000)
        self._attempts = 0
        self._fills = 0
        self._slippage_rejects = 0
        self._auth_vetoes = 0
        self._slip_mult = BASE_SLIP_MULT
        self._last_perf_emit = 0.0
        self._started = False
        self._sync_mode = bool(sync_mode)

    def start(self) -> None:
        if self._sync_mode or self._started:
            return
        self._started = True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._worker,
            name="fill-rate-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._sync_mode:
            return
        self._stop.set()
        try:
            self._q.put_nowait(None)
        except Exception:
            pass
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=1.0)
        self._thread = None
        self._started = False

    def reset(self) -> None:
        """Test / session reset — clears windows and counters."""
        with self._lock:
            self._outcomes.clear()
            self._timed_outcomes.clear()
            self._attempts = 0
            self._fills = 0
            self._slippage_rejects = 0
            self._auth_vetoes = 0
            self._slip_mult = BASE_SLIP_MULT
            self._last_perf_emit = 0.0
        # Drain queue without blocking
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    # ── hot-path producers (never block, never raise) ─────────────────

    def _emit(self, kind: str, detail: str = "") -> None:
        ev = _Event(kind=kind, ts=time.time(), detail=detail)
        if self._sync_mode:
            self._apply(ev)
            return
        if not self._started:
            self.start()
        try:
            self._q.put_nowait(ev)
        except Exception:
            pass

    def record_attempt(self) -> None:
        self._emit("attempt")

    def record_fill(self) -> None:
        self._emit("fill")
        # Soft hook — journal pulls rates at trade-close; keep fill path non-blocking.
        try:
            from diagnostics.performance_journal import start_performance_journal

            start_performance_journal()
        except Exception:
            pass

    def record_slippage_reject(self, detail: str = "") -> None:
        self._emit("slippage_reject", detail=str(detail or "")[:120])

    def record_auth_veto(self, detail: str = "") -> None:
        self._emit("auth_veto", detail=str(detail or "")[:80])

    def record_other_fail(self, detail: str = "") -> None:
        self._emit("other_fail", detail=str(detail or "")[:80])

    def request_perf_log(self, *, force: bool = False) -> None:
        self._emit("perf_force" if force else "perf_tick")

    def notify_backoff_activated(self) -> None:
        """Emit PERF_DIAGNOSTICS immediately on 5-strike back-off."""
        self._emit("perf_force")

    # ── lock-free / short-lock readers for routing ────────────────────

    def current_slip_multiplier(self) -> float:
        """Cached multiplier — plain float read under brief lock."""
        with self._lock:
            return float(self._slip_mult)

    def rolling_fill_rate(self, window: int = SHORT_WINDOW) -> float | None:
        with self._lock:
            return self._fill_rate_unlocked(window)

    def rolling_fill_rate_timed(self, seconds: float = 900.0) -> float | None:
        """Fill rate over a wall-clock window (default 15 minutes)."""
        cutoff = time.time() - float(seconds)
        with self._lock:
            sample = [o for ts, o in self._timed_outcomes if ts >= cutoff]
        if not sample:
            return None
        fills = sum(1 for o in sample if o == "fill")
        return fills / float(len(sample))

    def rolling_fill_rate_15m(self) -> float | None:
        return self.rolling_fill_rate_timed(900.0)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            rate = self._fill_rate_unlocked(WINDOW_SIZE)
            short = self._fill_rate_unlocked(SHORT_WINDOW)
        timed = self.rolling_fill_rate_15m()
        with self._lock:
            return {
                "attempts": self._attempts,
                "fills": self._fills,
                "slippage_rejects": self._slippage_rejects,
                "auth_vetoes": self._auth_vetoes,
                "fill_rate_pct": None if rate is None else round(100.0 * rate, 1),
                "fill_rate_short_pct": None if short is None else round(100.0 * short, 1),
                "fill_rate_15m_pct": None if timed is None else round(100.0 * timed, 1),
                "slip_multiplier": float(self._slip_mult),
                "window": len(self._outcomes),
            }

    def format_perf_line(self) -> str:
        snap = self.snapshot()
        rate = snap["fill_rate_pct"]
        rate_s = "n/a" if rate is None else f"{rate:.0f}%"
        return (
            f"[PERF_DIAGNOSTICS] Fill Rate: {rate_s} | "
            f"Slippage Rejects: {snap['slippage_rejects']} | "
            f"Auth Vetoes: {snap['auth_vetoes']} | "
            f"Current Slip Multiplier: {snap['slip_multiplier']:.1f}x"
        )

    # ── worker ────────────────────────────────────────────────────────

    def _worker(self) -> None:
        next_periodic = time.time() + PERF_INTERVAL_SEC
        while not self._stop.is_set():
            timeout = max(0.05, min(1.0, next_periodic - time.time()))
            try:
                ev = self._q.get(timeout=timeout)
            except queue.Empty:
                now = time.time()
                if now >= next_periodic:
                    self._emit_perf(force=True)
                    next_periodic = now + PERF_INTERVAL_SEC
                continue
            if ev is None:
                break
            self._apply(ev)
            if ev.kind == "perf_force":
                next_periodic = time.time() + PERF_INTERVAL_SEC

    def _apply(self, ev: _Event) -> None:
        if ev.kind in ("perf_force", "perf_tick"):
            self._emit_perf(force=ev.kind == "perf_force")
            return

        with self._lock:
            if ev.kind == "attempt":
                self._attempts += 1
                return

            if ev.kind == "fill":
                self._fills += 1
                self._outcomes.append("fill")
                self._timed_outcomes.append((ev.ts, "fill"))
            elif ev.kind == "slippage_reject":
                self._slippage_rejects += 1
                self._outcomes.append("slippage_reject")
                self._timed_outcomes.append((ev.ts, "slippage_reject"))
            elif ev.kind == "auth_veto":
                self._auth_vetoes += 1
                # Auth vetoes are tracked but do not dilute broker fill-rate window
                return
            elif ev.kind == "other_fail":
                self._outcomes.append("other_fail")
                self._timed_outcomes.append((ev.ts, "other_fail"))
            else:
                return

            self._recompute_slip_mult_unlocked()

        # Periodic emit check outside lock
        now = time.time()
        with self._lock:
            due = (now - self._last_perf_emit) >= PERF_INTERVAL_SEC
        if due:
            self._emit_perf(force=False)

    def _fill_rate_unlocked(self, window: int) -> float | None:
        if not self._outcomes:
            return None
        sample = list(self._outcomes)[-int(window) :]
        if not sample:
            return None
        fills = sum(1 for o in sample if o == "fill")
        return fills / float(len(sample))

    def _recompute_slip_mult_unlocked(self) -> None:
        rate = self._fill_rate_unlocked(SHORT_WINDOW)
        if rate is None:
            self._slip_mult = BASE_SLIP_MULT
            return
        # Need a full short window before relaxing
        if len(self._outcomes) < SHORT_WINDOW:
            self._slip_mult = BASE_SLIP_MULT
            return
        if rate < FILL_RATE_RELAX_THRESHOLD:
            self._slip_mult = RELAXED_SLIP_MULT
        else:
            self._slip_mult = BASE_SLIP_MULT

    def _emit_perf(self, *, force: bool) -> None:
        with self._lock:
            now = time.time()
            if not force and (now - self._last_perf_emit) < PERF_INTERVAL_SEC:
                return
            self._last_perf_emit = now
        try:
            log_engine(self.format_perf_line())
        except Exception:
            pass
        # Instrumentation cycle on the monitor worker — never on the tick lane
        if not self._sync_mode:
            try:
                from diagnostics.param_tuner import run_instrumentation_cycle

                run_instrumentation_cycle()
            except Exception:
                pass

    def drain_for_tests(self, timeout: float = 0.5) -> None:
        """Wait until the async queue is empty (unit tests)."""
        if self._sync_mode:
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._q.empty():
                time.sleep(0.01)  # let worker finish current event
                if self._q.empty():
                    return
            time.sleep(0.005)


def is_high_conviction_obi(direction: str, obi: float | None) -> bool:
    """
    Exceptionally strong book pressure for slip relaxation.

    Accepts |OBI| >= 0.40. Supports both supportive-signed feeds
    (BUY >= +0.40 / SELL <= -0.40) and the alternate-signed example
    (BUY <= -0.40 / SELL >= +0.40).
    """
    if obi is None:
        return False
    try:
        v = float(obi)
    except (TypeError, ValueError):
        return False
    if abs(v) < HIGH_CONVICTION_OBI_ABS:
        return False
    return True


_monitor: FillRateMonitor | None = None
_monitor_lock = threading.Lock()


def get_fill_rate_monitor(*, sync_mode: bool | None = None) -> FillRateMonitor:
    """Process-wide singleton. ``sync_mode=True`` for deterministic unit tests."""
    global _monitor
    with _monitor_lock:
        if _monitor is None:
            _monitor = FillRateMonitor(sync_mode=bool(sync_mode))
            if not _monitor._sync_mode:
                _monitor.start()
        elif sync_mode is True and not _monitor._sync_mode:
            # Upgrade to sync for tests
            _monitor.stop()
            _monitor = FillRateMonitor(sync_mode=True)
        return _monitor


def reset_fill_rate_monitor_for_tests() -> None:
    mon = get_fill_rate_monitor(sync_mode=True)
    mon.reset()
