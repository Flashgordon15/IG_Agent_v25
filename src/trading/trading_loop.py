"""
v25 agent orchestration loop — 5s tick, 7 gates, snapshot IPC (Section 4.5 Step 9).

Owns gate evaluation order and calls execution.trading_loop.TradingLoop.process_tick
for gate 7 only. No GUI imports. Trading continues if the FastAPI dashboard fails.
"""

from __future__ import annotations

import math
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from api.snapshot import GATE_NAMES
from api.snapshot_store import publish_tick
from data.models import Quote
from execution.trading_loop import TickOutcome
from execution.trading_loop import TradingLoop as ExecutionTickLoop
from signals.signal_engine import SignalResult
from system.config import Config
from system.engine_log import log_engine
from system.paths import project_root
from trading.environment_scorer import (
    FACTOR_ATR_MAX,
    FACTOR_SESSION_MAX,
    FACTOR_SPREAD_MAX,
    FACTOR_TREND_MAX,
    GATE_PASS_MIN,
    SAFE_DEFAULT_SCORE,
    EnvironmentScorer,
)
from trading.gate_readiness import compute_trade_readiness, format_health_badge_text
from trading.open_position_view import (
    enrich_positions_with_quote,
    normalize_sync_position,
    positions_from_store_rows,
)
from trading.points_engine import PointsEngine
from trading.price_trend import compute_price_trend_30m
from trading.session_manager import SessionManager
from trading.session_summary import SessionTickTracker, write_session_end_summary
from trading.trade_eligibility import build_trade_eligibility

STAGE1_GBP_RISK_CAP = 150.0
SPREAD_NORMAL_MULTIPLIER = 2.5
DAILY_LOSS_LIMIT_GBP = 200.0
DEFAULT_TICK_INTERVAL_SEC = 5.0
FLATTEN_VERIFY_WAIT_SEC = 10.0


def signal_gate_explanation(sig: SignalResult, threshold: float) -> tuple[str, str]:
    """Human-readable (gate_detail, block_reason) for dashboard / gates."""
    conf = float(sig.adjusted_confidence)
    snap = sig.snapshot or {}
    raw = str(snap.get("raw_signal") or "").strip()

    if sig.signal in ("BUY", "SELL"):
        if conf < threshold:
            msg = f"{sig.signal} {conf:.1f}% below {threshold:.1f}% threshold"
            return msg, msg
        return f"{sig.signal} {conf:.1f}% (>= {threshold:.1f}%)", ""

    if snap.get("rsi_block"):
        reason = str(snap["rsi_block"])
        lead = raw or "BUY/SELL"
        return f"WAIT — {reason} ({lead} score {conf:.1f}%)", reason

    if "blocked:" in sig.notes:
        reason = sig.notes.split("blocked:", 1)[1].split(",", 1)[0].strip()
        return f"WAIT — {reason} ({conf:.1f}% score held)", reason

    notes_lower = (sig.notes or "").lower()
    if "duplicate suppressed" in notes_lower:
        reason = "awaiting next closed 5m bar"
        return f"WAIT — {reason}", reason
    if "collecting live data" in notes_lower:
        reason = "collecting candle history"
        return f"WAIT — {reason}", reason

    for part in (sig.notes or "").split("|"):
        part = part.strip()
        if "BLOCKED:" in part or part.startswith("vol regime="):
            return f"WAIT — {part}", part

    buy = snap.get("buy_score")
    sell = snap.get("sell_score")
    try:
        b = float(buy) if buy is not None else None
        s = float(sell) if sell is not None else None
    except (TypeError, ValueError):
        b = s = None

    if b is not None and s is not None and max(b, s) < threshold:
        reason = f"scores buy={b:.0f} sell={s:.0f} need >={threshold:.0f}%"
        return f"WAIT — {reason}", reason

    if raw in ("BUY", "SELL"):
        reason = f"{raw} scored {conf:.1f}% but output is WAIT"
        return f"WAIT — {reason}", reason

    return f"WAIT — no tradable direction ({conf:.1f}%)", "no BUY/SELL on closed bar"


def _feeder_bar_from_snapshot(
    snap: dict[str, Any],
) -> tuple[str, dict[str, float]] | None:
    """Extract closed-bar OHLC for feeder ``bar_close`` (handles pandas Series)."""
    last_raw = snap.get("last")
    last: dict[str, Any] = {}
    if isinstance(last_raw, dict):
        last = last_raw
    elif last_raw is not None:
        try:
            import pandas as pd

            if isinstance(last_raw, pd.Series):
                last = last_raw.to_dict()
            elif hasattr(last_raw, "to_dict"):
                last = last_raw.to_dict()
        except Exception:
            return None
    bar_time = str(last.get("time") or snap.get("bar_time") or "").strip()
    if not bar_time or not last:
        return None

    def _f(key: str, alt: str | None = None) -> float:
        try:
            val = last.get(key) if alt is None else last.get(key, last.get(alt))
            return float(val or 0)
        except (TypeError, ValueError):
            return 0.0

    return bar_time, {
        "open": _f("open", "price"),
        "high": _f("high", "price"),
        "low": _f("low", "price"),
        "close": _f("close", "price"),
        "volume": _f("volume"),
    }


def _atr_from_signal_snapshot(snapshot: dict[str, Any] | None) -> float:
    if not snapshot:
        return 0.0
    last = snapshot.get("last")
    try:
        if last is not None and hasattr(last, "get"):
            return float(last.get("atr", 0) or 0)
    except (TypeError, ValueError):
        pass
    try:
        return float(snapshot.get("atr", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class GateResult:
    name: str
    passed: bool
    value: Any = None
    detail: str = ""


@dataclass
class TickContext:
    quote: Quote
    gates: list[GateResult] = field(default_factory=list)
    all_passed: bool = False
    wait_reason: str = ""
    signal: SignalResult | None = None
    fitness: float = 0.0
    outcome: TickOutcome | None = None


class TradingLoop:
    """
    Standalone orchestrator — 7 gates in spec order, then execution process_tick.

    POST /api/close and the dashboard are separate; this module never imports GUI code.
    """

    def __init__(
        self,
        config: Config,
        *,
        market: str,
        epic: str,
        session_manager: SessionManager,
        environment_scorer: EnvironmentScorer,
        points_engine: PointsEngine,
        signal_engine: Any,
        execution_loop: ExecutionTickLoop,
        quote_source: Callable[[], Quote | None],
        learning_store: Any | None = None,
        tick_interval_sec: float | None = None,
        on_flatten: Callable[[], int] | None = None,
        position_sync: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        publish_snapshots: bool = True,
        on_snapshot: Callable[[dict[str, Any]], None] | None = None,
        instrument_id: str = "",
    ) -> None:
        self._config = config
        self._market = market
        self._epic = epic
        self._session = session_manager
        self._env = environment_scorer
        self._points = points_engine
        self._signal_engine = signal_engine
        self._execution_loop = execution_loop
        self._quote_source = quote_source
        self._store = learning_store
        self._tick_interval = float(
            tick_interval_sec
            if tick_interval_sec is not None
            else getattr(config, "refresh_seconds", DEFAULT_TICK_INTERVAL_SEC)
        )
        self._on_flatten = on_flatten
        self._position_sync = position_sync
        self._clock = clock or datetime.now
        self._publish_snapshots = bool(publish_snapshots)
        self._on_snapshot = on_snapshot
        self._instrument_id = str(instrument_id or "")

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()
        self._last_context: TickContext | None = None
        self._tick_count = 0
        self._session_tracker = SessionTickTracker()
        self._ml_store: Any | None = None
        self._ml_decision_log: list[dict] = []  # rolling last-20 ML blend decisions
        self._gap_first_seen_at: datetime | None = (
            None  # wall-clock when gap first detected
        )
        self._balance_refresher: Any | None = None
        self._last_tick_mono: float = 0.0
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: threading.Thread | None = None
        self._silence_alert_sent = False
        # Market constraints cached at session level in a background thread so the
        # trading-loop tick is never blocked by a REST call to /markets/{epic}.
        self._market_constraints_cache: dict[str, Any] = {}
        self._market_constraints_fetched: bool = False
        self._feeder_last_bar_key: str | None = None
        self._last_ml_prob: float | None = None
        self._last_sig_direction: str = "WAIT"

    @property
    def config(self) -> Config:
        return self._config

    @property
    def last_context(self) -> TickContext | None:
        with self._lock:
            return self._last_context

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._stop.clear()
            self._watchdog_stop.clear()
            self._silence_alert_sent = False
            self._running = True
            self._thread = threading.Thread(
                target=self._loop_thread,
                name=f"ig-agent-trading-loop-{self._epic[-12:]}",
                daemon=True,
            )
            self._thread.start()
            self._watchdog_thread = threading.Thread(
                target=self._silence_watchdog,
                name=f"ig-loop-watchdog-{self._epic[-12:]}",
                daemon=True,
            )
            self._watchdog_thread.start()
        log_engine(f"trading_loop started epic={self._epic}")

    def stop(self) -> None:
        self._stop.set()
        self._watchdog_stop.set()
        thread = None
        watchdog = None
        with self._lock:
            thread = self._thread
            watchdog = self._watchdog_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=self._tick_interval + 2.0)
        if watchdog is not None and watchdog.is_alive():
            watchdog.join(timeout=2.0)
        with self._lock:
            self._running = False
            self._thread = None
            self._watchdog_thread = None
        log_engine(f"trading_loop stopped epic={self._epic}")

    def run_once(self) -> TickContext | None:
        """Run a single tick synchronously (tests)."""
        return self._run_tick()

    def _loop_thread(self) -> None:
        from system.stream_ready import wait_stream_ready

        log_engine(
            f"trading_loop thread starting epic={self._epic} — awaiting stream_ready"
        )
        ready = wait_stream_ready(timeout=120.0, epic=self._epic)
        log_engine(
            f"trading_loop thread epic={self._epic} stream_ready={ready} — entering tick loop"
        )
        try:
            while not self._stop.is_set():
                try:
                    self._run_tick()
                except Exception as e:
                    self._session_tracker.record_error()
                    log_engine(
                        f"trading_loop tick error (continuing): {type(e).__name__}: {e}"
                    )
                if self._stop.wait(self._tick_interval):
                    break
        finally:
            with self._lock:
                self._running = False

    def _stream_live_for_watchdog(self) -> bool:
        try:
            from system.market_data_hub import get_market_data_hub
            from system.stream_ready import is_stream_ready

            if not is_stream_ready():
                return False
            snap = get_market_data_hub().get_snapshot(self._epic)
            if snap is None or snap.bid <= 0 or snap.offer <= 0:
                return False
            return float(snap.age_seconds()) <= 60.0
        except Exception:
            return False

    def _silence_watchdog(self) -> None:
        import time

        silence_sec = 120.0
        while not self._watchdog_stop.wait(15.0):
            if self._stop.is_set():
                break
            last = self._last_tick_mono
            if last <= 0:
                continue
            if time.monotonic() - last < silence_sec:
                continue
            if not self._stream_live_for_watchdog():
                continue
            if self._silence_alert_sent:
                continue
            self._silence_alert_sent = True
            log_engine(
                f"CRITICAL: Trading loop silent for >{int(silence_sec)}s — possible deadlock "
                f"(market={self._market} epic={self._epic})"
            )
            try:
                from system.telegram_notifier import get_telegram_notifier

                notifier = get_telegram_notifier()
                if notifier is not None:
                    notifier.send_alert(
                        f"⚠️ Trading loop deadlock detected — restarting {self._market}",
                        dedupe_key=f"loop_silent:{self._epic}",
                    )
            except Exception:
                pass
            # Self-heal: signal the stuck loop to stop so the orchestrator can respawn it.
            log_engine(
                f"Watchdog: requesting loop restart for {self._market} ({self._epic})"
            )
            self._stop.set()

    def _run_tick(self) -> TickContext | None:
        import time

        self._last_tick_mono = time.monotonic()
        self._silence_alert_sent = False
        quote = self._quote_source()
        if quote is None:
            ctx = TickContext(
                quote=Quote(self._clock(), 0.0, 0.0),
                wait_reason="no quote",
            )
            ctx.gates = self._offline_gates(ctx.wait_reason)
            log_engine(
                f"WAIT — no quote epic={self._epic} market={self._market} "
                "(hub/REST returned no bid/offer)"
            )
            try:
                from system.gate_activity import record_gate_evaluation

                record_gate_evaluation(self._epic)
            except Exception:
                pass
            self._publish_snapshot(ctx)
            with self._lock:
                self._last_context = ctx
            return ctx

        self._tick_count += 1
        try:
            from system.market_data_hub import get_market_data_hub

            spread_pts = max(0.0, float(quote.offer) - float(quote.bid))
            if spread_pts > 0:
                get_market_data_hub().record_spread(self._epic, spread_pts)
        except Exception:
            pass
        self._maybe_refresh_account_balance()
        try:
            self._signal_engine.add_quote(self._market, quote)
        except Exception as e:
            log_engine(f"signal_engine.add_quote failed: {type(e).__name__}: {e}")
        try:
            self._session.on_tick(quote)
        except Exception as e:
            log_engine(f"session_manager.on_tick failed: {type(e).__name__}: {e}")

        if self._session.is_session_open():
            self._session_tracker.reset_for_session(self._session.session_open_time)

        self._flatten_if_needed()

        gates = self._evaluate_gates(quote)
        self._log_gate_check(quote, gates)
        self._emit_feeder_telemetry(quote, gates)
        try:
            from system.gate_activity import record_gate_evaluation

            record_gate_evaluation(self._epic)
        except Exception:
            pass
        self._maybe_consume_points_skip_on_suppressed_signal(gates)
        all_passed = all(g.passed for g in gates)
        wait_reason = ""
        if not all_passed:
            failed = next(g for g in gates if not g.passed)
            wait_reason = f"{failed.name}: {failed.detail}"

        signal: SignalResult | None = None
        fitness = 0.0
        for g in gates:
            if g.name == "environment_fitness":
                v = g.value
                if isinstance(v, dict):
                    fitness = float(v.get("score", 0) or 0)
                else:
                    fitness = float(v or 0.0)
            if g.name == "signal_confidence" and isinstance(g.value, dict):
                signal = g.value.get("signal")

        outcome: TickOutcome | None = None
        try:
            self._execution_loop.execution_engine.update_positions(
                self._market, self._epic, quote
            )
        except Exception as e:
            log_engine(f"update_positions failed: {type(e).__name__}: {e}")

        if all_passed:
            sig_dir = "?"
            confidence = 0.0
            prefetched: SignalResult | None = None
            for g in gates:
                if g.name == "signal_confidence" and isinstance(g.value, dict):
                    sig_dir = str(g.value.get("direction") or "?")
                    raw_sig = g.value.get("signal")
                    if isinstance(raw_sig, SignalResult):
                        prefetched = raw_sig
                    try:
                        confidence = float(g.value.get("confidence") or 0)
                    except (TypeError, ValueError):
                        confidence = 0.0
                    break
            trade_size = self._trade_size_from_gates(gates, confidence)
            self._emit_feeder_order_intent(gates, confidence, trade_size)
            log_engine(
                f"ALL GATES PASSED — attempting trade "
                f"market={self._market} epic={self._epic} "
                f"confidence={confidence:.1f} size={trade_size}"
            )
            log_engine(
                f"GATES PASS epic={self._epic} market={self._market} "
                f"signal={sig_dir} fitness={int(round(fitness))}% "
                f"allow_live_trading={self._config.allow_live_trading} "
                f"dry_run={self._config.dry_run} "
                f"auto_trade={self._execution_loop.auto_trade} "
                "— invoking execution pipeline"
            )
            try:
                outcome = self._execution_loop.process_tick(
                    self._market,
                    self._epic,
                    quote,
                    prefetched_signal=prefetched,
                )
                self._log_execution_outcome(outcome)
                exec_wait = self._execution_wait_reason(outcome)
                if exec_wait:
                    wait_reason = exec_wait
                    all_passed = False
                    self._mark_execution_gate_blocked(gates, exec_wait)
                    log_engine(f"WAIT — {wait_reason}")
            except Exception as e:
                log_engine(f"gate 7 execution failed: {type(e).__name__}: {e}")
                wait_reason = f"execution: {type(e).__name__}"
                all_passed = False
        else:
            log_engine(f"WAIT — {wait_reason}")

        ctx = TickContext(
            quote=quote,
            gates=gates,
            all_passed=all_passed,
            wait_reason=wait_reason,
            signal=signal if isinstance(signal, SignalResult) else None,
            fitness=fitness,
            outcome=outcome,
        )
        self._publish_snapshot(ctx)
        with self._lock:
            self._last_context = ctx
        return ctx

    def _rate_limit_gate_status(self) -> tuple[bool, str]:
        try:
            from system.rate_limit_manager import get_rate_limit_manager

            mgr = get_rate_limit_manager()
            if not mgr.is_rest_blocked():
                return True, ""
            rem = int(mgr.seconds_until_rest_reset())
            mins, secs = divmod(max(0, rem), 60)
            detail = f"IG API rate limit — REST blocked for {mins}m {secs}s"
            return False, detail
        except Exception:
            return True, ""

    def _feeder_session_name(self) -> str:
        try:
            from signals.indicators import session_name

            return str(session_name())
        except Exception:
            return ""

    def _emit_feeder_telemetry(self, quote: Quote, gates: list[GateResult]) -> None:
        """v25→v26 feeder: gates, signal_eval, bar_close (non-blocking)."""
        try:
            from feeder.event_bus import (
                emit,
                emit_bar_close,
                emit_gate_result,
                emit_signal_eval,
            )

            session = self._feeder_session_name()
            epic = self._epic
            market = self._market
            gates_passed = [g.name for g in gates if g.passed]

            for g in gates:
                val = g.value if isinstance(g.value, dict) else None
                emit_gate_result(
                    epic=epic,
                    market=market,
                    session=session,
                    gate_name=g.name,
                    passed=g.passed,
                    detail=(g.detail or "")[:500],
                    value=val,
                )

            sig_gate = next((g for g in gates if g.name == "signal_confidence"), None)
            if sig_gate and isinstance(sig_gate.value, dict):
                raw_sig = sig_gate.value.get("signal")
                snap: dict[str, Any] = {}
                direction = "WAIT"
                raw_score = 0.0
                adjusted = 0.0
                setup_key = ""
                reason = ""
                if isinstance(raw_sig, SignalResult):
                    direction = str(raw_sig.signal or "WAIT")
                    raw_score = float(raw_sig.raw_confidence or 0)
                    adjusted = float(raw_sig.adjusted_confidence or 0)
                    setup_key = str(raw_sig.setup_key or "")
                    reason = str(raw_sig.notes or "")
                    snap = dict(raw_sig.snapshot or {})
                ml_prob = sig_gate.value.get("ml_probability")
                ml_f = float(ml_prob) if ml_prob is not None else None
                emit_signal_eval(
                    epic=epic,
                    market=market,
                    session=session,
                    direction=direction,
                    raw_score=raw_score,
                    adjusted_score=float(sig_gate.value.get("confidence") or adjusted),
                    setup_key=setup_key or str(sig_gate.value.get("setup") or ""),
                    would_fire=bool(sig_gate.passed),
                    reason=reason,
                    gates_passed=gates_passed,
                    ml_probability=ml_f,
                )
                bar_payload = _feeder_bar_from_snapshot(snap)
                if bar_payload is not None:
                    bar_time, ohlc = bar_payload
                    bar_key = f"{epic}:{bar_time}"
                    if bar_key != self._feeder_last_bar_key:
                        self._feeder_last_bar_key = bar_key
                        emit_bar_close(
                            epic=epic,
                            market=market,
                            session=session,
                            bar_time=bar_time,
                            open_=ohlc["open"],
                            high=ohlc["high"],
                            low=ohlc["low"],
                            close=ohlc["close"],
                            volume=ohlc["volume"],
                        )

            if self._tick_count % 12 == 0:
                from feeder.event_bus import emit_regime_snapshot

                fit_gate = next(
                    (g for g in gates if g.name == "environment_fitness"), None
                )
                pts_gate = next((g for g in gates if g.name == "points_state"), None)
                fitness = None
                if fit_gate and isinstance(fit_gate.value, (int, float)):
                    fitness = float(fit_gate.value)
                elif fit_gate and isinstance(fit_gate.value, dict):
                    fitness = fit_gate.value.get("fitness")
                vol_regime = ""
                if sig_gate and isinstance(sig_gate.value, dict):
                    raw_sig = sig_gate.value.get("signal")
                    if isinstance(raw_sig, SignalResult):
                        snap = raw_sig.snapshot or {}
                        vol_regime = str(snap.get("vol_regime") or "")
                points_state = ""
                if pts_gate and isinstance(pts_gate.value, dict):
                    points_state = str(pts_gate.value.get("state") or "")
                spread = max(0.0, float(quote.offer) - float(quote.bid))
                emit_regime_snapshot(
                    epic=epic,
                    market=market,
                    session=session,
                    fitness=float(fitness) if fitness is not None else None,
                    vol_regime=vol_regime,
                    points_state=points_state,
                    spread=spread,
                )

            if self._tick_count % 60 == 0:
                spread = max(0.0, float(quote.offer) - float(quote.bid))
                emit(
                    "quote_tick",
                    epic=epic,
                    market=market,
                    session=session,
                    payload={
                        "bid": float(quote.bid),
                        "offer": float(quote.offer),
                        "spread_pts": spread,
                    },
                )
                try:
                    daily_pnl = float(self._daily_pnl_signed_gbp())
                except Exception:
                    daily_pnl = 0.0
                emit(
                    "account_snapshot",
                    epic=epic,
                    market=market,
                    session=session,
                    payload={
                        "points_state": self._points.get_state(),
                        "daily_pnl_gbp": daily_pnl,
                        "open_epic": int(
                            self._execution_loop.execution_engine.trade_tracker.count_open_for_epic(
                                epic
                            )
                        ),
                    },
                )
        except Exception:
            pass

    def _emit_feeder_order_intent(
        self,
        gates: list[GateResult],
        confidence: float,
        trade_size: float,
    ) -> None:
        try:
            from feeder.event_bus import emit_order_intent

            direction = "WAIT"
            setup_key = ""
            stop_pts = float(self._config.stop_distance_points)
            risk_gbp = 0.0
            for g in gates:
                if g.name == "signal_confidence" and isinstance(g.value, dict):
                    direction = str(g.value.get("direction") or "WAIT")
                    setup_key = str(g.value.get("setup") or "")
                if g.name == "risk_validation" and isinstance(g.value, dict):
                    stop_pts = float(g.value.get("stop") or stop_pts)
                    risk_gbp = float(g.value.get("risk_gbp") or 0)
            emit_order_intent(
                epic=self._epic,
                market=self._market,
                session=self._feeder_session_name(),
                direction=direction,
                size=float(trade_size),
                confidence=float(confidence),
                setup_key=setup_key,
                risk_gbp=risk_gbp,
                stop_points=stop_pts,
            )
        except Exception:
            pass

    def _log_gate_check(self, quote: Quote, gates: list[GateResult]) -> None:
        sig_dir = "WAIT"
        confidence = 0.0
        setup = ""
        fitness = 0.0
        for g in gates:
            if g.name == "environment_fitness":
                v = g.value
                if isinstance(v, dict):
                    fitness = float(v.get("score", 0) or 0)
                else:
                    fitness = float(v or 0.0)
            if g.name == "signal_confidence" and isinstance(g.value, dict):
                sig_dir = str(g.value.get("direction") or "WAIT")
                setup = str(g.value.get("setup") or "")
                try:
                    confidence = float(g.value.get("confidence") or 0)
                except (TypeError, ValueError):
                    confidence = 0.0
        tracker = self._execution_loop.execution_engine.trade_tracker
        open_epic = int(tracker.count_open_for_epic(self._epic))
        total_raw = tracker.count_open_total()
        open_total = (
            max(open_epic, int(total_raw))
            if isinstance(total_raw, (int, float))
            else open_epic
        )
        threshold = float(self._points.trade_confidence_threshold(self._config))
        trade_size = self._trade_size_from_gates(gates, confidence)
        log_engine(
            f"GATE CHECK {self._epic}: confidence={confidence:.1f} "
            f"threshold={threshold:.1f} fitness={fitness:.0f} "
            f"allow_live={self._config.allow_live_trading} "
            f"dry_run={self._config.dry_run} "
            f"size={trade_size} direction={sig_dir} setup={setup or '—'} "
            f"open_epic={open_epic} open_total={open_total} "
            f"max_epic={self._config.max_positions_per_epic} "
            f"max_total={self._config.max_open_positions} "
            f"all_pass={all(g.passed for g in gates)}"
        )

    def _execution_wait_reason(self, outcome: Any | None) -> str:
        if outcome is None:
            return "execution: process_tick returned no outcome"
        block = getattr(outcome, "block_reason", None)
        if block:
            return f"execution: {block}"
        sig = getattr(outcome, "signal", None)
        direction = str(getattr(sig, "signal", "WAIT") if sig else "WAIT")
        validation = getattr(outcome, "validation", None)
        if direction not in ("BUY", "SELL"):
            return f"execution: inner signal={direction} (outer gates had passed)"
        if validation is not None and not getattr(validation, "allowed", False):
            reasons = getattr(validation, "reasons", None) or []
            return f"execution: {'; '.join(str(r) for r in reasons) or 'validation failed'}"
        execution = getattr(outcome, "execution", None)
        if execution is None:
            return "execution: validation OK but no order submitted"
        success = bool(getattr(execution, "success", False))
        action = str(getattr(execution, "action", "") or "")
        if success or action == "SUBMITTED":
            return ""
        rejection = str(
            getattr(execution, "rejection_reason", "") or action or "rejected"
        )
        return f"execution: {rejection}"

    def _mark_execution_gate_blocked(
        self, gates: list[GateResult], detail: str
    ) -> None:
        for idx, g in enumerate(gates):
            if g.name != "execution":
                continue
            gates[idx] = GateResult(
                name="execution",
                passed=False,
                value="blocked",
                detail=detail,
            )
            break

    def _trade_size_from_gates(
        self, gates: list[GateResult], confidence: float
    ) -> float:
        for g in gates:
            if g.name == "risk_validation" and isinstance(g.value, dict):
                for key in ("actual_size", "effective_size", "base_size"):
                    try:
                        val = float(g.value.get(key) or 0)
                    except (TypeError, ValueError):
                        val = 0.0
                    if val > 0:
                        return val
        try:
            mult = float(self._points.get_size_multiplier(confidence))
            return max(0.0, float(self._config.trade_size) * mult)
        except Exception:
            return float(self._config.trade_size)

    def _log_execution_outcome(self, outcome: Any | None) -> None:
        """Log post-gate execution decision (silent blocks previously had no WAIT line)."""
        if outcome is None:
            log_engine(
                f"EXEC SKIP epic={self._epic} — process_tick returned no outcome"
            )
            return
        block = getattr(outcome, "block_reason", None)
        if block:
            log_engine(f"EXEC BLOCKED epic={self._epic} — {block}")
            return
        sig = getattr(outcome, "signal", None)
        direction = str(getattr(sig, "signal", "WAIT") if sig else "WAIT")
        validation = getattr(outcome, "validation", None)
        if direction not in ("BUY", "SELL"):
            log_engine(
                f"EXEC SKIP epic={self._epic} — inner signal={direction} "
                "(outer gates had passed)"
            )
            return
        if validation is not None and not getattr(validation, "allowed", False):
            reasons = getattr(validation, "reasons", None) or []
            log_engine(
                f"EXEC BLOCKED epic={self._epic} validation — "
                f"{'; '.join(str(r) for r in reasons) or 'failed'}"
            )
            return
        execution = getattr(outcome, "execution", None)
        if execution is None:
            log_engine(
                f"EXEC SKIP epic={self._epic} signal={direction} — "
                "validation OK but no execution (auto_trade/live_gate/pending?)"
            )
            return
        action = str(getattr(execution, "action", "") or "")
        success = bool(getattr(execution, "success", False))
        rejection = str(getattr(execution, "rejection_reason", "") or "")
        if success or action == "SUBMITTED":
            log_engine(f"EXEC OK epic={self._epic} signal={direction} action={action}")
            try:
                from system.telegram_notifier import send_critical_alert

                params = getattr(execution, "execution_params", None) or {}
                size = params.get("size", self._config.trade_size)
                send_critical_alert(f"📈 Trade: {direction} {self._epic} size={size}")
            except Exception as e:
                log_engine(f"telegram trade alert failed: {type(e).__name__}: {e}")
        else:
            log_engine(
                f"EXEC REJECTED epic={self._epic} signal={direction} "
                f"action={action} reason={rejection or 'unknown'}"
            )

    def _offline_gates(self, reason: str) -> list[GateResult]:
        gates: list[GateResult] = []
        for name in GATE_NAMES:
            gates.append(GateResult(name=name, passed=False, value=None, detail=reason))
        return gates

    def _evaluate_gates(self, quote: Quote) -> list[GateResult]:
        results: list[GateResult] = []
        for name in GATE_NAMES:
            try:
                if name == "session_open":
                    results.append(self._gate_session_open())
                elif name == "cold_start_gap":
                    results.append(self._gate_cold_start_gap(quote))
                elif name == "environment_fitness":
                    results.append(self._gate_environment_fitness(quote))
                elif name == "points_state":
                    results.append(self._gate_points_state())
                elif name == "risk_validation":
                    results.append(self._gate_risk_validation(quote))
                elif name == "expectancy_ok":
                    results.append(self._gate_expectancy_ok())
                elif name == "calendar_ok":
                    results.append(self._gate_calendar_ok())
                elif name == "signal_confidence":
                    results.append(self._gate_signal_confidence())
                elif name == "ml_veto":
                    results.append(self._gate_ml_veto())
                elif name == "execution":
                    prior_ok = bool(results) and all(r.passed for r in results)
                    rate_ok, rate_detail = self._rate_limit_gate_status()
                    if not rate_ok:
                        prior_ok = False
                        detail = rate_detail
                        value = "rate_limited"
                    elif prior_ok:
                        detail = "Ready — order path armed (process_tick on this tick)"
                        value = "armed"
                    else:
                        blockers = [
                            r.name.replace("_", " ") for r in results if not r.passed
                        ]
                        blocker_txt = ", ".join(blockers[:3])
                        if len(blockers) > 3:
                            blocker_txt += f" +{len(blockers) - 3} more"
                        detail = (
                            f"Not armed — waiting on: {blocker_txt}"
                            if blocker_txt
                            else "Not armed — prior gates incomplete"
                        )
                        value = "waiting"
                    results.append(
                        GateResult(
                            name="execution",
                            passed=prior_ok,
                            value=value,
                            detail=detail,
                        )
                    )
                else:
                    results.append(
                        GateResult(name=name, passed=False, detail="unknown gate")
                    )
            except Exception as e:
                detail = f"{type(e).__name__}: {e}"
                log_engine(f"gate {name} error — WAIT: {detail}")
                results.append(
                    GateResult(name=name, passed=False, value=None, detail=detail)
                )
        return results

    def _gate_session_open(self) -> GateResult:
        from system.market_data_hub import get_market_data_hub

        at = quote_time(self._clock())
        phase = self._session.snapshot().phase
        hub_maint = get_market_data_hub().is_in_maintenance(self._epic)
        open_now = bool(self._session.is_session_open(at=at))
        blocked, mins_left = self._session.is_entry_blocked_near_session_end(at=at)
        detail = "market closed"
        if blocked and open_now:
            detail = f"entry blocked — session ends in {mins_left}min"
            return GateResult(
                name="session_open",
                passed=False,
                value={"open": True, "entry_blocked": True, "mins_left": mins_left},
                detail=detail,
            )
        if hub_maint:
            detail = "Japan 225 maintenance — stream paused until prices resume"
            open_now = False
        elif phase == "MAINTENANCE":
            detail = "Daily maintenance ~22:00 BST — session resumes when IG reopens"
            open_now = False
        elif open_now:
            detail = "market open"
            try:
                from system.market_watch.japan225_session import (
                    japan225_strategy_paused,
                )

                paused, pause_msg = japan225_strategy_paused(self._epic)
                if paused:
                    open_now = False
                    detail = pause_msg or "Japan 225 strategy paused"
            except Exception:
                pass
            # Also enforce per-instrument trading session whitelist at gate level
            if open_now:
                try:
                    from signals.indicators import session_name
                    from trading.instrument_registry import InstrumentRegistry

                    wl = InstrumentRegistry(
                        self._config.as_dict()
                    ).session_whitelist_for_epic(self._epic)
                    if not wl:
                        wl = list(self._config.trading_session_whitelist)
                    if wl:
                        sess = session_name()
                        if sess not in wl:
                            open_now = False
                            detail = f"Outside allowed trading session (current={sess})"
                except Exception:
                    pass
        next_open_iso = ""
        if not open_now:
            try:
                from system.market_watch.calendar import get_market_status

                ms = get_market_status(self._epic)
                if ms and ms.next_open_at:
                    # Market physically closed — use calendar next open
                    next_open_iso = ms.next_open_at.isoformat()
                elif ms and ms.open:
                    # Market is physically open but blocked by session whitelist.
                    # Find when the next whitelisted strategy session starts.
                    from datetime import timedelta

                    from signals.indicators import session_name as _sess_name
                    from trading.instrument_registry import InstrumentRegistry

                    wl = InstrumentRegistry(
                        self._config.as_dict()
                    ).session_whitelist_for_epic(self._epic)
                    if not wl:
                        wl = list(self._config.trading_session_whitelist)
                    if wl:
                        now_local = datetime.now()
                        for offset_min in range(15, 25 * 60, 15):
                            probe = now_local + timedelta(minutes=offset_min)
                            if _sess_name(probe) in wl:
                                next_open_iso = probe.replace(
                                    minute=0, second=0, microsecond=0
                                ).isoformat()
                                break
            except Exception:
                pass
        return GateResult(
            name="session_open",
            passed=open_now,
            value={"open": open_now, "next_open": next_open_iso},
            detail=detail,
        )

    def _gate_cold_start_gap(self, quote: Quote) -> GateResult:
        from trading.session_manager import GAP_CLEAR_BARS

        cold = bool(self._session.is_cold_start())
        atr = self._atr_estimate(quote)
        # Use uncapped elapsed bars so the expiry at GAP_CLEAR_BARS can actually fire.
        # bars_since_open() is intentionally capped at COLD_START_BARS(6) for cold-start
        # detection only; gap expiry needs the true elapsed count.
        bars_cold = self._session.bars_since_open()
        bars_elapsed = self._session.elapsed_bars_since_open()
        gap = bool(self._session.check_gap_open(atr, open_price=float(quote.mid)))
        # Track wall-clock age of gap independently of bar counting.
        # Protects against mid-session restarts where _open_time is reset to restart time.
        if gap:
            if self._gap_first_seen_at is None:
                self._gap_first_seen_at = datetime.now()
        else:
            self._gap_first_seen_at = None
        # Gap block expires after GAP_CLEAR_BARS bars (1 hour) — market has had time to settle.
        # Wall-clock fallback: if gap has been visible for ≥60 min, clear regardless of bar count.
        gap_age_sec = (
            (datetime.now() - self._gap_first_seen_at).total_seconds()
            if self._gap_first_seen_at
            else 0
        )
        if gap and (
            bars_elapsed >= GAP_CLEAR_BARS or gap_age_sec >= GAP_CLEAR_BARS * 5 * 60
        ):
            gap = False
        passed = (not cold) and (not gap)
        if cold:
            detail = f"cold start — {bars_cold}/6 bars"
        elif gap:
            remaining = max(0, GAP_CLEAR_BARS - bars_elapsed)
            detail = f"gap open >1.0× ATR (clears in ~{remaining * 5}min)"
        else:
            detail = "cold start and gap OK"
        return GateResult(
            name="cold_start_gap",
            passed=passed,
            value={"cold": cold, "gap": gap, "bars": bars_elapsed},
            detail=detail,
        )

    def _fitness_factors_payload(self) -> dict[str, Any]:
        """Decomposed environment fitness for dashboard /state (atr/trend/session/spread)."""
        try:
            raw = self._env.get_factors()
            last = self._env.last_score()
            sentiment = raw.get("sentiment")
            if not isinstance(sentiment, dict):
                sentiment = self._env.get_sentiment_factor(self._market)
            return {
                "atr": round(float(raw.get("atr", 0)), 2),
                "trend": round(float(raw.get("trend", 0)), 2),
                "session": round(float(raw.get("session", 0)), 2),
                "spread": round(float(raw.get("spread", 0)), 2),
                "sentiment_adjustment": round(float(raw.get("sentiment_adj", 0)), 2),
                "max": {
                    "atr": FACTOR_ATR_MAX,
                    "trend": FACTOR_TREND_MAX,
                    "session": FACTOR_SESSION_MAX,
                    "spread": FACTOR_SPREAD_MAX,
                },
                "total": round(float(last.total), 1),
                "gate_min": GATE_PASS_MIN,
                "capped_cold_start": bool(last.capped_cold_start),
                "capped_gap_open": bool(last.capped_gap_open),
                "sentiment": sentiment,
            }
        except Exception:
            return {}

    def _gate_environment_fitness(self, quote: Quote) -> GateResult:
        try:
            quote_df = self._signal_engine.quote_df(self._market)
            score = float(self._env.score(self._market, quote=quote, quote_df=quote_df))
        except Exception as e:
            log_engine(
                f"environment_fitness gate: score failed for {self._market}: "
                f"{type(e).__name__}: {e}"
            )
            score = float(SAFE_DEFAULT_SCORE)
        score_int = int(round(score))
        passed = score >= GATE_PASS_MIN
        sent = {}
        if hasattr(self._env, "get_sentiment_factor"):
            try:
                sent = self._env.get_sentiment_factor(self._market)
            except Exception:
                sent = {}
        sent_label = str(sent.get("label") or "")
        detail = f"fitness {score_int}% (need >={int(GATE_PASS_MIN)}%)"
        if sent_label and sent_label != "neutral":
            detail += f" — {sent_label}"
        factors_payload = self._fitness_factors_payload()
        return GateResult(
            name="environment_fitness",
            passed=passed,
            value={
                "score": score_int,
                "display": f"{score_int}%",
                "sentiment": sent,
                "factors": factors_payload,
            },
            detail=detail,
        )

    def _maybe_consume_points_skip_on_suppressed_signal(
        self, gates: list[GateResult]
    ) -> None:
        """After 3 losses, burn one skip slot per actionable signal while paused."""
        if not self._points.is_session_paused():
            return
        points_gate = next((g for g in gates if g.name == "points_state"), None)
        sig_gate = next((g for g in gates if g.name == "signal_confidence"), None)
        if points_gate is None or points_gate.passed:
            return
        if sig_gate is None or not sig_gate.passed:
            return
        if self._points.consume_signal_skip():
            remaining = self._points.session_skips_remaining()
            log_engine(
                f"points session pause: consumed skip slot ({remaining} remaining)"
            )

    def _gate_points_state(self) -> GateResult:
        state = self._points.get_state()
        paused = self._points.is_session_paused()
        day_stopped = False  # disabled: max_daily_loss_gbp is the hard stop
        loss_gbp = self._daily_loss_gbp()
        daily_limit = self._config.max_daily_loss_gbp
        passed = state != "STOP" and not paused and loss_gbp < daily_limit
        if state == "STOP":
            detail = "points state STOP"
        elif paused:
            n = self._points.session_skips_remaining()
            detail = (
                f"session pause — skip {n} actionable signal(s) "
                f"(BUY/SELL that would have fired)"
            )
        elif loss_gbp >= daily_limit:
            detail = f"daily loss £{loss_gbp:.2f} >= £{daily_limit:.0f}"
            if not getattr(self, "_daily_loss_alert_sent", False):
                self._daily_loss_alert_sent = True
                try:
                    from system.telegram_notifier import send_critical_alert

                    send_critical_alert("🛑 Drawdown limit hit — trading halted")
                except Exception as e:
                    log_engine(
                        f"telegram daily-loss alert failed: {type(e).__name__}: {e}"
                    )
        else:
            detail = f"points {state}"
        return GateResult(
            name="points_state",
            passed=passed,
            value=state,
            detail=detail,
        )

    def _maybe_refresh_account_balance(self) -> None:
        client = self._rest_client()
        if client is None:
            return
        try:
            if self._balance_refresher is None:
                from system.account_balance_refresh import AccountBalanceRefresher

                self._balance_refresher = AccountBalanceRefresher(
                    client,
                    open_count_fn=self._ig_open_position_count,
                )
            refresher = self._balance_refresher
            # Reuse a single worker thread instead of creating one per tick.
            # Creating a new thread every 5s × 6 markets = 72 threads/min; at
            # multi-hour runtimes this hits the OS thread limit.
            worker = getattr(self, "_balance_refresh_worker", None)
            if worker is not None and worker.is_alive():
                return  # previous refresh still in progress — skip
            t = threading.Thread(
                target=refresher.maybe_refresh,
                daemon=True,
                name=f"account-balance-refresh-{self._epic[-8:]}",
            )
            self._balance_refresh_worker = t
            t.start()
        except Exception:
            pass

    def _dynamic_max_per_epic(
        self, base_cap: int, open_count: int, tracker: Any
    ) -> tuple[int, str]:
        from trading.position_ladder import dynamic_max_per_epic

        return dynamic_max_per_epic(
            epic=self._epic,
            base_cap=base_cap,
            open_count=open_count,
            points_state=self._points.get_state(),
            tracker=tracker,
        )

    def _gate_risk_validation(self, quote: Quote) -> GateResult:
        from execution.market_suspension import gate_detail, is_blocked
        from system.market_data_hub import get_market_data_hub

        if is_blocked():
            detail = gate_detail() or "Market suspended"
            return GateResult(
                name="risk_validation",
                passed=False,
                value={"market_suspended": True},
                detail=detail,
            )

        spread = max(0.0, float(quote.offer) - float(quote.bid))
        cfg_normal = float(self._config.max_spread_points)
        normal = get_market_data_hub().normal_spread(self._epic, fallback=cfg_normal)
        spread_cap = normal * SPREAD_NORMAL_MULTIPLIER
        spread_ok = spread <= spread_cap if normal > 0 else True

        tracker = self._execution_loop.execution_engine.trade_tracker
        open_count = int(tracker.count_open_for_epic(self._epic))
        base_cap = max(1, int(self._config.max_positions_per_epic))
        max_per_epic, dynamic_unlock_reason = self._dynamic_max_per_epic(
            base_cap, open_count, tracker
        )
        try:
            max_open_total = max(1, int(self._config.max_open_positions))
        except (TypeError, ValueError):
            max_open_total = max_per_epic
        total_raw = tracker.count_open_total()
        if isinstance(total_raw, (int, float)):
            open_total = max(open_count, int(total_raw))
        else:
            open_total = open_count
        epic_slot_ok = open_count < max_per_epic
        total_slot_ok = open_total < max_open_total
        position_ok = epic_slot_ok and total_slot_ok

        stop = float(self._config.stop_distance_points)
        base_size = float(self._config.trade_size)
        point_value = float(self._config.get("ig_point_value_gbp", 1.0))
        # Plan with points-tier minimum size (CAUTION → 0.5× at 80%+).
        from trading.points_engine import CONF_MARGINAL_MIN

        planning_conf = max(
            CONF_MARGINAL_MIN,
            float(self._points.trade_confidence_threshold(self._config)),
        )
        size_mult = float(self._points.get_size_multiplier(planning_conf))
        effective_size = max(
            float(self._config.adaptive_min_trade_size),
            min(
                float(self._config.adaptive_max_trade_size),
                base_size * size_mult,
            ),
        )
        constraints = self._fetch_market_constraints()
        ig_min_raw = constraints.get("min_deal_size", effective_size)
        try:
            ig_min_size = float(ig_min_raw)
        except (TypeError, ValueError):
            ig_min_size = effective_size
        actual_size = max(effective_size, ig_min_size)
        cap_raw = self._config.get("risk_cap_gbp")
        try:
            risk_cap = float(cap_raw) if cap_raw is not None else STAGE1_GBP_RISK_CAP
        except (TypeError, ValueError):
            risk_cap = STAGE1_GBP_RISK_CAP

        # Auto-clip size to risk cap rather than hard-blocking the trade.
        size_was_clipped = False
        if point_value > 0 and stop > 0 and risk_cap > 0:
            max_size_by_risk = risk_cap / (stop * point_value)
            if actual_size > max_size_by_risk:
                increment = ig_min_size if ig_min_size > 0 else 0.01
                clipped = math.floor(max_size_by_risk / increment) * increment
                if clipped >= ig_min_size:
                    actual_size = clipped
                    size_was_clipped = True
                # else: even min size exceeds cap — leave actual_size as-is so the
                # risk check below fires with a clear log message.

        risk_gbp = stop * actual_size * point_value
        risk_ok = risk_gbp <= risk_cap

        portfolio_ok = True
        portfolio_detail = ""
        try:
            from system.portfolio_envelope import can_allocate, portfolio_gate_enabled

            if portfolio_gate_enabled():
                portfolio_ok, portfolio_detail = can_allocate(risk_gbp)
        except Exception:
            portfolio_ok = True

        passed = spread_ok and position_ok and risk_ok and portfolio_ok
        if not spread_ok:
            detail = (
                f"spread {spread:.1f} > {spread_cap:.1f} "
                f"(1.5× normal {normal:.1f}, cfg {cfg_normal:.1f})"
            )
        elif not epic_slot_ok:
            detail = (
                f"open positions {open_count} (max {max_per_epic} per epic"
                + (
                    f", unlocked: {dynamic_unlock_reason}"
                    if max_per_epic > base_cap
                    else ""
                )
                + ")"
            )
        elif not total_slot_ok:
            detail = f"total open positions {open_total} (max {max_open_total})"
        elif not risk_ok:
            detail = (
                f"risk £{risk_gbp:.2f} > £{risk_cap:.0f} cap "
                f"(stop {stop:.1f} × size {actual_size:.2g} × £/pt {point_value:.2f}"
                f"{', IG min' if actual_size > effective_size else ''})"
            )
        elif not portfolio_ok:
            detail = f"portfolio envelope: {portfolio_detail}"
        else:
            clip_note = f", clipped to {actual_size:.3g}" if size_was_clipped else ""
            detail = (
                f"OK — spread {spread:.1f} pts (normal {normal:.1f}, max {spread_cap:.1f}), "
                f"flat, risk £{risk_gbp:.0f} (cap £{risk_cap:.0f}){clip_note}"
            )
        return GateResult(
            name="risk_validation",
            passed=passed,
            value={
                "spread": round(spread, 1),
                "spread_normal": round(normal, 1),
                "spread_config": round(cfg_normal, 1),
                "open_count": open_count,
                "open_total": open_total,
                "max_per_epic": max_per_epic,
                "max_per_epic_base": base_cap,
                "dynamic_unlock_reason": dynamic_unlock_reason,
                "max_open_total": max_open_total,
                "risk_gbp": round(risk_gbp, 2),
                "base_size": round(base_size, 3),
                "effective_size": round(effective_size, 3),
                "actual_size": round(actual_size, 3),
                "size_clipped_to_risk_cap": size_was_clipped,
                "ig_min_deal_size": round(ig_min_size, 3),
                "size_multiplier": round(size_mult, 3),
                "stop_points": round(stop, 1),
                "point_value_gbp": round(point_value, 3),
                "spread_cap": round(spread_cap, 1),
                "risk_cap_gbp": risk_cap,
                "points_state": self._points.get_state(),
            },
            detail=detail,
        )

    def _gate_calendar_ok(self) -> GateResult:
        from system.calendar_gate import is_calendar_blocked
        from system.v26_config import calendar_settings

        cfg = calendar_settings()
        if not cfg.get("enabled"):
            return GateResult(
                name="calendar_ok",
                passed=True,
                value="off",
                detail="calendar guard disabled (config_v26.json)",
            )
        blocked, reason = is_calendar_blocked(str(getattr(self, "_epic", "") or ""))
        return GateResult(
            name="calendar_ok",
            passed=not blocked,
            value={"blocked": blocked},
            detail=reason if blocked else "no high-impact event window",
        )

    def _gate_expectancy_ok(self) -> GateResult:
        from system.setup_registry import (
            is_gate_enabled,
            is_setup_banned,
            setup_status,
        )

        if not is_gate_enabled():
            return GateResult(
                name="expectancy_ok",
                passed=True,
                value="off",
                detail="setup registry inactive (no banned setups)",
            )
        sig = self._signal_engine.evaluate(self._market)
        setup_key = str(sig.setup_key or "")
        if not setup_key:
            return GateResult(
                name="expectancy_ok",
                passed=True,
                value="—",
                detail="no setup key yet",
            )
        status = setup_status(setup_key)
        banned = is_setup_banned(setup_key)
        passed = not banned
        detail = (
            f"setup BANNED (14d E£/WR): {setup_key[:56]}"
            if banned
            else f"setup {status}: {setup_key[:56]}"
        )
        return GateResult(
            name="expectancy_ok",
            passed=passed,
            value=status,
            detail=detail,
        )

    def _gate_signal_confidence(self) -> GateResult:
        sig = self._signal_engine.evaluate(self._market)
        threshold = float(self._points.trade_confidence_threshold(self._config))
        conf = float(sig.adjusted_confidence)
        rules_conf = conf
        ml_prob: float | None = None
        if bool(self._config.get("USE_ML_SIGNAL", False)):
            try:
                from trading.ml_scorer import get_ml_scorer

                scorer = get_ml_scorer()
                from data.ml_training_store import MLTrainingStore
                from system.paths import data_dir

                _ML_MIN_TRAINING_RECORDS = 500

                def _ml_training_rows() -> int:
                    live = MLTrainingStore().record_count()
                    meta_path = data_dir() / "ml_model" / "training_meta.json"
                    try:
                        if meta_path.is_file():
                            import json as _json

                            meta = _json.loads(meta_path.read_text(encoding="utf-8"))
                            replay = int(meta.get("labelled_rows") or 0)
                            return max(live, replay)
                    except Exception:
                        pass
                    return live

                _ml_records = _ml_training_rows()
                if scorer.is_trained() and _ml_records >= _ML_MIN_TRAINING_RECORDS:
                    snap = sig.snapshot or {}
                    last = snap.get("last")
                    _last = last if (last is not None and hasattr(last, "get")) else {}
                    _atr = float(_last.get("atr", 0) or 0)
                    # Normalise ATR by configured stop distance so it is dimensionless
                    # and comparable across instruments (Wall St ~80pt stop vs Gold
                    # ~10pt stop vs FX sub-pip stop).
                    _stop = max(1.0, float(self._config.stop_distance_points))
                    # Keys must exactly match the model's training feature names
                    features = {
                        "adjusted_score": rules_conf,
                        "raw_score": float(snap.get("raw_confidence", rules_conf)),
                        "rsi": float(_last.get("rsi", 0) or 0),
                        "atr_ratio": _atr / _stop,
                    }
                    # Only blend if all model features are present
                    if all(f in features for f in scorer.feature_names):
                        ml_prob = scorer.score(
                            features,
                            use_ml_signal=True,
                            timeout_s=0.5,
                        )
                        if ml_prob > 0.0:
                            # Only blend when the model has meaningful conviction
                            # (≥15% deviation from 50%). Near-50% means the model
                            # is out-of-distribution or has no signal — don't let it
                            # veto a strong rules score.
                            _ML_CONVICTION = 0.15
                            blended = False
                            if abs(ml_prob - 0.5) >= _ML_CONVICTION:
                                conf = (rules_conf * 0.6) + (ml_prob * 100.0 * 0.4)
                                conf = max(0.0, min(100.0, conf))
                                blended = True
                                log_engine(
                                    f"ML score {ml_prob:.3f} rules {rules_conf:.1f} blended {conf:.1f}"
                                )
                            else:
                                log_engine(
                                    f"ML score {ml_prob:.3f} near-50% (no conviction) — using rules {rules_conf:.1f}"
                                )
                            # Record for the dashboard ML decision log
                            entry = {
                                "ts": datetime.now().strftime("%H:%M:%S"),
                                "market": self._market,
                                "direction": sig.signal,
                                "ml_prob": round(float(ml_prob), 3),
                                "rules_conf": round(rules_conf, 1),
                                "confidence": round(conf, 1),
                                "blended": blended,
                                "blend_note": (
                                    f"→ blended {conf:.1f}%"
                                    if blended
                                    else "near-50%, rules used"
                                ),
                                "setup": sig.setup_key,
                            }
                            self._ml_decision_log.append(entry)
                            if len(self._ml_decision_log) > 20:
                                self._ml_decision_log = self._ml_decision_log[-20:]
                elif scorer.is_trained():
                    log_engine(
                        f"ML blend skipped: {_ml_records} training records "
                        f"(need {_ML_MIN_TRAINING_RECORDS})"
                    )
            except Exception as e:
                log_engine(f"ML gate blend skipped: {type(e).__name__}: {e}")
        self._last_ml_prob = ml_prob
        self._last_sig_direction = str(sig.signal or "WAIT")
        passed = sig.signal in ("BUY", "SELL") and conf >= threshold
        detail, block_reason = signal_gate_explanation(sig, threshold)
        pts_state = self._points.get_state()
        if pts_state == "WARNING" and threshold >= 90.0:
            detail = f"{detail} (points {pts_state} — need >={threshold:.0f}%)"
        snap = sig.snapshot or {}
        return GateResult(
            name="signal_confidence",
            passed=passed,
            value={
                "signal": sig,
                "direction": sig.signal,
                "raw_direction": snap.get("raw_signal"),
                "confidence": conf,
                "rules_confidence": rules_conf,
                "ml_probability": ml_prob,
                "threshold": threshold,
                "config_signal_threshold": float(self._config.signal_threshold),
                "points_confidence_floor": float(self._points.get_threshold()),
                "min_size_threshold": float(
                    self._points.min_size_confidence_threshold()
                ),
                "points_state": self._points.get_state(),
                "block_reason": block_reason,
                "setup": sig.setup_key,
            },
            detail=detail,
        )

    def _gate_ml_veto(self) -> GateResult:
        from system.v26_config import (
            epic_min_probability,
            epic_ml_veto_enabled,
            ml_veto_settings,
        )

        cfg = ml_veto_settings()
        if not cfg.get("enabled"):
            return GateResult(
                name="ml_veto",
                passed=True,
                value="off",
                detail="ml_veto disabled (config_v26.json)",
            )
        epic = str(getattr(self, "_epic", "") or "")
        if epic and not epic_ml_veto_enabled(epic):
            return GateResult(
                name="ml_veto",
                passed=True,
                value="epic_off",
                detail=f"ml_veto off for {epic}",
            )
        direction = self._last_sig_direction
        if direction not in ("BUY", "SELL"):
            return GateResult(
                name="ml_veto",
                passed=True,
                value="WAIT",
                detail="no directional signal",
            )
        ml_prob = self._last_ml_prob
        ml_source = "v25_blend"
        if cfg.get("use_s4_models"):
            try:
                from trading.v26_ml_scorer import get_v26_ml_scorer

                v26 = get_v26_ml_scorer()
                if epic and v26.is_eligible(epic):
                    sig = self._signal_engine.evaluate(self._market)
                    snap = sig.snapshot or {}
                    _last_raw = snap.get("last")
                    last = (
                        _last_raw
                        if (_last_raw is not None and hasattr(_last_raw, "get"))
                        else {}
                    )
                    stop = max(1.0, float(self._config.stop_distance_points))
                    atr = float(last.get("atr", 0) or 0)
                    feats = {
                        "adjusted_score": float(sig.adjusted_confidence),
                        "rsi": float(last.get("rsi", 0) or 0),
                        "atr_ratio": atr / stop,
                    }
                    s4_prob = v26.score(epic, feats, timeout_s=0.5)
                    if s4_prob is not None:
                        ml_prob = s4_prob
                        ml_source = "s4_v26"
            except Exception as e:
                log_engine(f"ml_veto S4 scorer skipped: {type(e).__name__}: {e}")
        if ml_prob is None:
            return GateResult(
                name="ml_veto",
                passed=True,
                value=None,
                detail="ML unavailable — veto skipped",
            )
        min_p = (
            epic_min_probability(epic)
            if epic
            else float(cfg.get("min_probability") or 0.58)
        )
        passed = float(ml_prob) >= min_p
        return GateResult(
            name="ml_veto",
            passed=passed,
            value={
                "ml_probability": ml_prob,
                "min_probability": min_p,
                "source": ml_source,
            },
            detail=(
                f"{ml_source} prob {ml_prob:.3f} ≥ {min_p:.3f}"
                if passed
                else f"{ml_source} prob {ml_prob:.3f} < {min_p:.3f} (veto)"
            ),
        )

    def _daily_loss_gbp(self) -> float:
        try:
            if self._store is None:
                return 0.0
            pnl = float(self._store.sum_daily_pnl())
            return max(0.0, -pnl)
        except Exception:
            return 0.0

    def _atr_estimate(self, quote: Quote) -> float:
        try:
            row = getattr(self._signal_engine, "last_row", None)
            if callable(row):
                r = row(self._market, 15)
                if r is not None:
                    return float(r.get("atr", 0) or 0)
            df = self._signal_engine.quote_df(self._market)
            if df is not None and len(df) > 0:
                return float(df.iloc[-1].get("atr", 0) or 0)
        except Exception:
            pass
        return 0.0

    def _flatten_if_needed(self) -> None:
        at = quote_time(self._clock())
        try:
            if not self._session.should_run_flatten_attempt(at=at):
                return
        except Exception as e:
            log_engine(f"flatten attempt check failed: {type(e).__name__}: {e}")
            return
        threshold = self._session.mark_flatten_attempt(at=at)
        log_engine(
            f"session flatten — closing all open positions (T-{int(threshold or 0)}min)"
        )
        try:
            n = self._execute_flatten_close()
            log_engine(f"flatten close sent — {n} position(s)")
        except Exception as e:
            log_engine(f"flatten close failed: {type(e).__name__}: {e}")
            self._session.record_flatten_failure()
            if self._session.flatten_failures() >= 3:
                self._flatten_failed_critical()
            return
        self._verify_flatten_after_close(at)

    def _verify_flatten_after_close(self, at: datetime) -> None:
        time.sleep(FLATTEN_VERIFY_WAIT_SEC)
        sync = getattr(self, "_position_sync", None)
        if sync is not None and hasattr(sync, "sync_once"):
            try:
                sync.sync_once()
            except Exception as e:
                log_engine(
                    f"ig_position_sync verify sync failed: {type(e).__name__}: {e}"
                )
        open_count = self._ig_open_position_count()
        if open_count <= 0:
            log_engine("FLATTEN CONFIRMED — all positions closed")
            self._session.flatten_confirmed()
            self._write_session_summary_if_needed(at)
            return
        failures = self._session.record_flatten_failure()
        log_engine(
            f"flatten verify failed — {open_count} position(s) still open "
            f"(failure {failures}/3)"
        )
        if failures >= 3:
            self._flatten_failed_critical()

    def _write_session_summary_if_needed(self, at: datetime) -> None:
        try:
            from data.ml_training_store import MLTrainingStore

            ml = self._ml_store
            if ml is None:
                ml = MLTrainingStore()
            write_session_end_summary(
                session=self._session,
                store=self._store,
                points=self._points,
                tracker=self._session_tracker,
                close_at=at,
                ml_store=ml,
            )
        except Exception as e:
            log_engine(f"session_summary failed: {type(e).__name__}: {e}")

    def _flatten_failed_critical(self) -> None:
        log_engine("CRITICAL: FLATTEN FAILED — manual intervention required")
        self._trigger_emergency_stop()

    def _trigger_emergency_stop(self) -> None:
        script = project_root() / "scripts" / "emergency_stop.sh"
        if not script.is_file():
            log_engine(f"emergency_stop.sh not found at {script}")
            return
        try:
            subprocess.Popen(
                ["bash", str(script)],
                cwd=str(project_root()),
                start_new_session=True,
            )
            log_engine("emergency_stop.sh triggered")
        except Exception as e:
            log_engine(f"emergency_stop.sh launch failed: {type(e).__name__}: {e}")

    def _ig_open_position_count(self) -> int:
        sync = getattr(self, "_position_sync", None)
        if sync is not None:
            try:
                if hasattr(sync, "count_for_epic"):
                    return int(sync.count_for_epic(self._epic))
                return int(sync.total_open())
            except Exception:
                pass
        engine = self._execution_loop.execution_engine
        tracker = getattr(engine, "trade_tracker", None)
        if tracker is not None and hasattr(tracker, "count_open_for_epic"):
            try:
                return int(tracker.count_open_for_epic(self._epic))
            except Exception:
                pass
        store = getattr(engine, "store", None) or self._store
        if store is not None and hasattr(store, "count_open_trades"):
            try:
                return int(store.count_open_trades(self._epic))
            except Exception:
                pass
        log_engine(
            f"WARN: open position count unknown for {self._epic} — "
            "sync/tracker/store unavailable"
        )
        return -1

    def _execute_flatten_close(self) -> int:
        if self._on_flatten is not None:
            return int(self._on_flatten())
        return self._default_flatten()

    def _default_flatten(self) -> int:
        engine = self._execution_loop.execution_engine
        store = getattr(engine, "store", None) or self._store
        rest = getattr(engine, "_rest_client", None)
        if store is None or rest is None:
            return 0
        closed = 0
        if not hasattr(store, "active_trades"):
            log_engine("flatten: LearningStore.active_trades unavailable")
            return 0
        rows = store.active_trades()
        for row in rows:
            deal_id = str(row["ig_deal_id"] or "")
            if not deal_id:
                continue
            side = str(row["side"] or "BUY").upper()
            size = float(row["size"] or 0)
            epic = str(row["epic"] or self._epic)
            close_dir = "SELL" if side == "BUY" else "BUY"
            rest.close_position(
                deal_id,
                direction=close_dir,
                size=size,
                epic=epic,
                currency_code=self._config.currency_code,
                verify=True,
            )
            closed += 1
        return closed

    def _publish_snapshot(self, ctx: TickContext) -> None:
        try:
            payload = self._build_snapshot_payload(ctx)
            if self._on_snapshot is not None:
                self._on_snapshot(payload)
            elif self._publish_snapshots:
                publish_tick(payload)
        except Exception as e:
            log_engine(f"publish_tick failed: {type(e).__name__}: {e}")

    def build_snapshot_payload(self, ctx: TickContext | None = None) -> dict[str, Any]:
        """Build dashboard tick payload (orchestrator merge / tests)."""
        target = ctx if ctx is not None else self.last_context
        if target is None:
            return {}
        return self._build_snapshot_payload(target)

    def _snapshot_maintenance_flags(self) -> tuple[bool, bool]:
        hub_maint = False
        session_maint = False
        try:
            from system.market_data_hub import get_market_data_hub

            hub_maint = get_market_data_hub().is_in_maintenance(self._epic)
            session_maint = self._session.snapshot().phase == "MAINTENANCE"
        except Exception:
            pass
        return hub_maint, session_maint

    def _snapshot_stream_status(
        self,
        *,
        spread: float,
        hub_maint: bool,
        session_maint: bool,
        quote_ts: datetime,
        tick_age_s: float,
    ) -> tuple[str, float]:
        stream_status = "DISCONNECTED"
        if hub_maint or session_maint:
            stream_status = "MAINTENANCE"
        elif spread > 0:
            try:
                from system.market_data_hub import get_market_data_hub
                from system.stream_ready import is_stream_ready

                snap = get_market_data_hub().get_snapshot(self._epic)
                cap_raw = self._config.get("stale_threshold_seconds")
                try:
                    stale_after = (
                        float(cap_raw)
                        if cap_raw is not None
                        else float(self._config.refresh_seconds) * 2.0
                    )
                except (TypeError, ValueError):
                    stale_after = float(self._config.refresh_seconds) * 2.0
                if is_stream_ready():
                    stale_after = max(stale_after, 60.0)
                if snap and snap.age_seconds() <= stale_after:
                    stream_status = "LIVE"
                else:
                    stream_status = "STALE"
            except Exception:
                stream_status = "LIVE"
        return stream_status, tick_age_s

    def _build_snapshot_payload(self, ctx: TickContext) -> dict[str, Any]:
        quote = ctx.quote
        spread = max(0.0, float(quote.offer) - float(quote.bid))
        gates_payload = [
            {
                "name": g.name,
                "pass": g.passed,
                "value": _json_safe(g.value),
                "detail": g.detail,
            }
            for g in ctx.gates
        ]
        passing = sum(1 for g in ctx.gates if g.passed)
        total = len(ctx.gates) or len(GATE_NAMES)
        sig = ctx.signal
        if sig is None:
            for g in ctx.gates:
                if g.name == "signal_confidence" and isinstance(g.value, dict):
                    sig = g.value.get("signal")
                    break
        direction = "WAIT"
        confidence = 0.0
        setup = ""
        atr = 0.0
        block_reason = ""
        raw_direction = ""
        signal_threshold = float(self._points.trade_confidence_threshold(self._config))
        if isinstance(sig, SignalResult):
            direction = str(sig.signal or "WAIT")
            confidence = float(sig.adjusted_confidence)
            # Prefer the ML-blended confidence when available (gate already computed it)
            for _g in ctx.gates:
                if _g.name == "signal_confidence" and isinstance(_g.value, dict):
                    _blended = _g.value.get("confidence")
                    if _blended is not None:
                        confidence = float(_blended)
                    break
            setup = str(sig.setup_key or "")
            snap = sig.snapshot or {}
            raw_direction = str(snap.get("raw_signal") or "")
            atr = _atr_from_signal_snapshot(snap)
            _, block_reason = signal_gate_explanation(sig, signal_threshold)
        else:
            for g in ctx.gates:
                if g.name == "signal_confidence" and isinstance(g.value, dict):
                    block_reason = str(g.value.get("block_reason") or "")
                    raw_direction = str(g.value.get("raw_direction") or "")
                    signal_threshold = float(
                        g.value.get("threshold") or signal_threshold
                    )
                    break

        points_state = self._points.get_state()
        ps = self._points.snapshot()
        open_positions = self._positions_payload(quote)

        session_open = False
        for g in ctx.gates:
            if g.name == "session_open":
                session_open = bool(g.passed)
                break

        hub_maint, session_maint = self._snapshot_maintenance_flags()

        if hub_maint or session_maint:
            market_state = "MAINTENANCE"
        elif not session_open:
            market_state = "CLOSED"
        elif spread <= 0:
            market_state = "OFFLINE"
        else:
            market_state = "OPEN"

        badge = "BLOCKED"
        if not session_open:
            badge = "WATCHING"
        elif ctx.all_passed:
            badge = "READY"

        readiness = compute_trade_readiness(ctx.gates)
        badge_text = format_health_badge_text(badge, readiness)

        quote_ts = quote.time if isinstance(quote.time, datetime) else self._clock()
        tick_age_s = max(0.0, (self._clock() - quote_ts).total_seconds())
        if hub_maint or session_maint:
            try:
                from system.market_data_hub import get_market_data_hub

                snap = get_market_data_hub().get_snapshot(self._epic)
                if snap and snap.bid > 0:
                    tick_age_s = max(tick_age_s, snap.age_seconds())
            except Exception:
                pass

        stream_status, tick_age_s = self._snapshot_stream_status(
            spread=spread,
            hub_maint=hub_maint,
            session_maint=session_maint,
            quote_ts=quote_ts if isinstance(quote_ts, datetime) else self._clock(),
            tick_age_s=tick_age_s,
        )

        if stream_status == "STALE" and tick_age_s > 60.0:
            try:
                from system.telegram_notifier import get_telegram_notifier

                notifier = get_telegram_notifier()
                if notifier is not None:
                    notifier.notify_stream_stale(self._epic, tick_age_s)
            except Exception:
                pass
        elif stream_status == "LIVE":
            try:
                from system.telegram_notifier import get_telegram_notifier

                notifier = get_telegram_notifier()
                if notifier is not None:
                    notifier.clear_stream_stale(self._epic)
            except Exception:
                pass

        eligibility = build_trade_eligibility(
            gates=ctx.gates,
            session=self._session,
            signal_engine=self._signal_engine,
            market=self._market,
            epic=self._epic,
            block_reason=block_reason,
            sig=sig if isinstance(sig, SignalResult) else None,
            now=quote_time(self._clock()),
            quote_ts=quote_ts if isinstance(quote_ts, datetime) else None,
        )
        countdown = eligibility.to_dict() if eligibility else None

        price_trend = self._price_trend_payload(quote_ts)

        if self._session.is_session_open():
            self._session_tracker.record_tick(
                block_reason=block_reason or ctx.wait_reason or None,
                stream_live=stream_status == "LIVE",
            )

        watchdog_banner = None
        try:
            from system.watchdog_banner import banner_active, banner_message

            if banner_active():
                watchdog_banner = banner_message()
        except Exception:
            pass

        spread_stats: dict[str, float] = {}
        try:
            from system.market_data_hub import get_market_data_hub

            spread_stats = get_market_data_hub().spread_stats(
                self._epic, fallback=float(self._config.max_spread_points)
            )
        except Exception:
            pass

        sentiment_factor: dict[str, Any] = {}
        try:
            sentiment_factor = self._env.get_sentiment_factor(self._market)
        except Exception:
            pass

        return {
            "type": "tick",
            "epic": self._epic,
            "market": self._market,
            "instrument_id": self._instrument_id or None,
            "ts": _iso_ts(quote_ts),
            "watchdog_failed": watchdog_banner,
            "market_state": market_state,
            "bid": float(quote.bid) if quote.bid else None,
            "offer": float(quote.offer) if quote.offer else None,
            "spread": spread if spread > 0 else None,
            "spread_normal": spread_stats.get("normal"),
            "spread_current": spread_stats.get("current"),
            "sentiment": sentiment_factor,
            "tick_age_s": round(tick_age_s, 1),
            "stream_status": stream_status,
            "rest_calls_min": self._rest_calls_last_minute(),
            "errors": self._errors_snapshot(),
            "health": {
                "badge": badge,
                "badge_text": badge_text,
                "readiness": readiness,
                "gates": gates_payload,
                "summary": f"{passing} of {total} gates passing"
                + (f" — {ctx.wait_reason}" if ctx.wait_reason else ""),
            },
            "signal": {
                "direction": direction,
                "raw_direction": raw_direction or None,
                "confidence": int(round(confidence)),  # ML-blended if available
                "rules_confidence": int(round(float(sig.adjusted_confidence)))
                if isinstance(sig, SignalResult)
                else 0,
                "threshold": int(round(signal_threshold)),
                "config_signal_threshold": int(
                    round(float(self._config.signal_threshold))
                ),
                "points_confidence_floor": int(
                    round(float(self._points.get_threshold()))
                ),
                "threshold_delta": int(
                    round(confidence - float(self._points.get_threshold()))
                ),
                "min_size_threshold": int(
                    round(float(self._points.min_size_confidence_threshold()))
                ),
                "points_state": points_state,
                "block_reason": block_reason or None,
                "fitness": int(round(ctx.fitness)),
                "fitness_threshold": int(round(GATE_PASS_MIN)),
                "fitness_factors": self._fitness_factors_payload(),
                "atr": round(atr, 1) if atr else 0.0,
                "atr_threshold": (
                    round(float(self._config.min_atr_points), 1)
                    if float(self._config.min_atr_points) > 0
                    else None
                ),
                "setup": setup,
                "countdown": countdown,
                "price_trend": price_trend,
            },
            "price_trend": price_trend,
            "trade_eligibility": countdown,
            "points": {
                "state": points_state,
                "cumulative": float(ps.cumulative),
                "session": float(ps.session_score),
                "last_trade": float(ps.last_trade_score),
                "size_multiplier": float(self._points.get_size_multiplier(confidence)),
            },
            "positions": open_positions,
            "daily_pnl_gbp": self._daily_pnl_signed_gbp(open_positions),
            "balance_gbp": self._balance_gbp(),
            "win_rate_20": self._win_rate_20_pct(),
            "max_open_positions": int(self._config.max_open_positions),
            "max_positions_per_epic": int(self._config.max_positions_per_epic),
            "ml_training_records": self._ml_training_record_count(),
            "confirmed_trades": int(self._store.count_closed_trades() or 0)
            if self._store
            else 0,
            "ml_enabled": bool(self._config._data.get("USE_ML_SIGNAL", False)),
            "ml_decision_log": list(reversed(self._ml_decision_log)),
            "closed_trades": self._closed_trades_payload(),
            "recent_trades": self._recent_trades_results(),
            "pnl_history": self._pnl_history_payload(),
            "drawdown": self._drawdown_snapshot(),
        }

    def _price_trend_payload(self, quote_ts: datetime) -> dict[str, Any] | None:
        try:
            df = self._signal_engine.quote_df(self._market)
        except Exception:
            return None
        if df is None:
            return None
        now = quote_ts if isinstance(quote_ts, datetime) else self._clock()
        try:
            return compute_price_trend_30m(df, now=now)
        except Exception:
            return None

    def _rest_calls_last_minute(self) -> int:
        try:
            from system.rest_api_budget import get_rest_api_budget

            return get_rest_api_budget().calls_last_minute()
        except Exception:
            return 0

    def _rest_client(self) -> Any | None:
        try:
            return self._execution_loop.execution_engine._rest_client  # noqa: SLF001
        except Exception:
            return None

    def _fetch_market_constraints(self) -> dict[str, Any]:
        """IG dealing rules for this epic — returned from session-level background cache.

        The REST call to /markets/{epic} can hang if IG's API is slow.  We fetch
        once in a daemon thread at loop start and return the result; subsequent
        calls return the same cached dict.  The tick thread is never blocked.
        """
        if self._market_constraints_fetched:
            return self._market_constraints_cache

        # Trigger background fetch on first tick (non-blocking for the caller).
        self._market_constraints_fetched = True  # prevent re-spawning

        def _bg_fetch() -> None:
            client = self._rest_client()
            if client is None or not hasattr(client, "fetch_market_constraints"):
                return
            try:
                result = client.fetch_market_constraints(self._epic)
                if isinstance(result, dict):
                    self._market_constraints_cache = result
            except Exception:
                pass

        threading.Thread(
            target=_bg_fetch, daemon=True, name=f"market-constraints-{self._epic[-8:]}"
        ).start()
        return (
            self._market_constraints_cache
        )  # returns {} until background fetch completes

    def _account_summary(self) -> dict[str, float | None]:
        client = self._rest_client()
        if client is None:
            return {}
        try:
            if hasattr(client, "maybe_refresh_account_summary"):
                return client.maybe_refresh_account_summary(min_interval=60.0)
            if hasattr(client, "get_cached_account_summary"):
                return client.get_cached_account_summary()
        except Exception:
            pass
        return {}

    def _balance_gbp(self) -> float | None:
        client = self._rest_client()
        if client is None:
            return None
        try:
            if hasattr(client, "get_cached_account_summary"):
                bal = client.get_cached_account_summary().get("balance")
            else:
                bal = None
        except Exception:
            return None
        if bal is None:
            return None
        try:
            return float(bal)
        except (TypeError, ValueError):
            return None

    def _win_rate_20_pct(self) -> int | None:
        if self._store is None or not hasattr(self._store, "recent_closed_trades"):
            return None
        try:
            from system.closed_trades_display import is_excluded_display_row

            rows = self._store.recent_closed_trades(40)
            closed: list[dict[str, Any]] = []
            for row in rows:
                if is_excluded_display_row(row):
                    continue
                closed.append(row)
                if len(closed) >= 20:
                    break
            if not closed:
                return None
            wins = 0
            for row in closed:
                result = str(row.get("result") or "").upper()
                if not result:
                    pnl = row.get("ig_pnl_currency")
                    if pnl is None:
                        pnl = row.get("pnl_points")
                    try:
                        pnl_f = float(pnl)
                        result = (
                            "WIN" if pnl_f > 0 else "LOSS" if pnl_f < 0 else "BREAKEVEN"
                        )
                    except (TypeError, ValueError):
                        result = ""
                if result == "WIN":
                    wins += 1
            return int(round((wins / len(closed)) * 100))
        except Exception:
            return None

    def _ml_training_record_count(self) -> int | None:
        try:
            if self._ml_store is not None:
                return self._ml_store.record_count()
            from data.ml_training_store import MLTrainingStore

            return MLTrainingStore().record_count()
        except Exception:
            return None

    def _closed_trades_payload(self) -> list[dict[str, Any]]:
        try:
            if self._store is None or not hasattr(self._store, "recent_closed_trades"):
                return []
            from system.closed_trades_display import (
                deduplicate_ig_imports,
                is_excluded_display_row,
            )

            rows = self._store.recent_closed_trades(limit=100)
            filtered = [r for r in rows if not is_excluded_display_row(r)]
            deduped = deduplicate_ig_imports(filtered)
            deduped.sort(key=lambda r: str(r.get("closed_at") or ""), reverse=True)
            out: list[dict[str, Any]] = []
            for row in deduped:
                pnl_gbp = row.get("ig_pnl_currency")
                pnl_pts = float(row.get("pnl_points") or 0)
                if pnl_gbp is not None:
                    pnl_gbp = float(pnl_gbp)
                if row.get("closed_at") is None:
                    result = "OPEN"
                elif pnl_gbp is None:
                    result = "PENDING"
                elif pnl_gbp > 0:
                    result = "WIN"
                elif pnl_gbp < 0:
                    result = "LOSS"
                else:
                    result = "BREAKEVEN"
                out.append(
                    {
                        "deal_id": row.get("deal_id") or row.get("ig_deal_id"),
                        "market": row.get("market") or row.get("epic"),
                        "epic": row.get("epic"),
                        "side": row.get("side") or row.get("direction"),
                        "direction": row.get("side") or row.get("direction"),
                        "entry_price": row.get("entry_price") or row.get("entry"),
                        "entry": row.get("entry_price") or row.get("entry"),
                        "exit_price": row.get("exit_price") or row.get("exit"),
                        "exit": row.get("exit_price") or row.get("exit"),
                        "pnl_gbp": pnl_gbp,
                        "pnl": pnl_gbp,
                        "pnl_pts": pnl_pts,
                        "result": result,
                        "closed_at": row.get("closed_at"),
                        "time": row.get("closed_at"),
                        "setup": row.get("setup_key"),
                        "confidence": row.get("confidence"),
                        "source": row.get("source"),
                    }
                )
                if len(out) >= 50:
                    break
            return out
        except Exception:
            return []

    def _recent_trades_results(self) -> list[dict[str, Any]]:
        try:
            if self._store is None or not hasattr(self._store, "recent_closed_trades"):
                return []
            from system.closed_trades_display import is_excluded_display_row

            rows = self._store.recent_closed_trades(50)
            out: list[dict[str, Any]] = []
            for row in rows:
                if is_excluded_display_row(row):
                    continue
                pnl_gbp = row.get("ig_pnl_currency")
                if pnl_gbp is not None:
                    result = "WIN" if float(pnl_gbp) > 0 else "LOSS"
                else:
                    pnl_pts = float(row.get("pnl_points") or 0)
                    result = "WIN" if pnl_pts > 0 else "LOSS"
                out.append({"result": result})
                if len(out) >= 20:
                    break
            return out
        except Exception:
            return []

    def _pnl_history_payload(self) -> list[dict[str, Any]]:
        try:
            if self._store is None or not hasattr(self._store, "recent_closed_trades"):
                return []
            from system.closed_trades_display import is_excluded_display_row

            rows = self._store.recent_closed_trades(100)
            rows_sorted = sorted(
                (
                    r
                    for r in rows
                    if r.get("closed_at") and not is_excluded_display_row(r)
                ),
                key=lambda r: str(r.get("closed_at") or ""),
            )
            cumulative = 0.0
            points: list[dict[str, Any]] = []
            for row in rows_sorted:
                pnl = row.get("ig_pnl_currency")
                if pnl is None:
                    continue
                cumulative += float(pnl)
                points.append(
                    {"time": str(row["closed_at"]), "value": round(cumulative, 2)}
                )
            return points
        except Exception:
            return []

    def _errors_snapshot(self) -> dict[str, Any]:
        try:
            from system.engine_log import get_engine_alerts_snapshot

            return get_engine_alerts_snapshot()
        except Exception:
            return {"count": 0, "type": None}

    def _drawdown_snapshot(self) -> dict[str, float]:
        try:
            from system.drawdown_monitor import snapshot as _dd_snap

            return _dd_snap()
        except Exception:
            return {}

    def _daily_pnl_signed_gbp(self, open_positions: list[Any] | None = None) -> float:
        journal = 0.0
        if self._store is not None:
            try:
                journal = float(self._store.sum_daily_pnl())
            except Exception:
                journal = 0.0
        has_open = bool(open_positions)
        if has_open or journal != 0.0:
            return journal
        ig_pl = self._account_summary().get("profit_loss")
        if ig_pl is not None:
            try:
                return float(ig_pl)
            except (TypeError, ValueError):
                pass
        return journal

    def _positions_payload(self, quote: Quote | None = None) -> list[dict[str, Any]]:
        point_value = float(self._config.get("ig_point_value_gbp", 1.0))
        raw: list[dict[str, Any]] = []

        def _append_raw(pos: dict[str, Any]) -> None:
            if not isinstance(pos, dict):
                return
            pos_epic = str(pos.get("epic") or "")
            if self._epic and pos_epic and pos_epic != self._epic:
                return
            raw.append(normalize_sync_position(pos))

        try:
            snap = self._execution_loop.execution_engine.trade_tracker.snapshot()
            for pos in snap.get("positions") or []:
                _append_raw(pos)
        except Exception:
            pass
        if not raw:
            sync = getattr(self, "_position_sync", None)
            if sync is not None and hasattr(sync, "snapshot_dict"):
                try:
                    for pos in sync.snapshot_dict().get("positions") or []:
                        _append_raw(pos)
                except Exception:
                    pass
        if not raw and self._store is not None:
            try:
                rows = self._store.active_trades(self._epic)
                raw = positions_from_store_rows(
                    rows, quote, point_value_gbp=point_value
                )
            except Exception:
                pass
        return enrich_positions_with_quote(
            raw, quote, point_value_gbp=point_value, epic=self._epic
        )


def quote_time(clock: datetime | Callable[[], datetime]) -> datetime:
    return clock() if callable(clock) else clock


def _json_safe(value: Any) -> Any:
    if isinstance(value, SignalResult):
        snap = value.snapshot or {}
        return {
            "signal": value.signal,
            "raw_direction": snap.get("raw_signal"),
            "confidence": value.adjusted_confidence,
            "setup": value.setup_key,
        }
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _iso_ts(when: datetime) -> str:
    # astimezone() on naive datetime assumes local system tz (BST in summer) → converts to UTC.
    # astimezone() on aware datetime converts from its tz to UTC. Both paths produce correct UTC.
    when = when.astimezone(timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
