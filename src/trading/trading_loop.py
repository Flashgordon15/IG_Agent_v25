"""
v25 agent orchestration loop — 5s tick, 7 gates, snapshot IPC (Section 4.5 Step 9).

Owns gate evaluation order and calls execution.trading_loop.TradingLoop.process_tick
for gate 7 only. No GUI imports. Trading continues if the FastAPI dashboard fails.
"""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from api.snapshot import GATE_NAMES
from api.snapshot_store import publish_tick
from data.models import Quote
from execution.trading_loop import TickOutcome, TradingLoop as ExecutionTickLoop
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
from trading.open_position_view import (
    enrich_positions_with_quote,
    normalize_sync_position,
    positions_from_store_rows,
)
from trading.points_engine import PointsEngine
from trading.session_manager import SessionManager
from trading.price_trend import compute_price_trend_30m
from trading.gate_readiness import compute_trade_readiness, format_health_badge_text
from trading.session_summary import SessionTickTracker, write_session_end_summary
from trading.trade_eligibility import build_trade_eligibility

STAGE1_GBP_RISK_CAP = 150.0
SPREAD_NORMAL_MULTIPLIER = 1.5
DAILY_LOSS_LIMIT_GBP = 200.0
STAGE1_MAX_OPEN_POSITIONS = 1
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
        self._balance_refresher: Any | None = None

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
            self._running = True
            self._thread = threading.Thread(
                target=self._loop_thread,
                name="ig-agent-trading-loop",
                daemon=True,
            )
            self._thread.start()
        log_engine("trading_loop started")

    def stop(self) -> None:
        self._stop.set()
        thread = None
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=self._tick_interval + 2.0)
        with self._lock:
            self._running = False
            self._thread = None
        log_engine("trading_loop stopped")

    def run_once(self) -> TickContext | None:
        """Run a single tick synchronously (tests)."""
        return self._run_tick()

    def _loop_thread(self) -> None:
        from system.stream_ready import wait_stream_ready

        wait_stream_ready(timeout=120.0)
        try:
            while not self._stop.is_set():
                try:
                    self._run_tick()
                except Exception as e:
                    self._session_tracker.record_error()
                    log_engine(
                        f"trading_loop tick error (continuing): "
                        f"{type(e).__name__}: {e}"
                    )
                if self._stop.wait(self._tick_interval):
                    break
        finally:
            with self._lock:
                self._running = False

    def _run_tick(self) -> TickContext | None:
        quote = self._quote_source()
        if quote is None:
            ctx = TickContext(
                quote=Quote(self._clock(), 0.0, 0.0),
                wait_reason="no quote",
            )
            ctx.gates = self._offline_gates(ctx.wait_reason)
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
            try:
                outcome = self._execution_loop.process_tick(
                    self._market, self._epic, quote
                )
            except Exception as e:
                log_engine(
                    f"gate 7 execution failed: {type(e).__name__}: {e}"
                )
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

    def _offline_gates(self, reason: str) -> list[GateResult]:
        gates: list[GateResult] = []
        for name in GATE_NAMES:
            gates.append(
                GateResult(name=name, passed=False, value=None, detail=reason)
            )
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
                elif name == "signal_confidence":
                    results.append(self._gate_signal_confidence())
                elif name == "execution":
                    prior_ok = bool(results) and all(r.passed for r in results)
                    if prior_ok:
                        detail = "Ready — order path armed (process_tick on this tick)"
                        value = "armed"
                    else:
                        blockers = [
                            r.name.replace("_", " ")
                            for r in results
                            if not r.passed
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
        elif phase == "MAINTENANCE":
            detail = "Daily maintenance ~22:00 BST — session resumes when IG reopens"
        elif open_now:
            detail = "market open"
        else:
            detail = "market closed"
        return GateResult(
            name="session_open",
            passed=open_now,
            value=open_now,
            detail=detail,
        )

    def _gate_cold_start_gap(self, quote: Quote) -> GateResult:
        cold = bool(self._session.is_cold_start())
        atr = self._atr_estimate(quote)
        gap = bool(
            self._session.check_gap_open(atr, open_price=float(quote.mid))
        )
        passed = (not cold) and (not gap)
        if cold:
            detail = f"cold start — {self._session.bars_since_open()}/6 bars"
        elif gap:
            detail = "gap open >1.0× ATR"
        else:
            detail = "cold start and gap OK"
        return GateResult(
            name="cold_start_gap",
            passed=passed,
            value={"cold": cold, "gap": gap, "bars": self._session.bars_since_open()},
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
            score = float(
                self._env.score(self._market, quote=quote, quote_df=quote_df)
            )
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
                "points session pause: consumed skip slot "
                f"({remaining} remaining)"
            )

    def _gate_points_state(self) -> GateResult:
        state = self._points.get_state()
        paused = self._points.is_session_paused()
        day_stopped = self._points.is_day_stopped()
        loss_gbp = self._daily_loss_gbp()
        passed = (
            state != "STOP"
            and not paused
            and not day_stopped
            and loss_gbp < DAILY_LOSS_LIMIT_GBP
        )
        if state == "STOP":
            detail = "points state STOP"
        elif day_stopped:
            detail = "day stop active"
        elif paused:
            n = self._points.session_skips_remaining()
            detail = (
                f"session pause — skip {n} actionable signal(s) "
                f"(BUY/SELL that would have fired)"
            )
        elif loss_gbp >= DAILY_LOSS_LIMIT_GBP:
            detail = f"daily loss £{loss_gbp:.2f} >= £{DAILY_LOSS_LIMIT_GBP:.0f}"
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
            self._balance_refresher.maybe_refresh()
        except Exception:
            pass

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
        normal = get_market_data_hub().normal_spread(
            self._epic, fallback=cfg_normal
        )
        spread_cap = normal * SPREAD_NORMAL_MULTIPLIER
        spread_ok = spread <= spread_cap if normal > 0 else True

        tracker = self._execution_loop.execution_engine.trade_tracker
        open_count = int(tracker.count_open_for_epic(self._epic))
        max_per_epic = max(1, int(self._config.max_positions_per_epic))
        position_ok = open_count < max_per_epic

        stop = float(self._config.stop_distance_points)
        base_size = float(self._config.trade_size)
        point_value = float(self._config.get("ig_point_value_gbp", 1.0))
        # Plan with points-tier minimum size (CAUTION → 0.25× at 80%+, 0.5× at 88%+).
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
        risk_gbp = stop * actual_size * point_value
        cap_raw = self._config.get("risk_cap_gbp")
        try:
            risk_cap = (
                float(cap_raw) if cap_raw is not None else STAGE1_GBP_RISK_CAP
            )
        except (TypeError, ValueError):
            risk_cap = STAGE1_GBP_RISK_CAP
        risk_ok = risk_gbp <= risk_cap

        passed = spread_ok and position_ok and risk_ok
        if not spread_ok:
            detail = (
                f"spread {spread:.1f} > {spread_cap:.1f} "
                f"(1.5× normal {normal:.1f}, cfg {cfg_normal:.1f})"
            )
        elif not position_ok:
            detail = f"open positions {open_count} (max {max_per_epic})"
        elif not risk_ok:
            detail = (
                f"risk £{risk_gbp:.2f} > £{risk_cap:.0f} cap "
                f"(stop {stop:.1f} × size {actual_size:.2g} × £/pt {point_value:.2f}"
                f"{', IG min' if actual_size > effective_size else ''})"
            )
        else:
            detail = (
                f"OK — spread {spread:.1f} pts (normal {normal:.1f}, max {spread_cap:.1f}), "
                f"flat, risk £{risk_gbp:.0f} (cap £{risk_cap:.0f})"
            )
        return GateResult(
            name="risk_validation",
            passed=passed,
            value={
                "spread": round(spread, 1),
                "spread_normal": round(normal, 1),
                "spread_config": round(cfg_normal, 1),
                "open_count": open_count,
                "risk_gbp": round(risk_gbp, 2),
                "base_size": round(base_size, 3),
                "effective_size": round(effective_size, 3),
                "actual_size": round(actual_size, 3),
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
                if scorer.is_trained():
                    snap = sig.snapshot or {}
                    last = snap.get("last")
                    features = {
                        "confidence": rules_conf,
                        "rsi": float(last.get("rsi", 0)) if last is not None and hasattr(last, "get") else 0.0,
                        "atr": float(last.get("atr", 0)) if last is not None and hasattr(last, "get") else 0.0,
                        "spread": float(last.get("spread", 0)) if last is not None and hasattr(last, "get") else 0.0,
                        "fitness_score": float(snap.get("fitness_score", 0) or 0),
                        "session_window": str(snap.get("session") or "unknown"),
                        "volume_regime": str(snap.get("vol_regime") or "unknown"),
                        "trend_bias": "mixed",
                    }
                    ml_prob = scorer.predict(features)
                    conf = (rules_conf * 0.6) + (ml_prob * 100.0 * 0.4)
                    log_engine(
                        f"ML score {ml_prob:.3f} rules {rules_conf:.1f} blended {conf:.1f}"
                    )
            except Exception as e:
                log_engine(f"ML gate blend skipped: {type(e).__name__}: {e}")
        passed = sig.signal in ("BUY", "SELL") and conf >= threshold
        detail, block_reason = signal_gate_explanation(sig, threshold)
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
            f"session flatten — closing all open positions "
            f"(T-{int(threshold or 0)}min)"
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
            log_engine(
                f"emergency_stop.sh launch failed: {type(e).__name__}: {e}"
            )

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
        return 0

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
        rows = store.conn.execute(
            "SELECT id, epic, side, size, deal_id FROM trades WHERE status='OPEN'"
        ).fetchall()
        for row in rows:
            deal_id = str(row["deal_id"] or "")
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
                    signal_threshold = float(g.value.get("threshold") or signal_threshold)
                    break

        points_state = self._points.get_state()
        ps = self._points.snapshot()
        open_positions = self._positions_payload(quote)

        session_open = False
        for g in ctx.gates:
            if g.name == "session_open":
                session_open = bool(g.passed)
                break

        hub_maint = False
        session_maint = False
        try:
            from system.market_data_hub import get_market_data_hub

            hub_maint = get_market_data_hub().is_in_maintenance(self._epic)
            session_maint = self._session.snapshot().phase == "MAINTENANCE"
        except Exception:
            pass

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
            "rest_calls_min": 0,
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
                "confidence": int(round(confidence)),
                "threshold": int(round(signal_threshold)),
                "config_signal_threshold": int(round(float(self._config.signal_threshold))),
                "points_confidence_floor": int(round(float(self._points.get_threshold()))),
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

    def _rest_client(self) -> Any | None:
        try:
            return self._execution_loop.execution_engine._rest_client  # noqa: SLF001
        except Exception:
            return None

    def _fetch_market_constraints(self) -> dict[str, Any]:
        """IG dealing rules for this epic (cached on REST client when available)."""
        client = self._rest_client()
        if client is None or not hasattr(client, "fetch_market_constraints"):
            return {}
        try:
            return client.fetch_market_constraints(self._epic)
        except Exception:
            return {}

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
        bal = self._account_summary().get("balance")
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
                        result = "WIN" if pnl_f > 0 else "LOSS" if pnl_f < 0 else "BREAKEVEN"
                    except (TypeError, ValueError):
                        result = ""
                if result == "WIN":
                    wins += 1
            return int(round((wins / len(closed)) * 100))
        except Exception:
            return None

    def _errors_snapshot(self) -> dict[str, Any]:
        try:
            from system.engine_log import get_engine_alerts_snapshot

            return get_engine_alerts_snapshot()
        except Exception:
            return {"count": 0, "type": None}

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
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    else:
        when = when.astimezone(timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
