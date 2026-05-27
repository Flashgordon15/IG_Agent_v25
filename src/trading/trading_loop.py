"""
v25 agent orchestration loop — 5s tick, 7 gates, snapshot IPC (Section 4.5 Step 9).

Owns gate evaluation order and calls execution.trading_loop.TradingLoop.process_tick
for gate 7 only. No GUI imports. Trading continues if the FastAPI dashboard fails.
"""

from __future__ import annotations

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
from trading.environment_scorer import GATE_PASS_MIN, EnvironmentScorer
from trading.points_engine import PointsEngine
from trading.session_manager import SessionManager

STAGE1_GBP_RISK_CAP = 50.0
SPREAD_NORMAL_MULTIPLIER = 1.5
DAILY_LOSS_LIMIT_GBP = 200.0
STAGE1_MAX_OPEN_POSITIONS = 1
DEFAULT_TICK_INTERVAL_SEC = 5.0


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
        clock: Callable[[], datetime] | None = None,
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
        self._clock = clock or datetime.now

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()
        self._last_context: TickContext | None = None
        self._tick_count = 0

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
        try:
            while not self._stop.is_set():
                try:
                    self._run_tick()
                except Exception as e:
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
            self._signal_engine.add_quote(self._market, quote)
        except Exception as e:
            log_engine(f"signal_engine.add_quote failed: {type(e).__name__}: {e}")
        try:
            self._session.on_tick(quote)
        except Exception as e:
            log_engine(f"session_manager.on_tick failed: {type(e).__name__}: {e}")

        self._flatten_if_needed()

        gates = self._evaluate_gates(quote)
        all_passed = all(g.passed for g in gates)
        wait_reason = ""
        if not all_passed:
            failed = next(g for g in gates if not g.passed)
            wait_reason = f"{failed.name}: {failed.detail}"

        signal: SignalResult | None = None
        fitness = 0.0
        for g in gates:
            if g.name == "environment_fitness":
                fitness = float(g.value or 0.0)
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
                    results.append(
                        GateResult(
                            name="execution",
                            passed=results and all(r.passed for r in results),
                            value="armed",
                            detail="Async order path via process_tick",
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
        open_now = bool(self._session.is_session_open(at=quote_time(self._clock())))
        detail = "market open" if open_now else "market closed"
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

    def _gate_environment_fitness(self, quote: Quote) -> GateResult:
        score = float(self._env.score(self._market, quote=quote))
        passed = score >= GATE_PASS_MIN
        return GateResult(
            name="environment_fitness",
            passed=passed,
            value=score,
            detail=f"fitness {score:.0f} (need >={GATE_PASS_MIN:.0f})",
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
            detail = "session pause active"
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

    def _gate_risk_validation(self, quote: Quote) -> GateResult:
        spread = max(0.0, float(quote.offer) - float(quote.bid))
        normal = float(self._config.max_spread_points)
        spread_cap = normal * SPREAD_NORMAL_MULTIPLIER
        spread_ok = spread <= spread_cap if normal > 0 else True

        tracker = self._execution_loop.execution_engine.trade_tracker
        open_count = int(tracker.count_open_for_epic(self._epic))
        position_ok = open_count < STAGE1_MAX_OPEN_POSITIONS

        stop = float(self._config.stop_distance_points)
        size = float(self._config.trade_size)
        point_value = float(self._config.get("ig_point_value_gbp", 1.0))
        risk_gbp = stop * size * point_value
        risk_ok = risk_gbp <= STAGE1_GBP_RISK_CAP

        passed = spread_ok and position_ok and risk_ok
        if not spread_ok:
            detail = f"spread {spread:.1f} > {spread_cap:.1f} (1.5× normal)"
        elif not position_ok:
            detail = f"open positions {open_count} (max {STAGE1_MAX_OPEN_POSITIONS - 1})"
        elif not risk_ok:
            detail = f"risk £{risk_gbp:.2f} > £{STAGE1_GBP_RISK_CAP:.0f} cap"
        else:
            detail = "spread, size, and position OK"
        return GateResult(
            name="risk_validation",
            passed=passed,
            value={
                "spread": spread,
                "open_count": open_count,
                "risk_gbp": risk_gbp,
            },
            detail=detail,
        )

    def _gate_signal_confidence(self) -> GateResult:
        sig = self._signal_engine.evaluate(self._market)
        threshold = float(self._points.get_threshold())
        conf = float(sig.adjusted_confidence)
        passed = sig.signal in ("BUY", "SELL") and conf >= threshold
        if sig.signal not in ("BUY", "SELL"):
            detail = f"signal {sig.signal} — no trade"
        else:
            detail = f"conf {conf:.1f}% (need >={threshold:.1f}%)"
        return GateResult(
            name="signal_confidence",
            passed=passed,
            value={"signal": sig, "confidence": conf, "threshold": threshold},
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
        try:
            if not self._session.should_flatten(at=quote_time(self._clock())):
                return
        except Exception as e:
            log_engine(f"should_flatten check failed: {type(e).__name__}: {e}")
            return
        log_engine("session flatten window — closing all open positions")
        try:
            if self._on_flatten is not None:
                n = int(self._on_flatten())
            else:
                n = self._default_flatten()
            log_engine(f"flatten complete — closed {n} position(s)")
        except Exception as e:
            log_engine(f"flatten failed: {type(e).__name__}: {e}")

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
            publish_tick(self._build_snapshot_payload(ctx))
        except Exception as e:
            log_engine(f"publish_tick failed: {type(e).__name__}: {e}")

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
        if isinstance(sig, SignalResult):
            direction = str(sig.signal or "WAIT")
            confidence = float(sig.adjusted_confidence)
            setup = str(sig.setup_key or "")
            try:
                atr = float((sig.snapshot or {}).get("atr", 0) or 0)
            except Exception:
                atr = 0.0

        points_state = self._points.get_state()
        ps = self._points.snapshot()
        open_positions = self._positions_payload()

        session_open = False
        for g in ctx.gates:
            if g.name == "session_open":
                session_open = bool(g.passed)
                break

        if not session_open:
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

        quote_ts = quote.time if isinstance(quote.time, datetime) else self._clock()
        tick_age_s = max(0.0, (self._clock() - quote_ts).total_seconds())

        stream_status = "DISCONNECTED"
        if spread > 0:
            try:
                from system.market_data_hub import get_market_data_hub

                snap = get_market_data_hub().get_snapshot(self._epic)
                stale_after = float(self._config.refresh_seconds) * 2.0
                if snap and snap.age_seconds() <= stale_after:
                    stream_status = "LIVE"
                else:
                    stream_status = "STALE"
            except Exception:
                stream_status = "LIVE"

        return {
            "type": "tick",
            "ts": _iso_ts(quote_ts),
            "market_state": market_state,
            "bid": float(quote.bid) if quote.bid else None,
            "offer": float(quote.offer) if quote.offer else None,
            "spread": spread if spread > 0 else None,
            "tick_age_s": round(tick_age_s, 1),
            "stream_status": stream_status,
            "rest_calls_min": 0,
            "errors": {"count": 0, "type": None},
            "health": {
                "badge": badge,
                "gates": gates_payload,
                "summary": f"{passing} of {total} gates passing"
                + (f" — {ctx.wait_reason}" if ctx.wait_reason else ""),
            },
            "signal": {
                "direction": direction,
                "confidence": confidence,
                "fitness": int(round(ctx.fitness)),
                "atr": atr,
                "setup": setup,
            },
            "points": {
                "state": points_state,
                "cumulative": float(ps.cumulative),
                "session": float(ps.session_score),
                "last_trade": float(ps.last_trade_score),
                "size_multiplier": float(self._points.get_size_multiplier(confidence)),
            },
            "positions": open_positions,
            "daily_pnl_gbp": self._daily_pnl_signed_gbp(),
            "balance_gbp": None,
            "win_rate_20": None,
        }

    def _daily_pnl_signed_gbp(self) -> float:
        try:
            if self._store is None:
                return 0.0
            return float(self._store.sum_daily_pnl())
        except Exception:
            return 0.0

    def _positions_payload(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        try:
            snap = self._execution_loop.execution_engine.trade_tracker.snapshot()
            for pos in snap.get("positions") or []:
                if not isinstance(pos, dict):
                    continue
                out.append(
                    {
                        "deal_id": pos.get("deal_id") or pos.get("dealId") or "",
                        "side": pos.get("side") or pos.get("direction") or "",
                        "entry": pos.get("entry") or pos.get("open_level"),
                        "current": pos.get("current") or pos.get("mid"),
                        "stop": pos.get("stop"),
                        "target": pos.get("target"),
                        "pnl_gbp": pos.get("pnl_gbp"),
                        "pnl_pts": pos.get("pnl_pts"),
                        "trail_active": bool(pos.get("trail_active", False)),
                        "breakeven_hit": bool(pos.get("breakeven_hit", False)),
                        "open_mins": pos.get("open_mins"),
                    }
                )
        except Exception:
            pass
        return out


def quote_time(clock: datetime | Callable[[], datetime]) -> datetime:
    return clock() if callable(clock) else clock


def _json_safe(value: Any) -> Any:
    if isinstance(value, SignalResult):
        return {
            "signal": value.signal,
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
