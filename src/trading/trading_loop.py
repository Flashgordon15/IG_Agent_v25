"""
v25 agent orchestration loop — 5s tick, 7 gates, snapshot IPC (Section 4.5 Step 9).

Owns gate evaluation order and calls execution.trading_loop.TradingLoop.process_tick
for gate 7 only. No GUI imports. Trading continues if the FastAPI dashboard fails.
"""

from __future__ import annotations

import math
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from system.guard.runtime_guard import guard_call, log_guarded_exception
from system.bare_metal_exec import bare_metal_hot_path_active


def _bare_metal_shadow_force_fill() -> bool:
    """E2E-only simulated fills — never on production integration paths."""
    return os.environ.get("IG_E2E_SHADOW_FORCE_FILL", "").strip() == "1"


_NIGHT_MATRIX_FORCE_GATE_EPICS: frozenset[str] = frozenset(
    {
        "CS.D.CFPGOLD.CFP.IP",  # Gold — 1 micro-lot, 10pt stop, 20pt limit
        "IX.D.DOW.IFM.IP",  # Wall St — canonical night-matrix dispatch epic
    }
)

_correlation_purge_lock = threading.Lock()


def force_reset_session_correlation_counters(
    *, reason: str = "trading_loop_coordinator"
) -> dict[str, Any]:
    """Purge session correlation BUY/SELL counters to 0/5 (thread-safe coordinator hook)."""
    from execution.correlation_guard import force_purge_session_correlation_counters
    from system.engine_log import log_engine

    with _correlation_purge_lock:
        snap = force_purge_session_correlation_counters(reason=reason)
    log_engine(
        f"trading_loop: correlation override applied ({reason}) "
        f"buy={snap.get('buy')} sell={snap.get('sell')}"
    )
    return snap


def _intercept_broker_connectivity_failure(exc: BaseException, *, subsystem: str) -> None:
    """Supervised network teardown — does not return on connectivity loss."""
    from system.guard.kernel_interceptor import dispatch_broker_connectivity_teardown

    dispatch_broker_connectivity_teardown(exc, source=subsystem)

from api.snapshot import GATE_NAMES
from api.snapshot_store import publish_tick
from data.models import Quote
from execution.spread_atr_circuit import (
    BLOCKED_SPREAD_TO_ATR_CIRCUIT_BREAKER,
    atr_from_signal_snapshot as _atr_from_signal_snapshot,
    spread_to_atr_circuit_max,
)
from execution.trading_loop import TickOutcome
from execution.trading_loop import TradingLoop as ExecutionTickLoop
from signals.signal_engine import (
    HIGH_CONFIDENCE_OVERRIDE_THRESHOLD,
    REQUIRE_CLOSED_BAR_G5,
    SignalResult,
)
from system.config import Config
from system.engine_log import log_engine
from system.paths import project_root
from trading.environment_scorer import (
    FACTOR_ATR_MAX,
    FACTOR_SESSION_MAX,
    FACTOR_SPREAD_MAX,
    FACTOR_TREND_MAX,
    SAFE_DEFAULT_SCORE,
    EnvironmentScorer,
)
from trading.gate_readiness import compute_trade_readiness, format_health_badge_text
from trading.open_position_view import (
    enrich_positions_with_quote,
    normalize_sync_position,
    position_map_from_rows,
    positions_from_store_rows,
    positions_list_from_map,
)
from trading.points_engine import PointsEngine
from trading.price_trend import compute_price_trend_30m
from trading.session_manager import SessionManager
from trading.session_summary import SessionTickTracker, write_session_end_summary
from trading.strictness_resolver import resolve_strictness
from trading.trade_eligibility import build_trade_eligibility

STAGE1_GBP_RISK_CAP = 150.0
SPREAD_NORMAL_MULTIPLIER = 2.5
OOH_SPREAD_SCALE = 4.0
OOH_SESSION_START_HOUR_BST = 21
OOH_SESSION_END_HOUR_BST = 7
DAILY_LOSS_LIMIT_GBP = 200.0
DEFAULT_TICK_INTERVAL_SEC = 5.0
GATE_EVAL_COOLDOWN_SEC = 10.0
_SPOT_GOLD_EPIC = "CS.D.CFPGOLD.CFP.IP"
_GBPUSD_FX_EPIC = "CS.D.GBPUSD.CFD.IP"
_USD_GBP_RATE_FALLBACK = 0.78
FLATTEN_VERIFY_WAIT_SEC = 10.0
# Bare-metal live execution fallback — merged config.signal_threshold takes precedence each tick.
LIVE_EXEC_SIGNAL_THRESHOLD = 45.0

# Friday session-validation capture — IG DEMO dispatch at 42% (env: IG_SESSION_VALIDATION=1).
SESSION_VALIDATION_CONFIDENCE_FLOOR = 42.0
# Production warmed-alpha floor — applied when v30 checkpoint is injected (Gate 5).
PRODUCTION_WARMED_CONFIDENCE_FLOOR = 54.5
_session_validation_logged = False
_warmed_alpha_boot_logged = False


def _production_runtime_mode_active() -> bool:
    try:
        from system.apex_runtime_mode import get_apex_runtime_mode

        return get_apex_runtime_mode().is_production
    except Exception:
        return os.environ.get("IG_APEX_RUNTIME_MODE", "").strip().upper() in (
            "PRODUCTION",
            "LIVE",
            "PROD",
        )


def _production_warmed_alpha_active() -> bool:
    if not _production_runtime_mode_active():
        return False
    try:
        from system.ml.cold_start_compiler import production_warmed_alpha_active

        return production_warmed_alpha_active()
    except Exception:
        return False


def _apply_production_warmed_confidence_floor(threshold: float) -> float:
    """Replace protective 62% cold-start floor with warmed 54.5% on production."""
    if not _production_warmed_alpha_active():
        return float(threshold)
    return min(float(threshold), PRODUCTION_WARMED_CONFIDENCE_FLOOR)


def _ensure_production_warmed_alpha_on_boot() -> None:
    global _warmed_alpha_boot_logged
    if not _production_runtime_mode_active():
        return
    try:
        from system.ml.cold_start_compiler import inject_warmed_alpha_weights

        applied = inject_warmed_alpha_weights()
        if applied and not _warmed_alpha_boot_logged:
            _warmed_alpha_boot_logged = True
            log_engine(
                f"TradingLoop: production warmed-alpha injected — "
                f"confidence floor {PRODUCTION_WARMED_CONFIDENCE_FLOOR:.1f}%"
            )
    except Exception as exc:
        log_guarded_exception("trading_loop_warmed_alpha_boot", exc)


def session_validation_capture_active() -> bool:
    from system.agent_execution_mode import demo_operational_floors_active

    return demo_operational_floors_active()


def _log_session_validation_floor_once() -> None:
    global _session_validation_logged
    if _session_validation_logged or not session_validation_capture_active():
        return
    _session_validation_logged = True
    log_engine(
        f"Session validation: confidence/fitness floors locked at "
        f"{SESSION_VALIDATION_CONFIDENCE_FLOOR:.0f}% — IG DEMO LiveExecutor armed"
    )


def _epic_requires_usd_gbp_risk_conversion(epic: str) -> bool:
    """Spot Gold and non-GBP IG index margin are USD-denominated at the broker."""
    key = str(epic or "").upper()
    if key == _SPOT_GOLD_EPIC or "CFPGOLD" in key:
        return True
    if key.startswith("IX.D.") and ".IFM." in key:
        return True
    return False


def _resolve_gate_eval_cooldown_sec() -> float:
    """Bounded gate-eval cooldown — safe default if config/state read fails."""
    try:
        from system.config_loader import get_config

        raw = getattr(get_config(), "gate_eval_cooldown_sec", None)
        if raw is None:
            return GATE_EVAL_COOLDOWN_SEC
        return max(1.0, min(60.0, float(raw)))
    except Exception:
        return GATE_EVAL_COOLDOWN_SEC


def _peak_confidence_from_signal(sig: SignalResult, conf: float) -> float:
    snap = sig.snapshot or {}
    raw_conf = float(snap.get("raw_confidence") or 0)
    try:
        buy = float(snap.get("buy_score")) if snap.get("buy_score") is not None else 0.0
    except (TypeError, ValueError):
        buy = 0.0
    try:
        sell = float(snap.get("sell_score")) if snap.get("sell_score") is not None else 0.0
    except (TypeError, ValueError):
        sell = 0.0
    return max(float(conf), raw_conf, buy, sell)


def _shield_integer_dispatch_size(
    raw_size: float, *, min_lot: int = 1
) -> tuple[int, bool]:
    """Project Apex Monolith Core Execution Shield — int(size // 1) with safe defaults."""
    calculated_size = 0.0
    final_size = 0
    under_min_lot = False
    try:
        from apex.hardening import floor_contract_size

        calculated_size = float(raw_size or 0)
        lot, under_min_lot = floor_contract_size(calculated_size, min_lot=min_lot)
        final_size = int(lot // 1)
    except Exception as exc:
        log_engine(f"[CORE ERROR] Order dispatcher exception caught: {exc}")
        return 0, True
    return final_size, under_min_lot


def promote_high_confidence_signal(
    sig: SignalResult, threshold: float, *, raw_size: float | None = None
) -> SignalResult:
    """
    Force dispatcher string to BUY/SELL when peak score clears override floor.

    Resolves gate passed=True but execution still sees WAIT deadlock.
    When raw_size is supplied, applies int(size // 1) whole-lot annotation.
    """
    import os
    import sys

    calculated_size = 0.0
    final_size = 0
    under_min_lot = False
    win_probability = 0.0
    model_verdict = "NEUTRAL"
    promoted = sig
    _ = os, sys  # containment — prevent UnboundLocalError from inner imports
    try:
        snap_in = sig.snapshot or {}
        try:
            win_probability = float(snap_in.get("win_probability") or 0.0)
        except (TypeError, ValueError):
            win_probability = 0.0
        model_verdict = str(snap_in.get("model_verdict") or model_verdict)
        if sig.signal in ("BUY", "SELL"):
            promoted = sig
        else:
            snap = sig.snapshot or {}
            raw = str(snap.get("raw_signal") or "").strip()
            if raw not in ("BUY", "SELL"):
                return sig
            peak = _peak_confidence_from_signal(sig, float(sig.adjusted_confidence))
            if peak < HIGH_CONFIDENCE_OVERRIDE_THRESHOLD or peak < float(threshold):
                return sig
            promoted_snap = dict(snap)
            promoted_snap["dispatch_promoted"] = True
            promoted_snap["raw_signal"] = raw
            promoted = SignalResult(
                signal=raw,
                raw_confidence=float(sig.raw_confidence),
                adjusted_confidence=max(float(sig.adjusted_confidence), peak),
                learning_delta=float(sig.learning_delta),
                setup_key=str(sig.setup_key),
                notes=f"{sig.notes} | dispatch promoted ({peak:.1f}%)",
                snapshot=promoted_snap,
            )
        if raw_size is not None:
            calculated_size = float(raw_size or 0)
            size_int, under_min = _shield_integer_dispatch_size(calculated_size)
            final_size = int(size_int // 1)
            under_min_lot = under_min
            snap_out = dict(promoted.snapshot or {})
            snap_out["dispatch_size_int"] = final_size
            snap_out["under_min_lot"] = under_min_lot
            snap_out["win_probability"] = win_probability
            snap_out["model_verdict"] = model_verdict
            promoted = SignalResult(
                signal=promoted.signal,
                raw_confidence=float(promoted.raw_confidence),
                adjusted_confidence=float(promoted.adjusted_confidence),
                learning_delta=float(promoted.learning_delta),
                setup_key=str(promoted.setup_key),
                notes=str(promoted.notes),
                snapshot=snap_out,
            )
        try:
            from system.agent_execution_mode import demo_broker_execution_active

            if demo_broker_execution_active() and promoted.signal in ("BUY", "SELL"):
                snap_demo = dict(promoted.snapshot or {})
                snap_demo["demo_live_gateway_armed"] = True
                promoted = SignalResult(
                    signal=promoted.signal,
                    raw_confidence=float(promoted.raw_confidence),
                    adjusted_confidence=float(promoted.adjusted_confidence),
                    learning_delta=float(promoted.learning_delta),
                    setup_key=str(promoted.setup_key),
                    notes=f"{promoted.notes} | DEMO→LiveExecutor.place_market_order",
                    snapshot=snap_demo,
                )
        except Exception as exc:
            _intercept_broker_connectivity_failure(exc, subsystem="trading_loop_order_dispatch")
            log_guarded_exception("trading_loop", exc)
    except Exception as exc:
        log_engine(f"[CORE ERROR] Order dispatcher exception caught: {exc}")
        return sig
    return promoted


def signal_gate_explanation(sig: SignalResult, threshold: float) -> tuple[str, str]:
    """Human-readable (gate_detail, block_reason) for dashboard / gates."""
    conf = float(sig.adjusted_confidence)
    snap = sig.snapshot or {}
    raw = str(snap.get("raw_signal") or "").strip()
    peak = _peak_confidence_from_signal(sig, conf)

    if peak >= HIGH_CONFIDENCE_OVERRIDE_THRESHOLD and raw in ("BUY", "SELL"):
        if peak >= float(threshold):
            return (
                f"PASS — {raw} {peak:.1f}% "
                f"(high-confidence override >= {threshold:.1f}%)",
                "",
            )

    if sig.signal in ("BUY", "SELL"):
        if conf < threshold:
            msg = f"{sig.signal} {conf:.1f}% below {threshold:.1f}% threshold"
            return msg, msg
        return f"{sig.signal} {conf:.1f}% (>= {threshold:.1f}%)", ""

    if snap.get("rsi_block") and peak < HIGH_CONFIDENCE_OVERRIDE_THRESHOLD:
        reason = str(snap["rsi_block"])
        lead = raw or "BUY/SELL"
        return f"WAIT — {reason} ({lead} score {conf:.1f}%)", reason

    if "blocked:" in sig.notes and peak < HIGH_CONFIDENCE_OVERRIDE_THRESHOLD:
        reason = sig.notes.split("blocked:", 1)[1].split(",", 1)[0].strip()
        return f"WAIT — {reason} ({conf:.1f}% score held)", reason

    notes_lower = (sig.notes or "").lower()
    if REQUIRE_CLOSED_BAR_G5 and "duplicate suppressed" in notes_lower:
        reason = "awaiting next closed 5m bar"
        return f"WAIT — {reason}", reason
    if "collecting live data" in notes_lower:
        reason = "collecting candle history"
        return f"WAIT — {reason}", reason

    for part in (sig.notes or "").split("|"):
        part = part.strip()
        if peak >= HIGH_CONFIDENCE_OVERRIDE_THRESHOLD:
            break
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
        if peak >= HIGH_CONFIDENCE_OVERRIDE_THRESHOLD and peak >= float(threshold):
            return (
                f"PASS — {raw} {peak:.1f}% "
                f"(high-confidence override >= {threshold:.1f}%)",
                "",
            )
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


NOT_IN_TOP_3_VOLATILITY_ROTATION = "NOT_IN_TOP_3_VOLATILITY_ROTATION"
SOFT_BLOCK_NOT_IN_TOP_3 = f"soft block — {NOT_IN_TOP_3_VOLATILITY_ROTATION}"
OFFLINE_BROKER_FEED_REJECTED = "OFFLINE_BROKER_FEED_REJECTED"


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
        paused_at_boot: bool = False,
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
        if clock is None:
            try:
                from system.apex_runtime_mode import ApexRuntimeMode, get_apex_runtime_mode

                if get_apex_runtime_mode() is ApexRuntimeMode.HARDENED_TESTBED:
                    from simulation.replay_clock import now_datetime

                    clock = now_datetime
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)
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
        self._broker_barrier_committed = False
        self._broker_resync_mono = 0.0
        # Market constraints cached at session level in a background thread so the
        # trading-loop tick is never blocked by a REST call to /markets/{epic}.
        self._market_constraints_cache: dict[str, Any] = {}
        self._market_constraints_fetched: bool = False
        self._feeder_last_bar_key: str | None = None
        self._last_ml_prob: float | None = None
        self._last_sig_direction: str = "WAIT"
        self._gate_signal_cache: SignalResult | None = None
        self._cached_signal: SignalResult | None = None
        self._last_feature_payload: dict[str, Any] | None = None
        self._last_probability_verdict: Any | None = None
        self._last_gate_eval_time: float = 0.0
        self._last_gate_eval_results: list[GateResult] | None = None
        self._gate_eval_lock = threading.Lock()
        self._entry_circuit_breaker: str = ""
        self.network_stable: bool = True
        self._tick_indicator_row: dict[str, Any] | None = None
        self._tick_live_state_vector: dict[str, Any] | None = None
        self._shm_matrix_pointer: Any = None
        self._alpha_matrix_lockout: bool = False
        self._last_matrix_lookup_us: float = 0.0
        from runtime.market_orchestrator import ROTATION_GRACE_CYCLES

        try:
            grace = int(config.get("rotation_grace_cycles") or ROTATION_GRACE_CYCLES)
        except (TypeError, ValueError):
            grace = ROTATION_GRACE_CYCLES
        self._rotation_grace_remaining: int = max(0, grace)

        self._paused_at_boot = bool(paused_at_boot)
        self._boot_ticks_enabled = threading.Event()
        if not self._paused_at_boot:
            self._boot_ticks_enabled.set()

    def unpause_from_boot(self) -> None:
        """Allow the loop thread to process live ticks (Gate 5 READY flip)."""
        self._paused_at_boot = False
        self._boot_ticks_enabled.set()

    @property
    def paused_at_boot(self) -> bool:
        with self._lock:
            return self._paused_at_boot

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

    def set_entry_circuit_breaker(self, reason: str) -> None:
        """In-memory entry gate isolation — never stops the loop thread."""
        with self._lock:
            self._entry_circuit_breaker = str(reason or "").strip()

    def clear_entry_circuit_breaker(self) -> None:
        with self._lock:
            self._entry_circuit_breaker = ""

    def entry_circuit_breaker(self) -> str:
        with self._lock:
            return self._entry_circuit_breaker

    def _hard_block_all_gates(
        self, detail: str, *, primary_gate: str
    ) -> list[GateResult]:
        blocked = GateResult(name=primary_gate, passed=False, detail=detail)
        results: list[GateResult] = [blocked]
        for name in GATE_NAMES:
            if name == primary_gate:
                continue
            results.append(GateResult(name=name, passed=False, detail=detail))
        return results

    def start(self) -> None:
        _ensure_production_warmed_alpha_on_boot()
        try:
            from system.protective_learning import ensure_autonomous_engine_on_boot

            ensure_autonomous_engine_on_boot()
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
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
        try:
            from system.protective_learning import (
                activate_test_mode_runtime,
                ensure_test_mode_execution_bypass_armed,
                ensure_test_mode_rsi_relaxation_armed,
            )

            activate_test_mode_runtime()
            ensure_test_mode_rsi_relaxation_armed()
            ensure_test_mode_execution_bypass_armed(self._store)
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
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

    def run_bare_metal_unified_tick(self, quote: Quote) -> TickContext | None:
        """Thread B hot path — naked frontier pointer → IG dispatch (single-string pipeline)."""
        try:
            from harmonization.tick_integrity import get_tick_integrity_filter

            ok, reason = get_tick_integrity_filter().validate_quote(quote)
            if not ok:
                return TickContext(
                    quote=quote,
                    gates=[],
                    all_passed=False,
                    wait_reason=f"TICK_INTEGRITY:{reason}",
                )
            deferred = self._bare_metal_compilation_gate(quote)
            if deferred is not None:
                return deferred
            ctx = self._run_frontier_tensor_tick(quote)
            try:
                self._shadow_tracer_after_tick(quote, ctx)
            except Exception:
                pass
            return ctx
        except Exception:
            self._bare_metal_schedule_socket_rebind()
            return TickContext(
                quote=quote,
                gates=[],
                all_passed=False,
                wait_reason="PIPELINE_REBIND",
            )

    def _bare_metal_schedule_socket_rebind(self) -> None:
        """Silent background websocket re-bind — never crash Thread B."""
        import threading

        def _worker() -> None:
            try:
                from system.feeds.multi_feed_hub import start_racing_multi_feed_hub

                start_racing_multi_feed_hub()
            except Exception:
                pass

        threading.Thread(
            target=_worker,
            name=f"bare-metal-rebind-{self._epic}",
            daemon=True,
        ).start()

    def _strategy_roundtrip_probe(self, ring: Any, coordinate: int) -> tuple[Any, int]:
        """Sub-microsecond 32-byte strategy slice extraction sanity check."""
        import time as _time

        t0 = _time.perf_counter_ns()
        strategy = ring.naked_strategy_lookup(int(coordinate))
        elapsed_ns = _time.perf_counter_ns() - t0
        self._last_matrix_lookup_us = float(elapsed_ns) / 1000.0
        return strategy, elapsed_ns

    def _bare_metal_compilation_gate(self, quote: Quote) -> TickContext | None:
        """
        Hard compilation gate — broker handle must be committed before matrix lookups.
        Triggers internal re-synchronization instead of CLIENT_MISSING dead-spin.
        """
        if self._broker_handle_verified():
            return None
        if self._resync_broker_handle() and self._broker_handle_verified():
            return None
        return TickContext(
            quote=quote,
            gates=[],
            all_passed=False,
            wait_reason="BROKER_BARRIER_RESYNC",
        )

    def _run_zero_gate_frontier_tick(self, quote: Quote) -> TickContext:
        """Legacy alias — single-string frontier tensor tick."""
        return self._run_frontier_tensor_tick(quote)

    def _broker_handle_verified(self) -> bool:
        if not getattr(self, "_broker_barrier_committed", False):
            return False
        client = self._rest_client()
        if client is None:
            return False
        try:
            from system.agent_execution_mode import authentic_demo_broker_required

            if authentic_demo_broker_required() or production_execution_active():
                from ig_api.mock_clients import MockIGRest
                from ig_api.rest_client import IGRestClient

                if isinstance(client, MockIGRest) or not isinstance(client, IGRestClient):
                    return False
        except Exception:
            pass
        return hasattr(client, "validate_order_schema") or hasattr(
            client, "place_market_order"
        )

    def _resync_broker_handle(self) -> bool:
        import time as _time

        now = _time.monotonic()
        if now - getattr(self, "_broker_resync_mono", 0.0) < 0.05:
            return False
        self._broker_resync_mono = now
        try:
            from system.agent_execution_mode import authentic_demo_broker_required

            if authentic_demo_broker_required() or production_execution_active():
                from system.bootstrap_phase_barrier import commit_rest_client_to_trading_loop
                from system.ig_rest_session import force_authenticated_ig_rest_client

                rest = force_authenticated_ig_rest_client()
                return commit_rest_client_to_trading_loop(self, rest)
            from system.bootstrap_phase_barrier import resync_trading_loop_broker

            return resync_trading_loop_broker(self)
        except Exception:
            return False

    def _shadow_tracer_after_tick(self, quote: Quote, ctx: TickContext | None) -> None:
        """Dry-run shadow pass — validate IG order schema without placement."""
        if ctx is None:
            return
        from trading.shadow_tracer import execute_shadow_dry_run, shadow_tracer_enabled

        if not shadow_tracer_enabled():
            return

        signal = ctx.signal
        direction = str(getattr(signal, "signal", "WAIT") or "WAIT")
        if direction not in ("BUY", "SELL"):
            direction = "BUY" if float(quote.offer or 0) >= float(quote.bid or 0) else "SELL"

        snap = getattr(signal, "snapshot", None) or {}
        strategy_raw = snap.get("strategy") or {}
        coordinate = int(snap.get("coordinate") or 0)
        win_zone = bool(ctx.all_passed) or int((strategy_raw or {}).get("zone") or 0) == 1

        trade_size = max(float((strategy_raw or {}).get("scalp_lot") or 0.1), 0.1)
        stop_pts = max(float((strategy_raw or {}).get("trailing_stop_distance") or 1.0), 1.0)
        limit_pts = max(float((strategy_raw or {}).get("dynamic_profit_target") or stop_pts), stop_pts)

        class _StrategyView:
            def as_dict(self) -> dict:
                return dict(strategy_raw)

        execute_shadow_dry_run(
            loop=self,
            quote=quote,
            epic=self._epic,
            market=self._market,
            direction=direction,
            coordinate=coordinate,
            strategy=_StrategyView(),
            trade_size=trade_size,
            stop_pts=stop_pts,
            limit_pts=limit_pts,
            win_zone=win_zone,
        )

    def _run_frontier_tensor_tick(self, quote: Quote) -> TickContext:
        """
        Single-string frontier tensor pass — coordinate key → naked 32-byte strategy slice.

        WIN_ZONE (1) bypasses legacy config chains; execution params are RAM-only.
        """
        import os
        import time as _time

        from intelligence.matrix_lookup_bridge import structural_metrics_from_quote
        from signals.signal_engine import SignalResult
        from system.ipc.ring_buffer import FAIL_ZONE, WIN_ZONE, get_alpha_ring_buffer

        if self._alpha_matrix_lockout:
            ctx = TickContext(
                quote=quote,
                gates=[],
                all_passed=False,
                wait_reason="MEMORY_DEATH_SWITCH: protective lockout 65%",
            )
            with self._lock:
                self._last_context = ctx
            return ctx

        try:
            self._signal_engine.add_quote(self._market, quote)
        except Exception:
            pass

        rsi, atr, momentum, direction = structural_metrics_from_quote(
            market=self._market,
            epic=self._epic,
            quote=quote,
            signal_engine=self._signal_engine,
            indicator_snapshot_fn=self._tick_indicator_snapshot,
        )
        # Zero-division geometry guard — force baseline metrics before array compile.
        if not rsi or rsi == 0.0:
            rsi = 50.0
        if not atr or atr == 0.0:
            atr = 1.5
        coordinate = self._compute_pattern_index(
            epic=self._epic,
            direction=direction,
            rsi=rsi,
            atr=atr,
            momentum=momentum,
        )

        from intelligence.matrix_prebaker import TOTAL_CELLS
        from system.ipc.ring_buffer import _string_diag_view
        from system.ipc.string_diagnostics import record_phase2, record_phase3

        p2_t0 = _time.perf_counter_ns()
        ring = get_alpha_ring_buffer()
        cell_empty = False
        try:
            frontier_zone = int(ring._frontier[int(coordinate) % TOTAL_CELLS])
            from system.ipc.ring_buffer import UNMAPPED

            cell_empty = frontier_zone == UNMAPPED
        except Exception:
            cell_empty = True
        diag = _string_diag_view(create=True)
        if diag is not None:
            record_phase2(
                diag,
                latency_us=int((_time.perf_counter_ns() - p2_t0) / 1000),
                coordinate=coordinate,
                rsi=rsi,
                atr=atr,
                momentum=momentum,
                total_cells=TOTAL_CELLS,
                cell_empty=cell_empty,
            )
        probe_active = False
        soak_active = False
        injection_payload: dict[str, Any] | None = None
        try:
            from trading.live_production_probe import (
                LIVE_PROBE_PAYLOAD,
                try_acquire_live_probe,
            )

            if try_acquire_live_probe(self._epic):
                injection_payload = dict(LIVE_PROBE_PAYLOAD)
                probe_active = True
                probe_dir = str(injection_payload["action"])
                probe_size = float(injection_payload["size"])
                ring.stamp_recency_coordinate(
                    coordinate,
                    zone=WIN_ZONE,
                    epic=str(injection_payload["epic"]),
                    rsi=rsi,
                    atr=atr,
                    momentum=momentum,
                    direction=probe_dir,
                    scalp_lot=probe_size,
                    trail_dist=max(float(atr) * 2.0, 5.0),
                    dyn_target=max(float(atr) * 3.0, 8.0),
                )
                direction = probe_dir
        except Exception:
            probe_active = False
            injection_payload = None

        if not probe_active:
            try:
                from system.soak_live_fire import try_consume_soak_injection

                soak_payload = try_consume_soak_injection(self._epic)
                if soak_payload:
                    injection_payload = dict(soak_payload)
                    soak_active = True
                    soak_dir = str(injection_payload["action"])
                    soak_size = float(injection_payload["size"])
                    ring.stamp_recency_coordinate(
                        coordinate,
                        zone=WIN_ZONE,
                        epic=str(injection_payload["epic"]),
                        rsi=rsi,
                        atr=atr,
                        momentum=momentum,
                        direction=soak_dir,
                        scalp_lot=soak_size,
                        trail_dist=max(float(atr) * 2.0, 5.0),
                        dyn_target=max(float(atr) * 3.0, 8.0),
                    )
                    direction = soak_dir
                    from system.engine_log import log_engine

                    log_engine(
                        f"SOAK_LIVE_FIRE WIN_ZONE stamp seq={injection_payload.get('sequence')} "
                        f"coord={coordinate} epic={injection_payload['epic']}"
                    )
            except Exception:
                soak_active = False
                if not probe_active:
                    injection_payload = None

        strategy, lookup_ns = self._strategy_roundtrip_probe(ring, coordinate)

        if diag is not None:
            sig_thr = 52.5
            atr_mult = 2.5
            try:
                from system.ipc.ring_buffer import CockpitShmHeader, _attach_cockpit_shm

                seg = _attach_cockpit_shm(create=False)
                hdr = CockpitShmHeader.from_buffer(seg.buf)
                if float(hdr.signal_threshold) > 0:
                    sig_thr = float(hdr.signal_threshold)
                if float(hdr.atr_multiplier) > 0:
                    atr_mult = float(hdr.atr_multiplier)
            except Exception:
                pass
            prior = int(getattr(self, "_string_fail_streak", 0) or 0)
            self._string_fail_streak = record_phase3(
                diag,
                latency_us=int(lookup_ns / 1000),
                zone=int(strategy.zone),
                signal_threshold=sig_thr,
                atr_multiplier=atr_mult,
                prior_fail_streak=prior,
            )

        win_zone = strategy.zone == WIN_ZONE or probe_active or soak_active
        if (probe_active or soak_active) and injection_payload:
            direction = str(injection_payload["action"])
        all_passed = win_zone and direction in ("BUY", "SELL")
        injecting = False
        wait_reason = ""
        if probe_active or soak_active:
            wait_reason = ""
        elif not win_zone:
            wait_reason = "SCANNING FRONTIER" if strategy.zone != FAIL_ZONE else "FAIL_ZONE"
        elif direction not in ("BUY", "SELL"):
            wait_reason = f"direction {direction}"

        confidence = max(
            strategy.win_probability * 100.0,
            100.0 if win_zone or probe_active or soak_active else 0.0,
        )
        signal = SignalResult(
            signal=direction if direction in ("BUY", "SELL") else "WAIT",
            raw_confidence=confidence,
            adjusted_confidence=confidence,
            learning_delta=0.0,
            setup_key=f"frontier|{self._epic}",
            notes="zero_gate_strategy_tensor",
            snapshot={
                "atr": atr,
                "rsi": rsi,
                "coordinate": coordinate,
                "strategy": strategy.as_dict(),
            },
        )

        outcome: TickOutcome | None = None
        if all_passed:
            if (probe_active or soak_active) and injection_payload:
                trade_size = float(injection_payload["size"])
            else:
                trade_size = max(float(strategy.scalp_lot), 0.1)
            stop_pts = max(float(strategy.trailing_stop_distance), 1.0)
            limit_pts = max(float(strategy.dynamic_profit_target), stop_pts)

            gate_exec = {
                "alpha_frontier": True,
                "zero_gate": True,
                "matrix_win_injection": True,
                "shadow_brain_injection": True,
                "gate_sourced": True,
                "coordinate": coordinate,
                "zone": WIN_ZONE if (probe_active or soak_active) else strategy.zone,
                "lookup_ns": lookup_ns,
                "direction": direction,
                "size": trade_size,
                "actual_size": trade_size,
                "stop_points": stop_pts,
                "limit_points": limit_pts,
                "stop_source": "strategy_tensor",
                "qmm_trailing_distance_points": stop_pts,
                "qmm_trailing_trigger_points": max(stop_pts * 0.5, 1.0),
                "qmm_breakeven_trigger_points": max(float(strategy.breakeven_buffer), 1.0),
                "strategy_payload": strategy.as_dict(),
            }
            if (probe_active or soak_active) and injection_payload:
                gate_exec["live_probe_alpha"] = probe_active
                gate_exec["soak_live_fire"] = soak_active
                gate_exec["signature"] = str(injection_payload.get("signature") or "")
                gate_exec["order_type"] = str(
                    injection_payload.get("order_type") or "MARKET"
                ).upper()
                gate_exec["injection_payload"] = dict(injection_payload)
            probe_live = probe_active and injection_payload is not None
            soak_live = soak_active and injection_payload is not None
            injection_live = probe_live or soak_live
            injecting = False
            try:
                if injection_live:
                    from system.engine_log import log_engine

                    tag = "SOAK_LIVE_FIRE" if soak_live else "LIVE_PROBE_ALPHA"
                    log_engine(
                        f"{tag} dispatch {injection_payload.get('action')} "
                        f"epic={injection_payload['epic']} "
                        f"size={injection_payload['size']} "
                        f"signature={injection_payload.get('signature')}"
                    )
                if soak_live and injection_payload:
                    self._soak_direct_broker_dispatch(
                        injection_payload=injection_payload,
                        coordinate=coordinate,
                        quote=quote,
                        trade_size=trade_size,
                        confidence=confidence,
                        lookup_ns=lookup_ns,
                        direction=direction,
                    )
                    outcome = None
                else:
                    gate_exec = self._finalize_gate_execution_params(
                        gate_exec, trade_size=trade_size
                    )
                    outcome = self._execution_loop.process_tick(
                        self._market,
                        self._epic,
                        quote,
                        prefetched_signal=signal,
                        gate_execution_params=gate_exec,
                        gate_snapshot={
                            "alpha_frontier": True,
                            "zero_gate": True,
                            "live_probe_alpha": probe_live,
                            "soak_live_fire": soak_live,
                        },
                        shadow_force_fill=False,
                    )
                if outcome is not None:
                    exec_res = getattr(outcome, "execution", None)
                    if exec_res is not None and (
                        bool(getattr(exec_res, "success", False))
                        or str(getattr(exec_res, "action", "") or "") == "SUBMITTED"
                    ):
                        injecting = True
                if injection_live and not soak_live:
                    from system.engine_log import log_engine

                    exec_ok = (
                        outcome is not None
                        and outcome.execution is not None
                        and bool(getattr(outcome.execution, "success", False))
                    )
                    tag = "LIVE_PROBE_ALPHA"
                    log_engine(
                        f"{tag} complete success={exec_ok} "
                        f"deal={getattr(outcome.execution, 'deal_id', '') if outcome and outcome.execution else '—'}"
                    )
                if soak_live:
                    pass
                elif outcome is not None and outcome.execution is not None:
                    exec_res = outcome.execution
                    action = str(getattr(exec_res, "action", direction) or direction)
                    success = bool(getattr(exec_res, "success", False))
                    if soak_live and not success:
                        success = bool(str(getattr(exec_res, "deal_id", "") or "").strip())
                    entry_px = float(quote.offer if direction == "BUY" else quote.bid)
                    exit_px = float(quote.bid if direction == "BUY" else quote.offer)
                    pnl_gbp = float(getattr(exec_res, "pnl_gbp", 0) or 0)
                    latency_us = float(lookup_ns) / 1000.0
                    deal_id = str(getattr(exec_res, "deal_id", "") or "")

                    if soak_live:
                        from system.soak_live_fire import emit_soak_telemetry, record_soak_result

                        emit_soak_telemetry(
                            epic=str(injection_payload["epic"]),
                            direction=direction,
                            entry=entry_px,
                            size=float(trade_size),
                            deal_id=deal_id,
                            coordinate=coordinate,
                            confidence=confidence,
                            latency_us=latency_us,
                            success=success,
                            sequence=int(injection_payload.get("sequence") or 0),
                        )
                        record_soak_result(
                            sequence=int(injection_payload.get("sequence") or 0),
                            success=success,
                            deal_id=deal_id,
                            http_status=200 if (success or deal_id) else 0,
                        )
                    elif probe_live:
                        from trading.live_production_probe import emit_live_probe_telemetry

                        emit_live_probe_telemetry(
                            epic=str(injection_payload["epic"]),
                            direction=direction,
                            entry=entry_px,
                            size=float(trade_size),
                            deal_id=deal_id,
                            coordinate=coordinate,
                            confidence=confidence,
                            latency_us=latency_us,
                            success=success,
                        )
                    else:
                        from system.unified_fulfillment_cache import (
                            record_execution_performance_row,
                        )

                        result = "WIN" if success else "LOSS"
                        status = (
                            "OPEN" if action.upper() == "OPEN" and success else "CLOSED"
                        )
                        record_execution_performance_row(
                            epic=self._epic,
                            direction=direction,
                            result=result,
                            confidence=confidence,
                            cell_index=coordinate,
                            latency_us=latency_us,
                            deal_id=deal_id,
                            size=float(trade_size),
                            entry=entry_px,
                            exit=exit_px,
                            pnl_gbp=pnl_gbp,
                            status=status,
                        )
                elif injection_live and injection_payload:
                    entry_px = float(quote.offer if direction == "BUY" else quote.bid)
                    if soak_live:
                        from system.soak_live_fire import emit_soak_telemetry, record_soak_result

                        emit_soak_telemetry(
                            epic=str(injection_payload["epic"]),
                            direction=direction,
                            entry=entry_px,
                            size=float(injection_payload["size"]),
                            deal_id="",
                            coordinate=coordinate,
                            confidence=confidence,
                            latency_us=float(lookup_ns) / 1000.0,
                            success=False,
                            sequence=int(injection_payload.get("sequence") or 0),
                        )
                        record_soak_result(
                            sequence=int(injection_payload.get("sequence") or 0),
                            success=False,
                            deal_id="",
                            http_status=0,
                        )
                    else:
                        from trading.live_production_probe import emit_live_probe_telemetry

                        emit_live_probe_telemetry(
                            epic=str(injection_payload["epic"]),
                            direction=direction,
                            entry=entry_px,
                            size=float(injection_payload["size"]),
                            deal_id="",
                            coordinate=coordinate,
                            confidence=confidence,
                            latency_us=float(lookup_ns) / 1000.0,
                            success=False,
                        )
                exec_wait = self._execution_wait_reason(outcome)
                if exec_wait:
                    wait_reason = exec_wait
                    all_passed = False
                    injecting = False
                    if (win_zone or probe_active) and diag is not None:
                        from system.ipc.string_diagnostics import emit_broker_tunnel_diag

                        emit_broker_tunnel_diag(reason=exec_wait)
                elif (
                    outcome is not None
                    and outcome.execution is not None
                    and not bool(getattr(outcome.execution, "success", False))
                    and (win_zone or probe_active)
                    and diag is not None
                ):
                    from system.ipc.string_diagnostics import emit_broker_tunnel_diag

                    emit_broker_tunnel_diag(
                        reason=str(
                            getattr(outcome.execution, "rejection_reason", "") or "order rejected"
                        )
                    )
            except Exception as exc:
                from system.engine_log import log_engine

                log_engine(
                    f"frontier dispatch failed epic={self._epic}: "
                    f"{type(exc).__name__}: {exc}"
                )
                wait_reason = f"execution: {type(exc).__name__}: {exc}"
                all_passed = False
                injecting = False

        ctx = TickContext(
            quote=quote,
            gates=[],
            all_passed=all_passed,
            wait_reason=wait_reason,
            signal=signal,
            fitness=confidence,
            outcome=outcome,
        )
        with self._lock:
            self._last_context = ctx

        try:
            from system.unified_fulfillment_cache import record_frontier_state

            now = _time.monotonic()
            last = float(getattr(self, "_last_frontier_diag_mono", 0.0) or 0.0)
            if now - last >= 0.4:
                self._last_frontier_diag_mono = now
                record_frontier_state(
                    epic=self._epic,
                    coordinate=coordinate,
                    zone=strategy.zone,
                    lookup_ns=lookup_ns,
                    direction=direction,
                    rsi=rsi,
                    atr=atr,
                    momentum=momentum,
                    win_zone=win_zone,
                    all_passed=all_passed,
                    injecting=injecting,
                    wait_reason=wait_reason,
                    feed_race_us=ring.feed_race_profile_us(),
                    strategy=strategy.as_dict(),
                )
        except Exception:
            pass
        return ctx

    def _loop_thread(self) -> None:
        from system.stream_ready import wait_stream_ready

        if self._paused_at_boot:
            log_engine(
                f"trading_loop thread starting epic={self._epic} — dormant (paused_at_boot)"
            )
            while not self._stop.is_set():
                if self._boot_ticks_enabled.wait(timeout=0.5):
                    break
            if self._stop.is_set():
                with self._lock:
                    self._running = False
                return
            log_engine(
                f"trading_loop thread epic={self._epic} — boot READY, arming tick loop"
            )

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
                    _intercept_broker_connectivity_failure(e, subsystem="trading_loop_tick")
                    self._sentinel_on_tick(loop_error=e)
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
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)
            # Self-heal: signal the stuck loop to stop so the orchestrator can respawn it.
            log_engine(
                f"Watchdog: requesting loop restart for {self._market} ({self._epic})"
            )
            self._stop.set()

    def _sentinel_stream_disconnected(self) -> bool:
        try:
            from system.stream_ready import is_stream_ready

            if not is_stream_ready():
                return True
            from system.market_data_hub import get_market_data_hub

            snap = get_market_data_hub().get_snapshot(self._epic)
            if snap is None or snap.bid <= 0 or snap.offer <= 0:
                return True
            return False
        except Exception:
            return False

    def _sentinel_quote_stale(self) -> bool:
        try:
            from system.market_data_hub import get_market_data_hub

            snap = get_market_data_hub().get_snapshot(self._epic)
            if snap is None:
                return True
            return float(snap.age_seconds()) > 45.0
        except Exception:
            return False

    def _sentinel_on_tick(self, *, loop_error: Exception | None = None) -> None:
        """Feed live loop health into v27 Operational AI monitor (§17)."""
        try:
            from ai.operational.system_monitor import get_system_monitor

            get_system_monitor().on_loop_tick(
                self._epic,
                loop_error=loop_error is not None,
                stream_disconnected=(
                    True
                    if loop_error is not None
                    else self._sentinel_stream_disconnected()
                ),
                quote_stale=(
                    False if loop_error is not None else self._sentinel_quote_stale()
                ),
            )
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)

    def _force_live_signal_threshold(self) -> float:
        """Live floor from merged config; fallback constant only when config is unset."""
        try:
            thr = float(getattr(self._config, "signal_threshold", LIVE_EXEC_SIGNAL_THRESHOLD))
        except (TypeError, ValueError):
            thr = float(LIVE_EXEC_SIGNAL_THRESHOLD)
        if thr <= 0:
            thr = float(LIVE_EXEC_SIGNAL_THRESHOLD)
        return thr

    def _dynamic_live_signal_threshold(self, *, atr: float, rsi: float) -> float:
        """Volatility-scaled live floor — relaxes into 75–80% paradox band."""
        base = self._force_live_signal_threshold()
        try:
            from harmonization.volatility_gate import no_trade_paradox_threshold

            baseline = float(
                getattr(self._config, "stop_distance_points", None)
                or getattr(self._config, "adaptive_atr_risk_multiple", None)
                or 10.0
            )
            thr = no_trade_paradox_threshold(
                base,
                atr=float(atr or 1.5),
                atr_baseline=max(baseline, 1.0),
                rsi=float(rsi or 50.0),
            )
        except Exception:
            thr = base
        try:
            self._config.signal_threshold = thr
        except Exception:
            pass
        return thr

    def _finalize_gate_execution_params(
        self,
        gate_exec: dict[str, Any] | None,
        *,
        trade_size: float | None = None,
    ) -> dict[str, Any]:
        """
        Gold / Wall St order assembly — non-null gate_execution_params envelope.

        Forces micro-lot (0.1), 10pt stop, 20pt limit for night-matrix dispatch epics.
        """
        from execution.types import force_inject_gate_execution_params
        from execution.epic_normalizer import normalize_night_matrix_epic
        from harmonization.iron_clad_risk import (
            mandatory_limit_points_for_epic,
            mandatory_stop_points_for_epic,
            MAX_ORDER_SIZE,
        )
        from trading.micro_lot_verification import clamp_micro_lot_size, micro_lot_verification_enabled

        raw = dict(gate_exec or {})
        epic_norm = normalize_night_matrix_epic(self._epic)
        stop_floor = mandatory_stop_points_for_epic(epic_norm)
        limit_floor = mandatory_limit_points_for_epic(epic_norm)
        if epic_norm in _NIGHT_MATRIX_FORCE_GATE_EPICS or micro_lot_verification_enabled():
            default_size = clamp_micro_lot_size(
                float(trade_size or raw.get("actual_size") or raw.get("size") or 1.0)
            )
            return force_inject_gate_execution_params(
                epic=epic_norm,
                size=default_size,
                gate_execution_params=raw,
                stop_points=stop_floor,
                limit_points=limit_floor,
            )
        if raw:
            from execution.types import normalize_gate_execution_params

            normalized = normalize_gate_execution_params(raw)
            if normalized is not None:
                return normalized
        return raw

    def _iron_clad_fallback_gate_exec(
        self, trade_size: float, *, atr: float = 0.0
    ) -> dict[str, Any]:
        """Non-bypassable stop/limit/size envelope when risk gate omits params."""
        from execution.epic_normalizer import normalize_night_matrix_epic
        from execution.types import force_inject_gate_execution_params
        from harmonization.iron_clad_risk import (
            mandatory_limit_points_for_epic,
            mandatory_stop_points_for_epic,
        )
        from trading.micro_lot_verification import clamp_micro_lot_size, micro_lot_verification_enabled

        epic_norm = normalize_night_matrix_epic(self._epic)
        stop_floor = mandatory_stop_points_for_epic(epic_norm)
        limit_floor = mandatory_limit_points_for_epic(epic_norm)
        if micro_lot_verification_enabled():
            size = clamp_micro_lot_size(float(trade_size))
        else:
            size = 1.0 if epic_norm in _NIGHT_MATRIX_FORCE_GATE_EPICS else float(trade_size)
        return force_inject_gate_execution_params(
            epic=epic_norm,
            size=size,
            stop_points=stop_floor,
            limit_points=limit_floor,
        )

    def _run_tick(self) -> TickContext | None:
        import os
        import time

        self._force_live_signal_threshold()
        self._last_tick_mono = time.monotonic()
        self._silence_alert_sent = False
        _tick_t0 = time.perf_counter()
        _ctx: TickContext | None = None

        if os.environ.get("IG_TEST_HARNESS", "").strip() == "1":
            try:
                _ctx = self._run_tick_harness_fast()
                return _ctx
            finally:
                self._publish_live_state_tick(
                    _ctx,
                    latency_ms=(time.perf_counter() - _tick_t0) * 1000.0,
                )

        try:
            from ai.operational.profiler import get_operational_profiler

            _prof = get_operational_profiler()
        except Exception:
            _prof = None
        try:
            _ctx = self._run_tick_core()
            return _ctx
        finally:
            if bare_metal_hot_path_active():
                pass
            else:
                if _prof is not None:
                    _prof.record_probe(
                        "probe_trading_loop_tick",
                        (time.perf_counter() - _tick_t0) * 1000.0,
                        epic=self._epic,
                    )
                    if _ctx is not None:
                        self._feed_profiler_session(_prof, _ctx)
                self._publish_live_state_tick(
                    _ctx,
                    latency_ms=(time.perf_counter() - _tick_t0) * 1000.0,
                )

    def _publish_live_state_tick(
        self,
        ctx: TickContext | None,
        *,
        latency_ms: float,
    ) -> None:
        """
        Non-blocking telemetry — updates in-memory state + native shared RAM.

        The hot path serializes a compact JSON byte string into the 64 KiB
        ``multiprocessing.shared_memory`` segment (sub-microsecond). No network
        I/O, WebSocket emission, or disk flush occurs on this path.
        """
        try:
            if ctx is None or ctx.quote is None:
                return
            from system.identity.state_cache import get_live_state_cache

            cache = get_live_state_cache()
            cache.record_tick(
                epic=str(self._epic),
                bid=float(ctx.quote.bid),
                offer=float(ctx.quote.offer),
                latency_ms=float(latency_ms),
            )
        except Exception as exc:
            log_guarded_exception("live_state_cache_tick", exc)

    def _run_tick_harness_fast(self) -> TickContext | None:
        """Deterministic harness path — signal + twin-engine + ML, no heavy gate stack."""
        from data.models import Quote

        self._gate_signal_cache = None
        quote = self._quote_source()
        if quote is None:
            ctx = TickContext(
                quote=Quote(self._clock(), 0.0, 0.0),
                wait_reason="no quote",
                all_passed=False,
            )
            return ctx

        sig = self._get_gate_signal()
        conf = float(sig.adjusted_confidence)
        rules_conf = conf
        ml_prob: float | None = None
        snap = sig.snapshot or {}
        last = snap.get("last") or self._tick_indicator_snapshot(quote)
        _atr = float(last.get("atr", 0) or 0)
        _stop = max(1.0, float(self._config.stop_distance_points))
        twin_features = {
            "adjusted_score": rules_conf,
            "rsi": float(last.get("rsi", 0) or 0),
            "atr_ratio": _atr / _stop,
        }

        try:
            from system.ml.twin_engine_core import get_twin_engine_core

            twin_prob = get_twin_engine_core().ingest_and_score(
                epic=str(getattr(self, "_epic", "") or ""),
                ts_utc=None,
                bid=float(quote.bid),
                offer=float(quote.offer),
                features=twin_features,
                direction=str(sig.signal or "WAIT"),
            )
            if twin_prob > 0.0:
                ml_prob = twin_prob
        except Exception as exc:
            log_guarded_exception("trading_loop_twin_engine", exc)

        if bool(self._config.get("USE_ML_SIGNAL", False)):
            try:
                from trading.ml_scorer import get_ml_scorer

                scorer = get_ml_scorer()
                if scorer.is_trained():
                    features = {
                        "adjusted_score": rules_conf,
                        "raw_score": float(snap.get("raw_confidence", rules_conf)),
                        "rsi": twin_features["rsi"],
                        "atr_ratio": twin_features["atr_ratio"],
                    }
                    if all(f in features for f in scorer.feature_names):
                        scored = scorer.score(
                            features, use_ml_signal=True, timeout_s=0.05
                        )
                        if scored > 0.0:
                            ml_prob = (
                                (float(ml_prob) * 0.7) + (scored * 0.3)
                                if ml_prob is not None
                                else scored
                            )
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)

        threshold = float(self._points.trade_confidence_threshold(self._config))
        direction = str(sig.signal or "WAIT").upper()
        passed = direction in ("BUY", "SELL") and conf >= threshold
        ctx = TickContext(
            quote=quote,
            all_passed=passed,
            wait_reason="" if passed else "harness_threshold",
        )
        self._last_ml_prob = ml_prob
        self._last_sig_direction = direction
        return ctx

    def _feed_profiler_session(self, prof: Any, ctx: TickContext) -> None:
        try:
            session_open = any(
                g.name == "session_open" and g.passed for g in (ctx.gates or [])
            )
            min_atr = float(getattr(self._config, "min_atr_points", 0) or 0)
            atr_cleared = False
            gate_fails: dict[str, int] = {}
            for g in ctx.gates or []:
                if g.name == "environment_fitness":
                    val = g.value if isinstance(g.value, dict) else {}
                    factors = (
                        val.get("factors", {})
                        if isinstance(val.get("factors"), dict)
                        else {}
                    )
                    atr_pts = float(factors.get("atr") or 0)
                    if atr_pts > 0:
                        atr_cleared = atr_pts >= min_atr if min_atr > 0 else True
                if not g.passed:
                    gate_fails[g.name] = gate_fails.get(g.name, 0) + 1
            dominant = (
                max(gate_fails.items(), key=lambda kv: kv[1])[0] if gate_fails else ""
            )
            traded = bool(
                ctx.outcome and ctx.outcome.execution and ctx.outcome.execution.success
            )
            prof.update_session_activity(
                self._epic,
                session_open=session_open,
                trade_executed=traded,
                atr_filter_cleared=atr_cleared,
                gate_failures=gate_fails,
                dominant_gate_block=dominant,
            )
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)

    def _reset_gate_signal_cache(self) -> None:
        self._gate_signal_cache = None
        self._cached_signal = None

    def _trade_confidence_threshold(self) -> float:
        _log_session_validation_floor_once()
        if session_validation_capture_active():
            return SESSION_VALIDATION_CONFIDENCE_FLOOR
        try:
            threshold = float(self._points.trade_confidence_threshold(self._config))
        except Exception:
            threshold = float(self._config.signal_threshold)
        return _apply_production_warmed_confidence_floor(threshold)

    def _apply_operational_confidence_threshold(self, threshold: float) -> float:
        """Merge protective-learning caps with session-validation floor."""
        _log_session_validation_floor_once()
        if session_validation_capture_active():
            return SESSION_VALIDATION_CONFIDENCE_FLOOR
        try:
            from system.protective_learning import apply_temporary_test_confidence_floor

            threshold = apply_temporary_test_confidence_floor(float(threshold))
        except Exception:
            threshold = float(threshold)
        return _apply_production_warmed_confidence_floor(threshold)

    def _cache_promoted_signal(
        self, sig: SignalResult, *, raw_size: float | None = None
    ) -> SignalResult:
        """Promote WAIT→BUY/SELL and persist to primary gate signal caches."""
        promoted = promote_high_confidence_signal(
            sig, self._trade_confidence_threshold(), raw_size=raw_size
        )
        if raw_size is not None:
            snap = promoted.snapshot or {}
            if snap.get("under_min_lot"):
                from apex.hardening import under_min_lot_detail

                log_engine(under_min_lot_detail(int(snap.get("dispatch_size_int") or 0)))
        promoted = self._apply_micro_trend_promotion(promoted)
        if promoted.signal in ("BUY", "SELL"):
            try:
                from apex.avionics_story import append_avionics_story

                snap = promoted.snapshot or {}
                size_int = snap.get("dispatch_size_int")
                conf = float(promoted.adjusted_confidence)
                msg = (
                    f"PROMOTED: {self._market} {promoted.signal} at {conf:.1f}% — "
                    f"int(size // 1)={size_int if size_int is not None else '?'} "
                    f"→ LiveExecutor IG DEMO REST"
                )
                append_avionics_story(msg, kind="promoted", epic=self._epic)
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)
        self._gate_signal_cache = promoted
        self._cached_signal = promoted
        try:
            apply_fn = getattr(self._signal_engine, "apply_dispatch_promotion", None)
            if callable(apply_fn):
                apply_fn(self._market, promoted)
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        return promoted

    def _apply_micro_trend_promotion(self, sig: SignalResult) -> SignalResult:
        """Worker B micro-trend RoC — instant promote when 42%/45% bands clear."""
        try:
            from apex.microkernel import get_microkernel
            from signals.indicators import (
                STRATEGY_THRESHOLD_HIGH_PCT,
                STRATEGY_THRESHOLD_LOW_PCT,
            )

            mt = get_microkernel().micro_trend_for(self._epic)
            if not mt.get("promote"):
                return sig
            score = float(mt.get("score_pct") or 0.0)
            if score < STRATEGY_THRESHOLD_LOW_PCT:
                return sig
            raw_dir = str(mt.get("direction") or "").strip()
            if raw_dir not in ("BUY", "SELL"):
                return sig
            snap = dict(sig.snapshot or {})
            snap["micro_trend_score_pct"] = score
            snap["micro_trend_promote"] = True
            snap["micro_trend_tier"] = (
                "high" if score >= STRATEGY_THRESHOLD_HIGH_PCT else "low"
            )
            if str(snap.get("raw_signal") or "").strip() not in ("BUY", "SELL"):
                snap["raw_signal"] = raw_dir
            threshold = self._trade_confidence_threshold()
            peak = max(score, _peak_confidence_from_signal(sig, float(sig.adjusted_confidence)))
            if peak >= STRATEGY_THRESHOLD_LOW_PCT:
                return promote_high_confidence_signal(
                    SignalResult(
                        signal=sig.signal,
                        raw_confidence=float(sig.raw_confidence),
                        adjusted_confidence=max(float(sig.adjusted_confidence), peak),
                        learning_delta=float(sig.learning_delta),
                        setup_key=str(sig.setup_key),
                        notes=f"{sig.notes} | micro-trend RoC {score:.1f}%",
                        snapshot=snap,
                    ),
                    threshold,
                )
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        return sig

    @staticmethod
    def _replace_gate_result(
        results: list[GateResult], replacement: GateResult
    ) -> list[GateResult]:
        for idx, gate in enumerate(results):
            if gate.name == replacement.name:
                updated = list(results)
                updated[idx] = replacement
                return updated
        return list(results) + [replacement]

    @staticmethod
    def _out_of_hours_spread_scale(*, at: datetime | None = None) -> float:
        """21:00–07:00 Europe/London — widen spread cap for post-close broker expansion."""
        try:
            from zoneinfo import ZoneInfo

            now = at or datetime.now(ZoneInfo("Europe/London"))
        except Exception:
            now = at or datetime.now()
        hour = int(now.hour)
        if hour >= OOH_SESSION_START_HOUR_BST or hour < OOH_SESSION_END_HOUR_BST:
            return OOH_SPREAD_SCALE
        return 1.0

    def _reset_tick_memo(self) -> None:
        """Per-tick indicator / live_state_vector memo — gates 3 & 10."""
        self._tick_indicator_row = None
        self._tick_live_state_vector = None

    def _tick_indicator_snapshot(self, quote: Quote) -> dict[str, Any]:
        """Single quote_df / last_row read per tick (gate 3 cold_start_gap ATR)."""
        if self._tick_indicator_row is not None:
            return self._tick_indicator_row
        row: dict[str, Any] = {}
        try:
            last_row_fn = getattr(self._signal_engine, "last_row", None)
            if callable(last_row_fn):
                r = last_row_fn(self._market, 15)
                if r is not None:
                    if hasattr(r, "to_dict"):
                        row = dict(r.to_dict())
                    elif isinstance(r, dict):
                        row = dict(r)
            if not row:
                df = self._signal_engine.quote_df(self._market)
                if df is not None and len(df) > 0:
                    last = df.iloc[-1]
                    row = (
                        dict(last.to_dict())
                        if hasattr(last, "to_dict")
                        else dict(last)
                    )
        except Exception:
            row = {}
        self._tick_indicator_row = row
        return row

    def _build_live_state_vector(
        self,
        quote: Quote,
        points_state: dict[str, Any],
    ) -> dict[str, Any]:
        """Memoized in-RAM feature vector — shared by gates 3, 10, horizon overlay."""
        if self._tick_live_state_vector is not None:
            return self._tick_live_state_vector
        ind = self._tick_indicator_snapshot(quote)
        merged = dict(points_state)
        try:
            _atr = float(ind.get("atr", 0) or 0)
            _stop = max(1.0, float(self._config.stop_distance_points))
            if not float(merged.get("atr_multiplier") or 0):
                merged["atr_multiplier"] = _atr / _stop if _stop > 0 else 0.0
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        from ml.interim_scorer import extract_live_state_vector

        vector = extract_live_state_vector(self._market, quote, merged)
        if isinstance(vector, dict):
            vector = dict(vector)
            vector["indicator_row"] = ind
            vector["atr"] = float(ind.get("atr", 0) or 0.0)
            vector["rsi"] = float(ind.get("rsi", 0) or 0.0)
            self._tick_live_state_vector = vector
            return vector
        self._tick_live_state_vector = {}
        return self._tick_live_state_vector

    def _publish_ml_sizing_multiplier(self, multiplier: float) -> None:
        """Write Gate 11 sizing into the shared per-tick live_state_vector."""
        mult = float(multiplier)
        self._ml_sizing_multiplier = mult
        vec = self._tick_live_state_vector
        if isinstance(vec, dict):
            vec["ml_sizing_multiplier"] = mult

    def _ml_sizing_multiplier_from_live_state(self) -> float:
        """Gate 7 reads Gate 11 output from the shared live_state_vector object."""
        vec = self._tick_live_state_vector
        if isinstance(vec, dict):
            try:
                return float(vec.get("ml_sizing_multiplier", 1.0) or 1.0)
            except (TypeError, ValueError):
                return 1.0
        return float(getattr(self, "_ml_sizing_multiplier", 1.0) or 1.0)

    def _get_gate_signal(self) -> SignalResult:
        """Single signal evaluation per tick — reused across gate stack (§20 latency)."""
        if getattr(self, "_gate_signal_cache", None) is None:
            sig = self._signal_engine.evaluate(self._market)
            return self._cache_promoted_signal(sig)
        return self._gate_signal_cache

    def _emergency_protective_lockout_65(self) -> None:
        """
        Null-pointer memory death-switch — OS unmapped SHM forces 65% protective floor.
        Zero polling overhead; triggered only on naked pointer fault.
        """
        self._alpha_matrix_lockout = True
        self._shm_matrix_pointer = None
        try:
            from system.protective_learning import apply_temporary_test_confidence_floor

            apply_temporary_test_confidence_floor(65.0)
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        log_engine(
            "MEMORY_DEATH_SWITCH: alpha matrix pointer fault — protective lockout 65%"
        )

    def _bind_shm_matrix_pointer(self) -> bool:
        if self._shm_matrix_pointer is not None:
            return True
        try:
            from system.ipc.ring_buffer import get_alpha_ring_buffer, unified_engine_active

            if unified_engine_active():
                ring = get_alpha_ring_buffer()
                self._shm_matrix_pointer = ring.matrix_view()
                return True
        except Exception:
            pass
        try:
            from intelligence.matrix_prebaker import get_alpha_matrix_segment

            segment = get_alpha_matrix_segment(create=False)
            self._shm_matrix_pointer = segment.matrix
            return True
        except Exception:
            self._shm_matrix_pointer = None
            return False

    def _soak_direct_broker_dispatch(
        self,
        *,
        injection_payload: dict[str, Any],
        coordinate: int,
        quote: Quote,
        trade_size: float,
        confidence: float,
        lookup_ns: int,
        direction: str,
    ) -> bool:
        """Synchronous soak harness — POST /positions/otc on order-dispatch lane."""
        from system.engine_log import log_engine
        from system.soak_live_fire import emit_soak_telemetry, record_soak_result

        seq = int(injection_payload.get("sequence") or 0)
        epic = str(injection_payload["epic"])
        entry_px = float(quote.offer if direction == "BUY" else quote.bid)
        latency_us = float(lookup_ns) / 1000.0

        try:
            from execution.atomic_gateway import assert_execution_allowed, order_dispatch_lane

            hold = assert_execution_allowed()
            if hold:
                record_soak_result(sequence=seq, success=False, deal_id="", http_status=0)
                emit_soak_telemetry(
                    epic=epic,
                    direction=direction,
                    entry=entry_px,
                    size=float(trade_size),
                    deal_id="",
                    coordinate=coordinate,
                    confidence=confidence,
                    latency_us=latency_us,
                    success=False,
                    sequence=seq,
                )
                log_engine(f"SOAK_LIVE_FIRE complete success=False deal=— reason={hold}")
                return False

            client = self._rest_client()
            if client is None:
                record_soak_result(sequence=seq, success=False, deal_id="", http_status=0)
                log_engine("SOAK_LIVE_FIRE complete success=False deal=— reason=no_rest_client")
                return False

            from execution.entry_inflight import clear_entry, try_begin_entry

            clear_entry(epic)
            try_begin_entry(epic, direction, trade_size)
            stop_pts = max(float(injection_payload.get("stop_points") or 10.0), 5.0)
            with order_dispatch_lane():
                data = client.place_market_order(
                    epic=epic,
                    direction=direction,
                    size=float(trade_size),
                    stop_distance=stop_pts,
                    limit_distance=stop_pts * 1.5,
                )
            ref = str(data.get("dealReference") or "")
            ok = bool(ref)
            clear_entry(epic)
            record_soak_result(
                sequence=seq,
                success=ok,
                deal_id=ref,
                http_status=200 if ok else 0,
            )
            emit_soak_telemetry(
                epic=epic,
                direction=direction,
                entry=entry_px,
                size=float(trade_size),
                deal_id=ref,
                coordinate=coordinate,
                confidence=confidence,
                latency_us=latency_us,
                success=ok,
                sequence=seq,
            )
            log_engine(f"SOAK_LIVE_FIRE complete success={ok} deal={ref or '—'}")
            return ok
        except Exception as exc:
            record_soak_result(sequence=seq, success=False, deal_id="", http_status=0)
            log_engine(f"SOAK_LIVE_FIRE complete success=False deal=— error={type(exc).__name__}: {exc}")
            try:
                from execution.entry_inflight import clear_entry

                clear_entry(epic)
            except Exception:
                pass
            return False

    def _compute_pattern_index(
        self,
        *,
        epic: str,
        direction: str,
        rsi: float,
        atr: float,
        momentum: float,
    ) -> int:
        from intelligence.matrix_prebaker import (
            epic_slot,
            matrix_cell_index,
            quantize_atr,
            quantize_momentum,
            quantize_rsi,
        )

        # Phase 2 — geometry quantization guard (non-destructive baseline before vector compile).
        if not rsi or rsi == 0.0:
            rsi = 50.0
        if not atr or atr == 0.0:
            atr = 1.5

        return matrix_cell_index(
            epic_id=epic_slot(epic),
            direction=str(direction).upper(),
            rsi_q=quantize_rsi(rsi),
            atr_q=quantize_atr(atr, epic=epic),
            mom_q=quantize_momentum(momentum),
        )

    def _naked_matrix_row_lookup(self, pattern_index: int) -> Any:
        """Direct naked pointer read — ring buffer or SHM; faults trigger death-switch."""
        try:
            from system.ipc.ring_buffer import get_alpha_ring_buffer, unified_engine_active

            if unified_engine_active():
                return get_alpha_ring_buffer().lookup_row(int(pattern_index))
        except (AttributeError, ValueError, FileNotFoundError, TypeError, IndexError):
            self._emergency_protective_lockout_65()
            raise
        except Exception:
            pass
        try:
            from intelligence.matrix_prebaker import matrix_row_with_streaming_ffill

            matrix_payload = matrix_row_with_streaming_ffill(
                self._shm_matrix_pointer,
                int(pattern_index),
                epic=self._epic,
            )
            return matrix_payload
        except (AttributeError, ValueError, FileNotFoundError, TypeError, IndexError):
            self._emergency_protective_lockout_65()
            raise

    def _synthetic_alpha_matrix_gates(
        self,
        lookup: Any,
        signal: SignalResult,
        *,
        confidence: float,
        fitness: float,
        direction: str,
        ml_probability: float | None = None,
    ) -> list[GateResult]:
        """Compact gate snapshot for dashboard telemetry — no 12-gate recompute."""
        ml_prob = float(ml_probability if ml_probability is not None else lookup.win_probability)
        floor = float(getattr(lookup, "live_threshold", None) or lookup.signal_floor)
        lookup.signal_floor = floor
        ml_passed = ml_prob >= float(lookup.ml_floor)
        sig_passed = confidence >= floor
        fit_passed = fitness >= float(lookup.fitness_floor)
        return [
            GateResult(
                "alpha_matrix_lookup",
                bool(lookup.hit),
                value={
                    "cell_index": lookup.cell_index,
                    "win_probability": lookup.win_probability,
                    "latency_us": lookup.latency_us,
                    "samples": lookup.samples,
                    "reason": lookup.reason,
                },
                detail="" if lookup.hit else str(lookup.reason or "cell_empty"),
            ),
            GateResult(
                "signal_confidence",
                sig_passed,
                value={
                    "confidence": confidence,
                    "signal": signal,
                    "direction": direction,
                    "floor": floor,
                    "threshold": floor,
                    "live_threshold": floor,
                },
                detail=(
                    ""
                    if sig_passed
                    else (
                        f"signal_confidence failed: conf {confidence:.2f} "
                        f"< {floor:.2f}% custom threshold"
                    )
                ),
            ),
            GateResult(
                "environment_fitness",
                fit_passed,
                value={"score": fitness, "floor": float(lookup.fitness_floor)},
                detail=(
                    ""
                    if fit_passed
                    else (
                        f"environment_fitness failed: score {fitness:.2f} "
                        f"< floor {float(lookup.fitness_floor):.2f}"
                    )
                ),
            ),
            GateResult(
                "ml_veto",
                ml_passed,
                value={
                    "ml_probability": ml_prob,
                    "floor": float(lookup.ml_floor),
                },
                detail=(
                    ""
                    if ml_passed
                    else (
                        f"ml_veto failed: prob {ml_prob:.3f} "
                        f"< {float(lookup.ml_floor):.3f} custom threshold"
                    )
                ),
            ),
            GateResult(
                "cold_start_gap",
                True,
                value={"open": True, "capped": False},
            ),
            GateResult(
                "alpha_matrix_approved",
                bool(lookup.approved),
                value={"approved": bool(lookup.approved)},
                detail="" if lookup.approved else "historical cell not winning",
            ),
        ]

    def _run_tick_alpha_matrix(self, quote: Quote, *, bare_metal: bool = False) -> TickContext:
        """
        Live Vanguard zero-latency path — naked SHM pointer lookup only.

        ``bare_metal=True`` (Thread B): no logging, snapshots, sentinel, or shadow_log.
        """
        from intelligence.matrix_prebaker import (
            COL_APPROVED,
            COL_FITNESS_FLOOR,
            COL_ML_FLOOR,
            COL_SAMPLES,
            COL_SIGNAL_FLOOR,
            COL_WIN_PROB,
            record_lookup_latency_us,
        )
        from intelligence.matrix_lookup_bridge import structural_metrics_from_quote
        from system.ipc.ring_buffer import get_alpha_ring_buffer

        hot = bare_metal or bare_metal_hot_path_active()
        if not hot:
            from intelligence.matrix_lookup_bridge import log_lookup_telemetry

        self._reset_gate_signal_cache()
        if not hot:
            self._reset_tick_memo()
        try:
            self._signal_engine.add_quote(self._market, quote)
        except Exception as e:
            if not hot:
                log_engine(f"signal_engine.add_quote failed: {type(e).__name__}: {e}")
        if not hot:
            try:
                self._refresh_hud_indicators()
            except Exception as e:
                log_engine(f"hud indicator refresh failed: {type(e).__name__}: {e}")

        if self._alpha_matrix_lockout:
            wait_reason = "MEMORY_DEATH_SWITCH: protective lockout 65%"
            ctx = TickContext(
                quote=quote,
                gates=self._offline_gates(wait_reason),
                all_passed=False,
                wait_reason=wait_reason,
            )
            if not hot:
                self._publish_snapshot(ctx)
                with self._lock:
                    self._last_context = ctx
                self._sentinel_on_tick()
            return ctx

        rsi, atr, momentum, direction = structural_metrics_from_quote(
            market=self._market,
            epic=self._epic,
            quote=quote,
            signal_engine=self._signal_engine,
            indicator_snapshot_fn=self._tick_indicator_snapshot,
        )
        live_thr = self._dynamic_live_signal_threshold(atr=atr, rsi=rsi)
        try:
            from trading.dynamic_adaptation import DynamicAdaptationEngine

            base_thr = self._force_live_signal_threshold()
            DynamicAdaptationEngine.refresh_for_epic(
                self._epic,
                base_signal=base_thr,
            )
            live_thr = DynamicAdaptationEngine.effective_signal_threshold(
                self._epic,
                live_thr,
            )
        except Exception as exc:
            log_guarded_exception("trading_loop_dynamic_adapt", exc)
        pattern_index = self._compute_pattern_index(
            epic=self._epic,
            direction=direction,
            rsi=rsi,
            atr=atr,
            momentum=momentum,
        )

        lookup_hit = False
        lookup_approved = False
        cell_index = pattern_index
        signal_floor = 0.0
        fitness_floor = 0.0
        ml_floor = 0.0
        win_probability = 0.0
        samples = 0.0
        wait_reason = ""
        latency_us = 0.0

        if not self._bind_shm_matrix_pointer():
            self._emergency_protective_lockout_65()
            wait_reason = "ALPHA_MATRIX: shm unmapped"
            try:
                from system.gate_activity import record_gate_evaluation

                record_gate_evaluation(self._epic)
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)
        else:
            try:
                import time as _time

                t0 = _time.perf_counter()
                matrix_payload = self._naked_matrix_row_lookup(pattern_index)
                latency_us = (_time.perf_counter() - t0) * 1_000_000.0
                self._last_matrix_lookup_us = latency_us
                if not hot:
                    record_lookup_latency_us(latency_us)
                samples = float(matrix_payload[COL_SAMPLES])
                lookup_hit = samples > 0.0
                lookup_approved = lookup_hit and float(matrix_payload[COL_APPROVED]) >= 0.5
                signal_floor = float(matrix_payload[COL_SIGNAL_FLOOR])
                fitness_floor = float(matrix_payload[COL_FITNESS_FLOOR])
                ml_floor = float(matrix_payload[COL_ML_FLOOR])
                win_probability = float(matrix_payload[COL_WIN_PROB])
                try:
                    exp = (
                        (self._config.as_dict().get("exposure_expansion") or {})
                        if self._config
                        else {}
                    )
                    relief_pct = float(exp.get("alpha_matrix_confidence_relief_pct") or 0)
                    relief_epic = str(exp.get("epic") or "CS.D.EURUSD.CFD.IP")
                    if relief_pct > 0 and str(self._epic).strip() == relief_epic:
                        relief_mult = max(0.5, 1.0 - relief_pct / 100.0)
                        signal_floor *= relief_mult
                        ml_floor *= relief_mult
                        lookup_approved = lookup_hit and (
                            float(matrix_payload[COL_APPROVED]) >= 0.5
                            or float(win_probability) >= (0.5 * relief_mult)
                        )
                except Exception:
                    pass
            except (AttributeError, ValueError, FileNotFoundError, TypeError, IndexError):
                wait_reason = "MEMORY_DEATH_SWITCH: pointer fault"
            except Exception as exc:
                if not hot:
                    log_guarded_exception("trading_loop_alpha_matrix", exc)
                wait_reason = f"ALPHA_MATRIX: {type(exc).__name__}"

        ring = get_alpha_ring_buffer()
        effective_floor = float(signal_floor) if signal_floor > 0 else float(live_thr)
        signal_floor = min(float(live_thr), effective_floor)
        if fitness_floor <= 0.0 or fitness_floor > float(live_thr):
            fitness_floor = float(live_thr)
        try:
            from trading.dynamic_adaptation import StarvationSentinel

            StarvationSentinel.capture_baseline_floors(
                signal_floor, fitness_floor, ml_floor
            )
            signal_floor, fitness_floor, ml_floor = StarvationSentinel.apply_floor_overrides(
                signal_floor, fitness_floor, ml_floor
            )
        except Exception as exc:
            log_guarded_exception("trading_loop_starvation_sentinel", exc)
        matrix_win_injection = bool(lookup_approved)

        if hot:
            confidence = max(
                float(win_probability) * 100.0,
                float(signal_floor),
                float(live_thr),
            )
            signal = SignalResult(
                signal=direction if direction in ("BUY", "SELL") else "WAIT",
                raw_confidence=confidence,
                adjusted_confidence=confidence,
                learning_delta=0.0,
                setup_key=f"bare_metal|{self._epic}",
                notes="bare_metal_matrix",
                snapshot={"atr": atr, "rsi": rsi},
            )
        else:
            signal = self._get_gate_signal()
            confidence = float(signal.adjusted_confidence)
        fitness = float(fitness_floor)

        from types import SimpleNamespace

        lookup = SimpleNamespace(
            hit=lookup_hit,
            approved=lookup_approved,
            cell_index=cell_index,
            signal_floor=signal_floor,
            fitness_floor=fitness_floor,
            ml_floor=ml_floor,
            win_probability=win_probability,
            samples=samples,
            latency_us=latency_us,
            reason="" if lookup_hit else "cell_empty",
            live_threshold=live_thr,
        )
        gates = self._synthetic_alpha_matrix_gates(
            lookup,
            signal,
            confidence=confidence,
            fitness=fitness,
            direction=direction,
            ml_probability=float(win_probability),
        )

        all_passed = bool(lookup_hit) and not self._alpha_matrix_lockout
        if matrix_win_injection:
            all_passed = True
        if self._alpha_matrix_lockout:
            wait_reason = wait_reason or "MEMORY_DEATH_SWITCH: protective lockout 65%"
            all_passed = False
        elif wait_reason:
            all_passed = False
        elif not lookup_hit:
            wait_reason = "ALPHA_MATRIX: miss (empty cell)"
            all_passed = False
        elif not lookup_approved and not matrix_win_injection:
            wait_reason = "ALPHA_MATRIX: historical cell not winning"
            all_passed = False
        elif confidence < float(signal_floor) and not matrix_win_injection:
            wait_reason = (
                f"ALPHA_MATRIX: confidence {confidence:.1f}% "
                f"< floor {signal_floor:.1f}%"
            )
            all_passed = False
        elif signal.signal not in ("BUY", "SELL"):
            wait_reason = f"ALPHA_MATRIX: direction {signal.signal}"
            all_passed = False

        if wait_reason and not all_passed:
            try:
                from harmonization.trade_inhibitor_log import log_trade_inhibitor

                log_trade_inhibitor(
                    epic=str(self._epic or ""),
                    gate="alpha_matrix",
                    reason=wait_reason,
                    metrics={"confidence": f"{confidence:.1f}", "floor": f"{signal_floor:.1f}"},
                )
            except Exception:
                pass

        outcome: TickOutcome | None = None
        if not hot:
            try:
                self._execution_loop.execution_engine.update_positions(
                    self._market, self._epic, quote
                )
            except Exception as e:
                log_engine(f"update_positions failed: {type(e).__name__}: {e}")

        gate_snapshot = {g.name: bool(g.passed) for g in gates}
        if all_passed:
            if not hot:
                from intelligence.matrix_lookup_bridge import log_lookup_telemetry

                log_lookup_telemetry(
                    epic=self._epic,
                    market=self._market,
                    lookup=lookup,
                    direction=direction,
                )
            trade_size = self._trade_size_from_gates(gates, confidence) if not hot else float(
                getattr(self._config, "trade_size", 0.1) or 0.1
            )
            try:
                dispatch_size, under_min_lot = _shield_integer_dispatch_size(trade_size)
                if under_min_lot:
                    if not hot:
                        from apex.hardening import under_min_lot_detail

                        log_engine(under_min_lot_detail(dispatch_size))
                    wait_reason = "HOLD: UNDER_MIN_LOT"
                    all_passed = False
                else:
                    trade_size = float(dispatch_size)
            except Exception as exc:
                if not hot:
                    log_engine(f"[CORE ERROR] alpha matrix dispatch: {exc}")
                wait_reason = f"execution: {type(exc).__name__}"
                all_passed = False

            if all_passed:
                if not hot:
                    log_engine(
                        "ALPHA_MATRIX_PASS — naked pointer dispatch "
                        f"market={self._market} epic={self._epic} "
                        f"dir={direction} conf={confidence:.1f} "
                        f"cell={cell_index} win_p={win_probability:.3f} "
                        f"latency_us={latency_us:.1f}"
                    )
                gate_exec = {
                    "alpha_matrix": True,
                    "gate_sourced": True,
                    "matrix_win_injection": matrix_win_injection,
                    "shadow_brain_injection": matrix_win_injection,
                    "signal_threshold_floor": float(signal_floor),
                    "fitness_min_floor": float(fitness_floor),
                    "ml_veto_min_probability": float(ml_floor),
                    "direction": direction,
                    "size": trade_size,
                    "actual_size": trade_size,
                }
                try:
                    from execution.epic_normalizer import normalize_night_matrix_epic
                    from harmonization.iron_clad_risk import (
                        mandatory_limit_points_for_epic,
                        mandatory_stop_points_for_epic,
                        MAX_ORDER_SIZE,
                    )

                    epic_key = normalize_night_matrix_epic(self._epic)
                    stop_floor = mandatory_stop_points_for_epic(epic_key)
                    limit_floor = mandatory_limit_points_for_epic(epic_key)
                    trade_size = min(float(trade_size), MAX_ORDER_SIZE)
                    stop_pts = max(stop_floor, 1.0)
                    limit_pts = max(limit_floor, stop_pts * 2.0)
                    gate_exec["stop_points"] = stop_pts
                    gate_exec["limit_points"] = limit_pts
                    gate_exec["stop_source"] = "iron_clad_alpha_matrix"
                    gate_exec["actual_size"] = trade_size
                    gate_exec["size"] = trade_size
                except Exception as exc:
                    log_guarded_exception("trading_loop_alpha_gate_exec", exc)
                gate_exec = self._finalize_gate_execution_params(
                    gate_exec, trade_size=trade_size
                )
                try:
                    outcome = self._execution_loop.process_tick(
                        self._market,
                        self._epic,
                        quote,
                        prefetched_signal=signal,
                        gate_execution_params=gate_exec,
                        gate_snapshot=gate_snapshot,
                        shadow_force_fill=False,
                    )
                    if not hot:
                        self._log_execution_outcome(outcome)
                    elif outcome is not None and outcome.execution is not None:
                        from system.unified_fulfillment_cache import (
                            record_execution_performance_row,
                        )

                        exec_res = outcome.execution
                        deal_id = str(
                            getattr(exec_res, "deal_id", "")
                            or getattr(exec_res, "deal_reference", "")
                            or ""
                        ).strip()
                        if not deal_id:
                            pass
                        else:
                            result = (
                                "WIN" if bool(getattr(exec_res, "success", False)) else "LOSS"
                            )
                            entry_px = float(
                                quote.offer if direction == "BUY" else quote.bid
                            )
                            exit_px = float(
                                quote.bid if direction == "BUY" else quote.offer
                            )
                            pnl_gbp = float(getattr(exec_res, "pnl_gbp", 0) or 0)
                            record_execution_performance_row(
                                epic=self._epic,
                                direction=direction,
                                result=result,
                                confidence=confidence,
                                cell_index=cell_index,
                                latency_us=latency_us,
                                deal_id=deal_id,
                                size=float(trade_size),
                                entry=entry_px,
                                exit=exit_px,
                                pnl_gbp=pnl_gbp,
                            )
                    exec_wait = self._execution_wait_reason(outcome)
                    if exec_wait:
                        wait_reason = exec_wait
                        all_passed = False
                except Exception as e:
                    if not hot:
                        log_engine(f"alpha matrix execution failed: {type(e).__name__}: {e}")
                    wait_reason = f"execution: {type(e).__name__}"
                    all_passed = False
        elif wait_reason and not hot:
            log_engine(f"WAIT — {wait_reason}")

        ctx = TickContext(
            quote=quote,
            gates=gates,
            all_passed=all_passed,
            wait_reason=wait_reason,
            signal=signal,
            fitness=fitness,
            outcome=outcome,
        )
        if not hot:
            self._publish_snapshot(ctx)
            with self._lock:
                self._last_context = ctx
            self._sentinel_on_tick()
        else:
            with self._lock:
                self._last_context = ctx
            if hot:
                try:
                    import time as _time

                    from system.unified_fulfillment_cache import record_gate_diagnostics

                    now = _time.monotonic()
                    last = float(getattr(self, "_last_gate_diag_mono", 0.0) or 0.0)
                    if now - last >= 0.4:
                        self._last_gate_diag_mono = now
                        record_gate_diagnostics(
                            epic=self._epic,
                            gates=gates,
                            wait_reason=wait_reason,
                            all_passed=all_passed,
                            tuning={"signal_threshold": float(live_thr)},
                        )
                except Exception:
                    pass
        try:
            from system.gate_activity import record_gate_evaluation

            record_gate_evaluation(self._epic)
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        return ctx

    def _run_tick_core(self) -> TickContext | None:
        try:
            from ig_api.streaming_client import get_network_stable, log_execution_halt

            self.network_stable = get_network_stable()
        except Exception:
            self.network_stable = True
        if not self.network_stable:
            log_execution_halt()
            ctx = TickContext(
                quote=Quote(self._clock(), 0.0, 0.0),
                wait_reason="network blackout",
                all_passed=False,
            )
            ctx.gates = self._offline_gates("network blackout")
            self._publish_snapshot(ctx)
            with self._lock:
                self._last_context = ctx
            self._sentinel_on_tick()
            return ctx

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
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)
            self._publish_snapshot(ctx)
            with self._lock:
                self._last_context = ctx
            self._sentinel_on_tick()
            return ctx

        try:
            from system.soak_live_fire import soak_armed_for_epic

            if soak_armed_for_epic(self._epic):
                return self._run_frontier_tensor_tick(quote)
        except Exception as exc:
            log_guarded_exception("soak_live_fire_redirect", exc)

        try:
            from intelligence.matrix_lookup_bridge import prebaked_alpha_matrix_live_active

            if prebaked_alpha_matrix_live_active():
                return self._run_tick_alpha_matrix(quote)
        except Exception as exc:
            log_guarded_exception("trading_loop_alpha_matrix", exc)

        try:
            from system.market_integrity import check_quote_integrity

            integrity = check_quote_integrity(self._epic, quote)
            if not integrity.allowed:
                reason = integrity.reason or "DATA STALE"
                if "STALE" in reason.upper() or integrity.stream_status == "STALE":
                    try:
                        from ig_api.streaming_client import log_execution_halt

                        log_execution_halt()
                    except Exception as exc:
                        log_guarded_exception("trading_loop", exc)
                ctx = TickContext(
                    quote=quote,
                    wait_reason=reason,
                    all_passed=False,
                )
                ctx.gates = self._offline_gates(reason)
                self._publish_snapshot(ctx)
                with self._lock:
                    self._last_context = ctx
                self._sentinel_on_tick()
                return ctx
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)

        self._tick_count += 1
        try:
            from apex.microkernel import get_microkernel

            get_microkernel().on_tick_ingest(self._epic, quote)
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        try:
            from system.protective_learning import temporary_test_gate_active

            if temporary_test_gate_active():
                from intelligence.intelligence_worker import get_intelligence_worker

                get_intelligence_worker().enqueue_tick(
                    self._epic,
                    bid=float(quote.bid),
                    offer=float(quote.offer),
                )
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        try:
            from system.config_loader import get_config

            self._config = get_config()
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        self._reset_gate_signal_cache()
        self._reset_tick_memo()
        try:
            from system.market_data_hub import get_market_data_hub

            spread_pts = max(0.0, float(quote.offer) - float(quote.bid))
            if spread_pts > 0:
                get_market_data_hub().record_spread(self._epic, spread_pts)
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        self._maybe_refresh_account_balance()
        try:
            self._signal_engine.add_quote(self._market, quote)
        except Exception as e:
            log_engine(f"signal_engine.add_quote failed: {type(e).__name__}: {e}")
        try:
            self._refresh_hud_indicators()
        except Exception as e:
            log_engine(f"hud indicator refresh failed: {type(e).__name__}: {e}")
        try:
            self._session.on_tick(quote)
        except Exception as e:
            log_engine(f"session_manager.on_tick failed: {type(e).__name__}: {e}")

        if self._session.is_session_open():
            self._session_tracker.reset_for_session(self._session.session_open_time)

        self._friday_flatten_if_needed()
        self._flatten_if_needed()

        # Project Apex Monolith Core Circuit Breaker
        try:
            from apex import microkernel
            from system.system_state import BootPhase, get_system_state

            current_boot_phase = get_system_state().snapshot_model().phase
            if (
                not microkernel.is_warmup_complete()
                or current_boot_phase == BootPhase.WARMING
            ):
                log_engine(
                    f"HOLD: WARMING_CIRCUIT_BREAKER | epic={self._epic} "
                    f"market={self._market} phase={current_boot_phase}"
                )
                ctx = TickContext(
                    quote=quote,
                    wait_reason="HOLD: WARMING_CIRCUIT_BREAKER",
                    all_passed=False,
                )
                ctx.gates = self._offline_gates("HOLD: WARMING_CIRCUIT_BREAKER")
                self._publish_snapshot(ctx)
                with self._lock:
                    self._last_context = ctx
                self._sentinel_on_tick()
                return ctx
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)

        gates = self._evaluate_gates(quote)
        try:
            from apex.operational_transparency import (
                record_gate_rejection,
                record_opportunity_scanned,
            )

            record_opportunity_scanned(epic=self._epic)
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        self._log_gate_check(quote, gates)
        self._emit_feeder_telemetry(quote, gates)
        try:
            from system.gate_activity import record_gate_evaluation

            record_gate_evaluation(self._epic)
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        self._maybe_consume_points_skip_on_suppressed_signal(gates)
        natural_all_passed = all(g.passed for g in gates)
        try:
            from trading.gate_funnel_counter import record_sequential_gate_funnel

            record_sequential_gate_funnel(gates)
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        gate_snapshot = {g.name: bool(g.passed) for g in gates}
        shadow_brain = False
        try:
            from intelligence.shadow_brain_loop import shadow_brain_active

            shadow_brain = bool(shadow_brain_active())
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        wait_reason = ""
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
        if shadow_brain:
            try:
                from intelligence.shadow_brain_loop import process_shadow_brain_tick

                process_shadow_brain_tick(
                    epic=self._epic,
                    market=self._market,
                    quote=quote,
                    gates=gates,
                    gate_snapshot=gate_snapshot,
                    signal=signal if isinstance(signal, SignalResult) else None,
                    fitness=fitness,
                )
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)
            if not natural_all_passed:
                failed = next(g for g in gates if not g.passed)
                wait_reason = f"{failed.name}: {failed.detail}"
            ctx = TickContext(
                quote=quote,
                gates=gates,
                all_passed=natural_all_passed,
                wait_reason=wait_reason,
                signal=signal if isinstance(signal, SignalResult) else None,
                fitness=fitness,
                outcome=None,
            )
            self._publish_snapshot(ctx)
            with self._lock:
                self._last_context = ctx
            self._sentinel_on_tick()
            return ctx
        all_passed = natural_all_passed
        if not natural_all_passed:
            failed = next(g for g in gates if not g.passed)
            wait_reason = f"{failed.name}: {failed.detail}"
            sig_conf = 0.0
            for g in gates:
                if g.name == "signal_confidence" and isinstance(g.value, dict):
                    try:
                        sig_conf = float(g.value.get("confidence") or 0)
                    except (TypeError, ValueError):
                        sig_conf = 0.0
                    break
            log_engine(
                f"GATE_TRACE | epic={self._epic} market={self._market} "
                f"block={failed.name} conf={sig_conf:.1f} fitness={fitness:.0f} "
                f"detail={(failed.detail or '')[:100]}"
            )
            try:
                from apex.operational_transparency import record_gate_rejection

                record_gate_rejection(
                    str(failed.name or ""),
                    str(failed.detail or ""),
                    epic=self._epic,
                )
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)
            try:
                from apex.avionics_story import append_avionics_story

                threshold = self._trade_confidence_threshold()
                if failed.name == "signal_confidence":
                    append_avionics_story(
                        f"BLOCKED: {self._market} confidence {sig_conf:.1f}% "
                        f"is under the {threshold:.1f}% entry ceiling",
                        kind="blocked",
                        epic=self._epic,
                    )
                else:
                    append_avionics_story(
                        f"BLOCKED: {failed.name} — {(failed.detail or '')[:120]}",
                        kind="blocked",
                        epic=self._epic,
                    )
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)

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
            dispatch_size = 0
            under_min_lot = False
            try:
                dispatch_size, under_min_lot = _shield_integer_dispatch_size(trade_size)
                if under_min_lot:
                    from apex.hardening import under_min_lot_detail

                    log_engine(under_min_lot_detail(dispatch_size))
                    wait_reason = "HOLD: UNDER_MIN_LOT"
                    all_passed = False
                else:
                    trade_size = float(dispatch_size)
            except Exception as exc:
                log_engine(f"[CORE ERROR] Order dispatcher exception caught: {exc}")
                wait_reason = f"execution: {type(exc).__name__}"
                all_passed = False
            if all_passed:
                self._emit_feeder_order_intent(gates, confidence, trade_size)
                try:
                    from feeder.execution_quote_preflight import refresh_ig_execution_snapshot

                    snap_ok, snap_reason = refresh_ig_execution_snapshot(
                        self._epic, self._config
                    )
                    if not snap_ok:
                        wait_reason = snap_reason
                        all_passed = False
                        self._mark_execution_gate_blocked(gates, snap_reason)
                        log_engine(f"WAIT — {wait_reason}")
                except Exception as exc:
                    log_engine(
                        f"execution snapshot preflight skipped: {type(exc).__name__}: {exc}"
                    )
            if all_passed:
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
                    gate_exec = self._gate_execution_params_from_gates(gates)
                    if gate_exec is None:
                        ind = self._tick_indicator_snapshot(quote)
                        gate_exec = self._iron_clad_fallback_gate_exec(
                            trade_size,
                            atr=float(ind.get("atr") or 0),
                        )
                    gate_exec = self._finalize_gate_execution_params(
                        gate_exec, trade_size=trade_size
                    )
                    outcome = self._execution_loop.process_tick(
                        self._market,
                        self._epic,
                        quote,
                        prefetched_signal=prefetched,
                        gate_execution_params=gate_exec,
                        gate_snapshot=gate_snapshot,
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
            elif wait_reason == "HOLD: UNDER_MIN_LOT":
                log_engine(f"WAIT — {wait_reason}")
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
        self._sentinel_on_tick()
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
                eval_conf = float(sig_gate.value.get("confidence") or adjusted)
                try:
                    from system.risk_bands import threshold_pass_map

                    thresh_map = threshold_pass_map(eval_conf, direction)
                except Exception:
                    thresh_map = {}
                pilot = False
                try:
                    from system.v26_config import pilot_settings

                    pilot = epic == pilot_settings().get("primary_epic")
                except Exception as exc:
                    log_guarded_exception("trading_loop", exc)
                trade_ready = all(g.passed for g in gates)
                signal_actionable = bool(sig_gate.passed)
                first_block = next((g for g in gates if not g.passed), None)
                emit_signal_eval(
                    epic=epic,
                    market=market,
                    session=session,
                    direction=direction,
                    raw_score=raw_score,
                    adjusted_score=eval_conf,
                    setup_key=setup_key or str(sig_gate.value.get("setup") or ""),
                    would_fire=trade_ready,
                    signal_actionable=signal_actionable,
                    blocking_gate=str(first_block.name if first_block else ""),
                    reason=reason,
                    gates_passed=gates_passed,
                    ml_probability=ml_f,
                    threshold_pass=thresh_map or None,
                    risk_band=str(sig_gate.value.get("risk_band") or ""),
                    pilot_epic=pilot,
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
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)

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
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)

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

    def _gate_execution_params_from_gates(
        self, gates: list[GateResult]
    ) -> dict[str, Any] | None:
        """Approved sizing from risk_validation — single source for order submission."""
        from execution.types import freeze_gate_execution_params

        live_state_vector: dict[str, Any] = {}
        vec = self._tick_live_state_vector
        if isinstance(vec, dict):
            live_state_vector = vec
        else:
            for g in gates:
                if g.name == "signal_confidence" and isinstance(g.value, dict):
                    raw_vec = g.value.get("live_state_vector")
                    if isinstance(raw_vec, dict):
                        live_state_vector = raw_vec
                    break

        for g in gates:
            if g.name != "risk_validation" or not g.passed:
                continue
            if not isinstance(g.value, dict):
                continue
            v = g.value
            try:
                stop_pts = float(v.get("stop_points") or 0)
                limit_pts = float(v.get("limit_points") or 0)
            except (TypeError, ValueError):
                continue
            if stop_pts <= 0:
                continue
            if limit_pts <= 0:
                limit_pts = stop_pts * float(self._config.reward_multiple)
            try:
                raw_size = float(v.get("final_size") or v.get("actual_size") or 0)
            except (TypeError, ValueError):
                raw_size = 0.0
            size_int = int(raw_size // 1)
            if size_int < 1:
                continue
            raw = {
                "actual_size": float(size_int),
                "size": float(size_int),
                "final_size": size_int,
                "stop_points": stop_pts,
                "limit_points": limit_pts,
                "stop_source": v.get("stop_source"),
                "risk_gbp": v.get("risk_gbp"),
                "risk_band": v.get("risk_band"),
                "risk_cap_gbp": v.get("risk_cap_gbp"),
                "sizing_confidence": v.get("sizing_confidence"),
            }
            try:
                from execution.adaptive_horizon import classify_execution_horizon

                plan = classify_execution_horizon(
                    live_state_vector,
                    stop_points=stop_pts,
                    cfg=self._config,
                )
                raw.update(plan.to_execution_overlay())
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)
            return freeze_gate_execution_params(raw)
        return None

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
                from apex.operational_transparency import record_executed_trade

                record_executed_trade(epic=self._epic, side=direction)
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)
            try:
                from trading.continuous_optimization_worker import (
                    get_continuous_optimization_worker,
                )

                feat = getattr(self, "_last_feature_payload", None) or {}
                vector = feat.get("vector")
                verdict = getattr(self, "_last_probability_verdict", None)
                win_p = float(getattr(verdict, "win_probability", 0.5) if verdict else 0.5)
                model_v = str(getattr(verdict, "model_verdict", "") if verdict else "")
                exec_obj = getattr(outcome, "execution", None)
                deal_ref = (
                    getattr(exec_obj, "deal_id", None)
                    or getattr(exec_obj, "deal_reference", None)
                    or f"{self._epic}-{time.time_ns()}"
                )
                if vector is not None:
                    get_continuous_optimization_worker().record_execution(
                        deal_ref=str(deal_ref),
                        epic=str(self._epic),
                        direction=direction,
                        win_probability=win_p,
                        feature_vector=vector,
                        model_verdict=model_v,
                    )
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)
            try:
                from apex.ipc_bridge import broadcast_ledger_event

                exec_obj = getattr(outcome, "execution", None)
                params = getattr(getattr(outcome, "trade_signal", None), "gate_execution_params", None) or {}
                if not isinstance(params, dict):
                    params = {}
                broadcast_ledger_event(
                    {
                        "event": "broker_fill",
                        "ts": time.time(),
                        "epic": self._epic,
                        "side": direction,
                        "action": direction,
                        "size": int(
                            float(params.get("final_size") or params.get("actual_size") or 0)
                            // 1
                        ),
                        "entry": float(params.get("entry") or params.get("level") or 0),
                        "deal_id": getattr(exec_obj, "deal_id", None),
                        "deal_reference": getattr(exec_obj, "deal_reference", None),
                        "latency_ms": float(params.get("latency_ms") or 0),
                        "mode": "DEMO",
                        "source": "gate7_dispatch",
                    }
                )
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)
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

    def _refresh_hud_indicators(self) -> None:
        """Priority RSI/ATR refresh for telemetry — runs every tick, outside gate cooldown."""
        refresh = getattr(self._signal_engine, "refresh_hud_indicators", None)
        if callable(refresh):
            refresh(self._market)

    def _evaluate_gates(self, quote: Quote) -> list[GateResult]:
        from ai.operational.profiler_hooks import probe_hot_path

        cooldown = _resolve_gate_eval_cooldown_sec()
        used_cache = False
        gate_us: dict[str, float] = {}
        total_us = 0.0

        with self._gate_eval_lock:
            now = time.monotonic()
            last_ts = self._last_gate_eval_time
            cached = self._last_gate_eval_results
            if (
                cached is not None
                and last_ts > 0.0
                and (now - last_ts) < cooldown
            ):
                results = list(cached)
                used_cache = True
            else:
                t0 = time.perf_counter()
                with probe_hot_path("probe_gate_evaluation", epic=self._epic):
                    results = self._evaluate_gates_core(quote, gate_us=gate_us)
                total_us = (time.perf_counter() - t0) * 1_000_000.0
                self._last_gate_eval_time = now
                self._last_gate_eval_results = list(results)

        # Risk/spread gate uses live bid/offer every tick — never cooldown-cached.
        risk_gate = self._gate_risk_validation(quote)
        results = self._replace_gate_result(results, risk_gate)

        with self._gate_eval_lock:
            if self._last_gate_eval_results is not None:
                self._last_gate_eval_results = self._replace_gate_result(
                    list(self._last_gate_eval_results), risk_gate
                )

        if not used_cache:
            try:
                from system.diagnostics.perf_metrics import record_tick_gate_evaluation

                record_tick_gate_evaluation(
                    self._epic, total_us=total_us, gate_us=gate_us
                )
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)
        return results

    def _evaluate_gates_core(
        self, quote: Quote, *, gate_us: dict[str, float] | None = None
    ) -> list[GateResult]:
        try:
            from system.agent_execution_mode import (
                demo_sandbox_unblock_active,
                ensure_demo_sandbox_execution_armed,
            )

            if demo_sandbox_unblock_active():
                ensure_demo_sandbox_execution_armed()
                self.clear_entry_circuit_breaker()
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)

        try:
            from system.agent_execution_mode import demo_sandbox_unblock_active
            from system.manual_kill_monitor import is_master_kill_block_active

            if not demo_sandbox_unblock_active() and is_master_kill_block_active():
                return self._hard_block_all_gates(
                    "MASTER_KILL_SWITCH_ACTIVE",
                    primary_gate="broker_feed",
                )
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)

        try:
            from system.agent_execution_mode import demo_sandbox_unblock_active
            from system.qmm_process_supervisor import process_entry_blocked

            blocked, reason = process_entry_blocked()
            if blocked and not demo_sandbox_unblock_active():
                return self._hard_block_all_gates(reason, primary_gate="broker_feed")
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)

        breaker = self.entry_circuit_breaker()
        try:
            from system.agent_execution_mode import demo_sandbox_unblock_active

            if breaker and demo_sandbox_unblock_active():
                if breaker == "MASTER_KILL_SWITCH_ACTIVE" or "Circuit breaker" in breaker:
                    self.clear_entry_circuit_breaker()
                    breaker = ""
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        if breaker:
            return self._hard_block_all_gates(breaker, primary_gate="broker_feed")

        rotation = self._gate_active_rotation()
        if not rotation.passed:
            blocked = GateResult(
                name="active_rotation",
                passed=False,
                value=rotation.value,
                detail=SOFT_BLOCK_NOT_IN_TOP_3,
            )
            # Keep session_open / session_blackout accurate for dashboard market_state.
            results: list[GateResult] = [
                blocked,
                self._gate_session_open(),
                self._gate_session_blackout(),
            ]
            for name in GATE_NAMES:
                if name in ("session_open", "session_blackout"):
                    continue
                results.append(
                    GateResult(
                        name=name,
                        passed=False,
                        value=None,
                        detail=SOFT_BLOCK_NOT_IN_TOP_3,
                    )
                )
            return results

        from system.market_data_hub import get_market_data_hub

        current_spread = float(
            quote.get("spread", 0.0)
            if isinstance(quote, dict)
            else getattr(quote, "spread", 0.0)
        )
        shield_passed, rr_ratio_delta = (
            get_market_data_hub().verify_liquidity_shield_delta(
                self._epic, current_spread
            )
        )
        if not shield_passed:
            from system.gate_activity import record_liquidity_shield_block

            record_liquidity_shield_block(epic=self._epic)
            log_engine(
                f"LIQUIDITY_SHIELD_BLOCKED | epic={self._epic} spread={current_spread:.2f} "
                f"ratio={rr_ratio_delta:.2f}x (>3.5x baseline)"
            )
            return [
                GateResult(
                    name="risk_validation",
                    passed=False,
                    detail="BLOCKED_MULTI_BROKER_LIQUIDITY_SHIELD",
                )
            ]

        # Use ATR in price points from the signal snapshot — not environment
        # fitness factor scores (0–30), which caused false spread/ATR blocks.
        current_atr = 0.0
        try:
            sig = self._get_gate_signal()
            current_atr = _atr_from_signal_snapshot(sig.snapshot or {})
        except Exception:
            current_atr = 0.0
        if current_atr > 0.0 and current_spread > 0.0:
            spread_to_atr_ratio = current_spread / current_atr
            spread_atr_max = self._spread_to_atr_circuit_max()
            try:
                from system.agent_execution_mode import demo_sandbox_unblock_active

                if demo_sandbox_unblock_active():
                    spread_atr_max = max(spread_atr_max, 999.0)
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)
            if spread_to_atr_ratio > spread_atr_max:
                from system.qmm_process_supervisor import set_process_entry_block

                detail = BLOCKED_SPREAD_TO_ATR_CIRCUIT_BREAKER
                self.set_entry_circuit_breaker(detail)
                set_process_entry_block(detail)
                log_engine(
                    f"CIRCUIT_BREAKER_ACTIVE | epic={self._epic} "
                    f"spread/atr={spread_to_atr_ratio:.2f} "
                    f"(>{spread_atr_max:.2f}) - Locking entry gates."
                )
                return self._hard_block_all_gates(
                    detail, primary_gate="risk_validation"
                )

        # Gate 11 (ml_veto) can emit a sizing multiplier that Gate 7
        # (risk_validation) must apply on the same tick. We therefore evaluate
        # signal_confidence + ml_veto before risk_validation.
        results: list[GateResult] = []
        self._publish_ml_sizing_multiplier(1.0)
        gate_order = (
            "session_open",
            "session_blackout",
            "cold_start_gap",
            "environment_fitness",
            "points_state",
            "correlation_ok",
            "signal_confidence",
            "ml_veto",
            "risk_validation",
            "expectancy_ok",
            "calendar_ok",
            "execution",
        )
        for name in gate_order:
            import time as _time

            _g0 = _time.perf_counter()
            try:
                if name == "session_open":
                    results.append(self._gate_session_open())
                elif name == "session_blackout":
                    results.append(self._gate_session_blackout())
                elif name == "cold_start_gap":
                    results.append(self._gate_cold_start_gap(quote))
                elif name == "environment_fitness":
                    results.append(self._gate_environment_fitness(quote))
                elif name == "points_state":
                    results.append(self._gate_points_state())
                elif name == "correlation_ok":
                    results.append(self._gate_correlation_ok())
                elif name == "risk_validation":
                    results.append(self._gate_risk_validation(quote))
                elif name == "expectancy_ok":
                    results.append(self._gate_expectancy_ok())
                elif name == "calendar_ok":
                    results.append(self._gate_calendar_ok())
                elif name == "signal_confidence":
                    results.append(self._gate_signal_confidence(quote))
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
            finally:
                if gate_us is not None:
                    gate_us[name] = (_time.perf_counter() - _g0) * 1_000_000.0
        return results

    def _spread_to_atr_circuit_max(self) -> float:
        return spread_to_atr_circuit_max(self._config, self._epic)

    def _rotation_grace_cycles(self) -> int:
        from runtime.market_orchestrator import ROTATION_GRACE_CYCLES

        try:
            v = self._config.get("rotation_grace_cycles")
            return int(v) if v is not None else ROTATION_GRACE_CYCLES
        except (TypeError, ValueError):
            return ROTATION_GRACE_CYCLES

    def _gate_active_rotation(self) -> GateResult:
        try:
            from system.gate_relaxation import rotation_filter_bypassed

            if rotation_filter_bypassed():
                return GateResult(
                    name="active_rotation",
                    passed=True,
                    value={"bypass": True, "demo_soak": True},
                    detail="rotation filter bypassed (demo soak)",
                )
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        if not self._config.get("enforce_top3_rotation_filter", True):
            return GateResult(
                name="active_rotation",
                passed=True,
                value={"bypass": True},
                detail="rotation filter bypassed (config)",
            )
        from runtime.market_orchestrator import TOP_ROTATION_SLOTS, MarketOrchestrator

        active = MarketOrchestrator.get_global_active_epics()
        if len(active) < TOP_ROTATION_SLOTS:
            return GateResult(
                name="active_rotation",
                passed=True,
                value={"active_epics": active},
                detail="rotation filter inactive (<3 markets)",
            )
        grace_cycles = self._rotation_grace_cycles()
        in_active = self._epic in active
        if in_active:
            self._rotation_grace_remaining = grace_cycles
            passed = True
            detail = f"in top-{len(active)} rotation"
        elif self._rotation_grace_remaining > 0:
            self._rotation_grace_remaining -= 1
            passed = True
            detail = (
                f"rotation grace ({self._rotation_grace_remaining} cycles until mute)"
            )
        else:
            passed = False
            detail = NOT_IN_TOP_3_VOLATILITY_ROTATION
        return GateResult(
            name="active_rotation",
            passed=passed,
            value={
                "active_epics": active,
                "epic": self._epic,
                "grace_remaining": self._rotation_grace_remaining,
            },
            detail=detail,
        )

    def _gate_session_open(self) -> GateResult:
        try:
            from system.agent_execution_mode import demo_sandbox_unblock_active

            if demo_sandbox_unblock_active():
                return GateResult(
                    name="session_open",
                    passed=True,
                    value={"open": True, "demo_forced": True},
                    detail="DEMO sandbox — market forced open",
                )
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        from system.market_data_hub import get_market_data_hub
        from system.market_watch.market_status_updater import (
            cached_market_open,
            ensure_market_status_updater_started,
            get_cached_market_status,
        )

        at = quote_time(self._clock())
        phase = self._session.snapshot().phase
        hub_maint = get_market_data_hub().is_in_maintenance(self._epic)
        ensure_market_status_updater_started(
            epics=[self._epic],
            rest_client=self._rest_client(),
        )
        cached_open = cached_market_open(self._epic)
        if cached_open is not None:
            open_now = bool(cached_open)
        else:
            # Cold cache — local SessionManager state only (no sync REST/calendar).
            open_now = bool(getattr(self._session, "_session_open", False))
            if not open_now:
                try:
                    open_now = str(phase or "").upper() not in (
                        "CLOSED",
                        "MAINTENANCE",
                    )
                except Exception:
                    open_now = False
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
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)
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
                            bypass_whitelist = False
                            try:
                                from intelligence.premium_overnight import (
                                    is_premium_overnight_epic,
                                    night_matrix_session_allowed,
                                )

                                if is_premium_overnight_epic(
                                    self._epic, self._config
                                ):
                                    allowed, block_reason = (
                                        night_matrix_session_allowed(
                                            self._epic,
                                            config=self._config,
                                            now=at,
                                        )
                                    )
                                    if allowed:
                                        bypass_whitelist = True
                                        detail = (
                                            "market open (premium overnight 24/7)"
                                        )
                                    elif block_reason:
                                        open_now = False
                                        detail = block_reason
                            except Exception as exc:
                                log_guarded_exception("trading_loop", exc)
                            if not bypass_whitelist and open_now:
                                open_now = False
                                detail = (
                                    f"Outside allowed trading session "
                                    f"(current={sess})"
                                )
                except Exception as exc:
                    log_guarded_exception("trading_loop", exc)
        next_open_iso = ""
        if not open_now:
            try:
                ms = get_cached_market_status(self._epic)
                if ms and ms.next_open_at:
                    # Market physically closed — use cached next open
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
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)
        return GateResult(
            name="session_open",
            passed=open_now,
            value={"open": open_now, "next_open": next_open_iso},
            detail=detail,
        )

    def _gate_session_blackout(self) -> GateResult:
        try:
            from system.agent_execution_mode import demo_sandbox_unblock_active

            if demo_sandbox_unblock_active():
                return GateResult(
                    name="session_blackout",
                    passed=True,
                    value={"blocked": False, "demo_forced": True},
                    detail="DEMO sandbox — weekend blackout bypassed",
                )
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        from trading.entry_protection import check_session_blackout

        blocked, reason = check_session_blackout(
            self._epic,
            self._config,
            market=self._market,
        )
        if blocked:
            return GateResult(
                name="session_blackout",
                passed=False,
                value={"blocked": True, "reason": reason},
                detail=f"outside trading window ({reason})",
            )
        return GateResult(
            name="session_blackout",
            passed=True,
            value={"blocked": False},
            detail="within trading window",
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
                "gate_min": int(round(self._effective_fitness_gate_min())),
                "capped_cold_start": bool(last.capped_cold_start),
                "capped_gap_open": bool(last.capped_gap_open),
                "session_style": str(
                    getattr(last, "session_style", None) or "WESTERN_MOMENTUM"
                ),
                "fallback_active": bool(getattr(last, "fallback_active", False)),
                "sentiment": sentiment,
            }
        except Exception:
            return {}

    def _points_state_snapshot(self) -> dict[str, Any]:
        """Safe points state for gate relaxation — bare-metal loops may omit _points."""
        points = getattr(self, "_points", None)
        if points is None:
            return {"state": "HEALTHY", "blocked": False}
        try:
            return points.get_state()
        except Exception as exc:
            log_guarded_exception("trading_loop_points_state", exc)
            return {"state": "HEALTHY", "blocked": False}

    def _effective_fitness_gate_min(self) -> float:
        fitness_min = resolve_strictness(
            self._config, signal_engine=self._signal_engine, market=self._market
        ).fitness_floor
        try:
            from system.gate_relaxation import effective_fitness_min

            fitness_min = max(
                fitness_min,
                effective_fitness_min(
                    self._epic,
                    points_state=self._points_state_snapshot(),
                ),
            )
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        if session_validation_capture_active():
            return min(float(fitness_min), SESSION_VALIDATION_CONFIDENCE_FLOOR)
        try:
            from trading.dynamic_adaptation import DynamicAdaptationEngine

            fitness_min = DynamicAdaptationEngine.effective_fitness_min(
                self._epic,
                float(fitness_min),
            )
        except Exception as exc:
            log_guarded_exception("trading_loop_dynamic_adapt_fitness", exc)
        return float(fitness_min)

    def _gate_environment_fitness(self, quote: Quote) -> GateResult:
        try:
            from execution.scalping.config import is_scalping_enabled

            if is_scalping_enabled(self._config):
                return GateResult(
                    name="environment_fitness",
                    passed=True,
                    value={
                        "bypass": True,
                        "display": "scalping",
                        "reason": "scalping entry path",
                    },
                    detail="environment fitness bypassed (scalping framework)",
                )
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        if not self._config.get("enforce_environment_fitness_filter", True):
            return GateResult(
                name="environment_fitness",
                passed=True,
                value={"bypass": True, "display": "bypass"},
                detail="environment fitness filter bypassed (config)",
            )
        score_error = ""
        try:
            quote_df = self._signal_engine.quote_df(self._market)
            score = float(self._env.score(self._market, quote=quote, quote_df=quote_df))
        except Exception as e:
            score_error = f"{type(e).__name__}: {e}"
            log_engine(
                f"environment_fitness gate: score failed for {self._market}: "
                f"{score_error}"
            )
            try:
                from system.learning_demo_policy import learning_demo_enabled

                fail_closed = learning_demo_enabled()
            except Exception:
                fail_closed = True
            if fail_closed:
                return GateResult(
                    name="environment_fitness",
                    passed=False,
                    value={"score": 0, "error": score_error},
                    detail=f"environment scorer failed — entry blocked ({score_error})",
                )
            score = float(SAFE_DEFAULT_SCORE)
        score_int = int(round(score))
        fitness_min = self._effective_fitness_gate_min()
        passed = score >= fitness_min
        sent = {}
        if hasattr(self._env, "get_sentiment_factor"):
            try:
                sent = self._env.get_sentiment_factor(self._market)
            except Exception:
                sent = {}
        sent_label = str(sent.get("label") or "")
        detail = f"fitness {score_int}% (need >={int(fitness_min)}%)"
        if sent_label and sent_label != "neutral":
            detail += f" — {sent_label}"
        factors_payload = self._fitness_factors_payload()
        return GateResult(
            name="environment_fitness",
            passed=passed,
            value={
                "score": score_int,
                "display": f"{score_int}%",
                "fitness_min": int(round(fitness_min)),
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
        from datetime import date

        today = date.today().isoformat()
        if getattr(self, "_daily_loss_alert_day", "") != today:
            self._daily_loss_alert_day = today
            self._daily_loss_alert_sent = False
            self._daily_soft_pause_alert_sent = False

        state = self._points.get_state()
        paused = self._points.is_session_paused()
        from system.daily_loss_policy import daily_loss_gate_status

        loss_ok, loss_detail, loss_meta = daily_loss_gate_status(
            self._store, self._config
        )
        from trading.manual_intervention import entries_blocked_by_shield

        shield_blocked, shield_reason = entries_blocked_by_shield(
            self._store, self._config
        )
        passed = state != "STOP" and not paused and loss_ok and not shield_blocked
        if state == "STOP":
            detail = "points state STOP"
        elif paused:
            n = self._points.session_skips_remaining()
            detail = (
                f"session pause — skip {n} actionable signal(s) "
                f"(BUY/SELL that would have fired)"
            )
        elif shield_blocked:
            detail = shield_reason
        elif not loss_ok:
            detail = loss_detail
            tier = str(loss_meta.get("tier") or "")
            if tier == "hard" and not getattr(self, "_daily_loss_alert_sent", False):
                self._daily_loss_alert_sent = True
                try:
                    from system.alert_dispatcher import enqueue_critical_alert

                    enqueue_critical_alert(
                        "🛑 Drawdown limit hit — trading halted",
                        dedupe_key="daily_loss_hard",
                    )
                except Exception as e:
                    log_engine(
                        f"telegram daily-loss alert enqueue failed: "
                        f"{type(e).__name__}: {e}"
                    )
            elif tier == "soft" and not getattr(
                self, "_daily_soft_pause_alert_sent", False
            ):
                self._daily_soft_pause_alert_sent = True
                try:
                    from system.alert_dispatcher import enqueue_critical_alert

                    enqueue_critical_alert(
                        f"⚠️ Daily soft pause — {loss_detail} (v29.1 entries blocked)",
                        dedupe_key="daily_loss_soft",
                    )
                except Exception as e:
                    log_engine(
                        f"telegram soft-pause alert enqueue failed: "
                        f"{type(e).__name__}: {e}"
                    )
        else:
            detail = f"points {state} — {loss_detail}"
        return GateResult(
            name="points_state",
            passed=passed,
            value={"state": state, **loss_meta},
            detail=detail,
        )

    def _maybe_refresh_account_balance(self) -> None:
        try:
            from trading.points_engine import hub_equity_blind_override_active

            if hub_equity_blind_override_active():
                return
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
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
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)

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

    def _gate_correlation_ok(self) -> GateResult:
        from execution.correlation_guard import check_open_book_limits

        sig = self._get_gate_signal()
        direction = str(sig.signal or "WAIT").upper()
        if direction not in ("BUY", "SELL"):
            return GateResult(
                name="correlation_ok",
                passed=True,
                value="no_signal",
                detail="no directional signal",
            )
        tracker = self._execution_loop.execution_engine.trade_tracker
        snap = tracker.snapshot() if tracker is not None else {}
        positions = snap.get("positions") if isinstance(snap, dict) else []
        if not isinstance(positions, list):
            positions = []
        ok, detail = check_open_book_limits(
            self._epic,
            direction,
            positions,
        )
        return GateResult(
            name="correlation_ok",
            passed=ok,
            value={
                "direction": direction,
                "open_total": len(positions),
            },
            detail=detail or "correlation limits OK",
        )

    def _execution_stop_distance(
        self,
        *,
        setup_key: str,
        planning_conf: float,
        snapshot: dict[str, Any],
    ) -> tuple[float, str]:
        """Match LiveExecutor stopDistance — AdaptiveEngine ATR risk when enabled."""
        stop_source = "config_fallback"
        stop = 0.0
        try:
            adaptive = self._execution_loop.execution_engine._adaptive
            exec_settings = adaptive.settings(
                str(setup_key or ""),
                float(planning_conf),
                snapshot if snapshot else None,
            )
            stop = float(exec_settings.get("risk") or 0)
            if stop > 0:
                stop_source = "adaptive_atr"
            elif getattr(self._config, "adaptive_atr_risk_enabled", False):
                atr_val = float(exec_settings.get("atr") or 0)
                mult = float(getattr(self._config, "atr_multiplier", 2.5) or 2.5)
                if atr_val > 0 and mult > 0:
                    stop = atr_val * mult
                    stop_source = "adaptive_atr_direct"
        except (AttributeError, TypeError, ValueError) as exc:
            log_guarded_exception("trading_loop", exc)
        if stop <= 0:
            stop = float(
                self._config.default_stop_distance_points
                or self._config.stop_distance_points
            )
            stop_source = "config_fallback"
        return stop, stop_source

    def _gate_risk_validation(self, quote: Quote) -> GateResult:
        from apex.hardening import floor_contract_size
        from execution.atomic_gateway import (
            locked_per_asset_cap_gbp,
            locked_portfolio_ceiling_gbp,
            locked_session_equity_gbp,
        )

        session_equity_gbp = locked_session_equity_gbp()
        max_risk_cap_override = locked_per_asset_cap_gbp()
        _portfolio_ceiling_gbp = locked_portfolio_ceiling_gbp()
        _ = session_equity_gbp
        _ = _portfolio_ceiling_gbp
        from execution.market_suspension import gate_detail, is_blocked
        from system.market_data_hub import get_market_data_hub
        from trading.points_engine import (
            check_global_portfolio_risk,
            global_portfolio_risk_ceiling_gbp,
            per_asset_risk_cap_gbp,
        )

        if is_blocked():
            detail = gate_detail() or "Market suspended"
            return GateResult(
                name="risk_validation",
                passed=False,
                value={"market_suspended": True},
                detail=detail,
            )

        try:
            from system.paths import data_dir
            from system.portfolio_envelope import rehydrate

            flush_flag = data_dir() / "state" / "portfolio_risk_flush.flag"
            if flush_flag.exists():
                rehydrate(concurrent_risk_gbp=0.0, daily_deployed_gbp=0.0)
                flush_flag.unlink(missing_ok=True)
                log_engine(
                    "sector override: portfolio concurrent risk flushed to zero baseline"
                )
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)

        spread = max(0.0, float(quote.offer) - float(quote.bid))
        live_spread = spread
        cfg_normal = float(self._config.max_spread_points)
        normal = get_market_data_hub().normal_spread(self._epic, fallback=cfg_normal)
        ooh_scale = self._out_of_hours_spread_scale(at=self._clock())
        spread_multiplier = SPREAD_NORMAL_MULTIPLIER * ooh_scale
        spread_cap = normal * spread_multiplier
        v2_spread_meta: dict[str, Any] = {}
        try:
            snap_risk = dict(self._signal_engine.last_snapshot.get(self._market) or {})
            last_risk = snap_risk.get("last") or {}
            _atr_risk = float(last_risk.get("atr", 0) or 0)
            from harmonization.volatility_gate import dynamic_entry_spread_cap

            spread_cap = dynamic_entry_spread_cap(
                epic=str(self._epic or ""),
                normal_spread=float(normal),
                spread_multiplier=float(spread_multiplier),
                atr=_atr_risk,
            )
            try:
                from platform_v2 import platform_v2_enabled

                if platform_v2_enabled():
                    from platform_v2.adaptive_volatility_scalping import (
                        apply_v2_entry_gateway,
                    )

                    spread_cap, v2_spread_meta = apply_v2_entry_gateway(
                        epic=str(self._epic or ""),
                        normal_spread=float(normal),
                        spread_multiplier=float(spread_multiplier),
                        atr=_atr_risk,
                        live_spread=spread,
                        bid=float(quote.bid),
                        offer=float(quote.offer),
                    )
            except Exception as exc:
                log_guarded_exception("trading_loop_v2_spread", exc)
        except Exception as exc:
            log_guarded_exception("trading_loop_spread_cap", exc)
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
        try:
            from execution.correlation_guard import _max_open_positions_global

            max_open_total = min(max_open_total, _max_open_positions_global())
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        total_raw = tracker.count_open_total()
        if isinstance(total_raw, (int, float)):
            open_total = max(open_count, int(total_raw))
        else:
            open_total = open_count
        epic_slot_ok = open_count < max_per_epic
        total_slot_ok = open_total < max_open_total
        position_ok = epic_slot_ok and total_slot_ok

        from trading.points_engine import CONF_MARGINAL_MIN

        sig_for_risk = self._get_gate_signal()
        sizing_conf = float(sig_for_risk.adjusted_confidence or 0)
        threshold_floor = float(self._points.trade_confidence_threshold(self._config))
        if sizing_conf <= 0:
            try:
                from system.risk_bands import bands_enabled, entry_confidence_floor

                sizing_conf = (
                    entry_confidence_floor()
                    if bands_enabled()
                    else threshold_floor
                )
            except Exception:
                sizing_conf = threshold_floor
        planning_conf = max(threshold_floor, sizing_conf)

        snapshot = dict(self._signal_engine.last_snapshot.get(self._market) or {})
        stop, stop_source = self._execution_stop_distance(
            setup_key=str(sig_for_risk.setup_key or ""),
            planning_conf=planning_conf,
            snapshot=snapshot,
        )

        base_size = float(self._config.trade_size)
        point_value = float(self._config.get("ig_point_value_gbp", 1.0))
        hub = get_market_data_hub()
        usd_gbp_rate = 1.0
        risk_point_value_gbp = point_value
        if _epic_requires_usd_gbp_risk_conversion(self._epic):
            # Extract live conversion scalar to prevent broker 3006 margin rejections
            gbpusd_snap = hub.get_snapshot(_GBPUSD_FX_EPIC)
            try:
                raw_bid = float(gbpusd_snap.bid) if gbpusd_snap is not None else 0.0
            except (TypeError, ValueError, AttributeError):
                raw_bid = 0.0
            usd_gbp_rate = (1.0 / raw_bid) if raw_bid > 0 else _USD_GBP_RATE_FALLBACK
            risk_point_value_gbp = point_value * usd_gbp_rate
        size_mult = float(self._points.get_size_multiplier(planning_conf))
        corr_mult = 1.0
        corr_density = 0
        corr_detail = ""
        try:
            from execution.correlation_matrix import correlation_density_risk_multiplier

            snap = tracker.snapshot() if tracker is not None else {}
            positions = (
                snap.get("positions") if isinstance(snap, dict) else []
            ) or []
            corr_mult, corr_density, corr_detail = correlation_density_risk_multiplier(
                self._epic,
                positions if isinstance(positions, list) else [],
            )
            size_mult *= float(corr_mult)
        except Exception as e:
            log_engine(
                f"correlation_matrix sizing skipped epic={self._epic}: "
                f"{type(e).__name__}: {e}"
            )

        # Gate 11 ml_veto risk scaling (marginal probabilities → downscale
        # sizing instead of hard-blocking the entry path).
        ml_mult = self._ml_sizing_multiplier_from_live_state()
        if ml_mult != 1.0:
            size_mult *= ml_mult
        risk_band_label = ""
        risk_band_note = ""
        effective_size = max(
            float(self._config.adaptive_min_trade_size),
            min(
                float(self._config.adaptive_max_trade_size),
                base_size * size_mult,
            ),
        )
        v2_escalation_meta: dict[str, Any] = {}
        try:
            from platform_v2 import platform_v2_enabled

            if platform_v2_enabled():
                from platform_v2.compound_profit_escalation import (
                    apply_compound_escalation,
                )

                esc = apply_compound_escalation(
                    effective_size,
                    session_equity_gbp=session_equity_gbp,
                )
                effective_size = esc.size
                v2_escalation_meta = {
                    "tier_multiplier": esc.tier_multiplier,
                    "net_profit_gbp": esc.net_profit_gbp,
                    "profit_step": esc.profit_step,
                    "defensive_reset": esc.defensive_reset,
                    "reason": esc.reason,
                }
        except Exception as exc:
            log_guarded_exception("trading_loop_v2_escalation", exc)
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

        # Hard Overwrite: £10k session profile — per-asset net risk cap (£350 session).
        risk_cap = max_risk_cap_override

        # Auto-clip size to risk cap rather than hard-blocking the trade.
        size_was_clipped = False
        if risk_point_value_gbp > 0 and stop > 0 and risk_cap > 0:
            max_size_by_risk = risk_cap / (stop * risk_point_value_gbp)
            if actual_size > max_size_by_risk:
                increment = ig_min_size if ig_min_size > 0 else 0.01
                clipped = math.floor(max_size_by_risk / increment) * increment
                if clipped >= ig_min_size:
                    actual_size = clipped
                    size_was_clipped = True
                # else: even min size exceeds cap — leave actual_size as-is so the
                # risk check below fires with a clear log message.

        try:
            from system.risk_bands import apply_risk_band_to_size, bands_enabled

            if bands_enabled() and sizing_conf > 0:
                banded, risk_band_label, risk_band_note = apply_risk_band_to_size(
                    actual_size,
                    confidence=sizing_conf,
                    stop_pts=stop,
                    point_value_gbp=point_value,
                    epic_risk_cap_gbp=risk_cap,
                )
                if banded > 0:
                    actual_size = max(banded, ig_min_size)
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)

        from execution.size_floors import apply_operational_size_floor
        from trading.micro_lot_verification import clamp_micro_lot_size

        actual_size = clamp_micro_lot_size(actual_size)
        actual_size = apply_operational_size_floor(actual_size, self._epic)

        atr_pts = 0.0
        last_row = snapshot.get("last")
        if last_row is not None:
            try:
                atr_pts = float(
                    last_row.get("atr", 0)
                    if hasattr(last_row, "get")
                    else last_row["atr"]
                )
            except (TypeError, ValueError, KeyError):
                atr_pts = 0.0
        from execution.economic_check import apply_atr_protect_envelope

        stop, actual_size, atr_meta = apply_atr_protect_envelope(
            stop_pts=stop,
            size=actual_size,
            atr_pts=atr_pts,
            point_value_gbp=point_value,
            snapshot=snapshot,
            setup_key=str(sig_for_risk.setup_key or ""),
            direction=str(sig_for_risk.signal or ""),
            epic=self._epic,
        )
        if atr_meta.get("atr_protect_active"):
            stop_source = "atr_protect_exhaustion"

        # IG CFD integer contract sizing — int(size // 1); no fractional lots to broker API.
        calculated_size = 0.0
        final_size = 0
        under_min_lot = False
        try:
            calculated_size = float(actual_size)
            final_size, under_min_lot = _shield_integer_dispatch_size(
                calculated_size,
                min_lot=int(float(self._config.adaptive_min_trade_size) // 1) or 1,
            )
            actual_size = float(final_size)
        except Exception as exc:
            log_engine(f"[CORE ERROR] Order dispatcher exception caught: {exc}")
            return GateResult(
                name="risk_validation",
                passed=False,
                detail=f"execution shield: {type(exc).__name__}",
            )
        min_lot = float(self._config.adaptive_min_trade_size)

        try:
            from intelligence.target_engine import get_target_engine

            te = get_target_engine()
            if te.enabled and (te.capital_preservation or te.mission_accomplished):
                return GateResult(
                    name="risk_validation",
                    passed=False,
                    detail="TARGET_ACHIEVED_CAPITAL_PRESERVATION",
                    value={"target_daily_gbp": te.target_daily_gbp},
                )
            factor = float(getattr(te, "last_factor", 1.0) or 1.0)
            if factor < 1.0 and final_size > 0:
                compressed = max(1, int(final_size * factor))
                if compressed < final_size:
                    final_size = compressed
                    actual_size = float(final_size)
                    size_was_clipped = True
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)

        total_trade_cost_gbp = (stop + live_spread) * final_size * risk_point_value_gbp
        if _epic_requires_usd_gbp_risk_conversion(self._epic):
            calculated_risk_usd = (stop + live_spread) * final_size * point_value
            effective_risk_gbp = calculated_risk_usd * usd_gbp_rate
        else:
            calculated_risk_usd = (stop + live_spread) * final_size * point_value
            effective_risk_gbp = total_trade_cost_gbp
        risk_gbp = effective_risk_gbp
        effective_risk_cap = float(risk_cap)
        if risk_band_label == "probe":
            try:
                from system.risk_bands import probe_risk_target_gbp

                effective_risk_cap = float(probe_risk_target_gbp(sizing_conf) * 1.05)
            except Exception:
                effective_risk_cap = 80.0
        try:
            from system.protective_learning import (
                apply_temporary_test_risk_cap_gbp,
                log_temporary_test_execution_bypass_once,
                temporary_test_gate_active,
            )

            if temporary_test_gate_active():
                log_temporary_test_execution_bypass_once()
                effective_risk_cap = apply_temporary_test_risk_cap_gbp(effective_risk_cap)
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        effective_risk_cap = max_risk_cap_override
        risk_ok = (not under_min_lot) and (
            total_trade_cost_gbp <= max_risk_cap_override
        )

        portfolio_ok = True
        portfolio_detail = ""
        try:
            from system.portfolio_envelope import portfolio_gate_enabled, snapshot

            if portfolio_gate_enabled():
                concurrent = float(
                    (snapshot() or {}).get("concurrent_risk_gbp") or 0.0
                )
                portfolio_ok, portfolio_detail = check_global_portfolio_risk(
                    concurrent, risk_gbp
                )
                if portfolio_ok:
                    try:
                        from data.learning_store import LearningStore
                        from execution.portfolio_hooks import (
                            reconcile_portfolio_orphan_reservations,
                        )
                        from system.config_loader import get_config
                        from system.portfolio_envelope import can_allocate

                        reconcile_portfolio_orphan_reservations(
                            LearningStore(str(get_config().learning_db)),
                            cfg=self._config,
                            open_position_count=open_total,
                        )
                        concurrent = float(
                            (snapshot() or {}).get("concurrent_risk_gbp") or 0.0
                        )
                        portfolio_ok, portfolio_detail = check_global_portfolio_risk(
                            concurrent, risk_gbp
                        )
                        if portfolio_ok:
                            portfolio_ok, envelope_detail = can_allocate(
                                risk_gbp, reserve=False
                            )
                            if not portfolio_ok:
                                portfolio_detail = envelope_detail
                    except Exception as exc:
                        log_guarded_exception("trading_loop", exc)
        except Exception:
            portfolio_ok = True

        passed = (
            spread_ok
            and position_ok
            and risk_ok
            and portfolio_ok
            and not under_min_lot
        )
        if not spread_ok:
            detail = (
                f"spread {spread:.1f} > {spread_cap:.1f} "
                f"({spread_multiplier:.1f}× normal {normal:.1f}, cfg {cfg_normal:.1f}"
                f"{', OOH scale' if ooh_scale > 1.0 else ''})"
            )
            try:
                from harmonization.trade_inhibitor_log import log_trade_inhibitor

                log_trade_inhibitor(
                    epic=str(self._epic or ""),
                    gate="spread_verification",
                    reason=f"spread {spread:.2f} > cap {spread_cap:.2f}",
                    metrics={"stop_pts": "10.0"},
                )
            except Exception:
                pass
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
        elif under_min_lot:
            detail = (
                f"HOLD: UNDER_MIN_LOT — integer size {final_size} "
                f"< min lot {min_lot:.0f}"
            )
        elif not risk_ok:
            band_hint = ", probe band" if risk_band_label == "probe" else ""
            fx_hint = (
                f", USD→GBP ×{usd_gbp_rate:.4f}"
                if _epic_requires_usd_gbp_risk_conversion(self._epic)
                else ""
            )
            detail = (
                f"net risk £{total_trade_cost_gbp:.2f} > £{effective_risk_cap:.0f} cap "
                f"((stop {stop:.1f} + spread {live_spread:.1f}) × size {final_size} "
                f"× £/pt {risk_point_value_gbp:.2f}"
                f"{', IG min' if calculated_size > effective_size else ''}{band_hint}{fx_hint})"
            )
        elif not portfolio_ok:
            detail = f"portfolio envelope: {portfolio_detail}"
        else:
            clip_note = f", clipped to {actual_size:.3g}" if size_was_clipped else ""
            band_note = f", {risk_band_note}" if risk_band_note else ""
            detail = (
                f"OK — spread {spread:.1f} pts (normal {normal:.1f}, max {spread_cap:.1f}), "
                f"flat, net risk £{total_trade_cost_gbp:.0f} "
                f"(cap £{risk_cap:.0f}, equity £{locked_session_equity_gbp():,.0f})"
                f"{clip_note}{band_note}"
            )
            if corr_detail and corr_mult < 1.0:
                detail = f"{detail}; {corr_detail}"
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
                "effective_risk_gbp": round(effective_risk_gbp, 2),
                "calculated_risk_usd": round(
                    calculated_risk_usd, 2
                )
                if _epic_requires_usd_gbp_risk_conversion(self._epic)
                else None,
                "usd_gbp_rate": round(usd_gbp_rate, 6)
                if _epic_requires_usd_gbp_risk_conversion(self._epic)
                else None,
                "base_size": round(base_size, 3),
                "effective_size": round(effective_size, 3),
                "actual_size": round(actual_size, 3),
                "final_size": int(final_size),
                "under_min_lot": under_min_lot,
                "live_spread": round(live_spread, 2),
                "total_trade_cost_gbp": round(total_trade_cost_gbp, 2),
                "runtime_equity_gbp": locked_session_equity_gbp(),
                "global_portfolio_risk_ceiling_gbp": global_portfolio_risk_ceiling_gbp(),
                "size_clipped_to_risk_cap": size_was_clipped,
                "ig_min_deal_size": round(ig_min_size, 3),
                "size_multiplier": round(size_mult, 3),
                "ml_sizing_multiplier": round(ml_mult, 3),
                "correlation_density": int(corr_density),
                "correlation_size_multiplier": round(float(corr_mult), 3),
                "correlation_detail": corr_detail,
                "stop_points": round(stop, 1),
                "stop_source": stop_source,
                "limit_points": round(stop * float(self._config.reward_multiple), 1),
                "point_value_gbp": round(point_value, 3),
                "spread_cap": round(spread_cap, 1),
                "spread_multiplier": round(spread_multiplier, 2),
                "ooh_spread_scale": round(ooh_scale, 2),
                "risk_cap_gbp": risk_cap,
                "points_state": self._points.get_state(),
                "risk_band": risk_band_label,
                "sizing_confidence": round(sizing_conf, 1),
                "size_floor_applied": actual_size > effective_size,
                "atr_protect_active": bool(atr_meta.get("atr_protect_active")),
                **{
                    k: v
                    for k, v in atr_meta.items()
                    if str(k).startswith("atr_protect_")
                },
                "platform_v2_spread": v2_spread_meta,
                "platform_v2_escalation": v2_escalation_meta,
            },
            detail=detail,
        )

    def _gate_calendar_ok(self) -> GateResult:
        from risk.economic_calendar import get_economic_calendar

        cfg = getattr(self, "_config", None) or getattr(self, "config", None)
        cal = get_economic_calendar(cfg)
        if not cal.enabled:
            from system.v26_config import calendar_settings

            cfg = calendar_settings()
            if not cfg.get("enabled"):
                return GateResult(
                    name="calendar_ok",
                    passed=True,
                    value="off",
                    detail="calendar guard disabled",
                )
            from system.calendar_gate import is_calendar_blocked

            blocked, reason = is_calendar_blocked(str(getattr(self, "_epic", "") or ""))
            return GateResult(
                name="calendar_ok",
                passed=not blocked,
                value={"blocked": blocked},
                detail=reason if blocked else "no high-impact event window",
            )
        blocked, reason = cal.check_block(
            str(getattr(self, "_epic", "") or ""),
            market=str(getattr(self, "_market", "") or ""),
        )
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
        sig = self._get_gate_signal()
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

    def _apply_hierarchical_probability_gate(
        self, sig: SignalResult, quote: Quote, threshold: float
    ) -> tuple[SignalResult, float, GateResult | None]:
        """Compile 128-dim state + Pillar 4 ML verdict — promote or ML veto."""
        peak = _peak_confidence_from_signal(sig, float(sig.adjusted_confidence))
        try:
            from signals.feature_state import compile_current_feature_state
            from signals.indicators import STRATEGY_THRESHOLD_LOW_PCT
            from trading.probability_engine import (
                annotate_signal_with_probability,
                apply_hierarchical_probability_gate,
            )

            if peak < STRATEGY_THRESHOLD_LOW_PCT:
                return sig, threshold, None

            pts = self._points
            points_ledger = {
                "last_trade": getattr(pts, "_last_trade_score", 0.0),
                "session": getattr(pts, "_session_score", 0.0),
                "cumulative": getattr(pts, "_cumulative", 0.0),
                "state": pts.get_state(),
                "confidence_floor": pts.trade_confidence_threshold(self._config),
            }
            feature_payload = compile_current_feature_state(
                market=str(getattr(self, "_market", "") or ""),
                epic=str(getattr(self, "_epic", "") or ""),
                snapshot=sig.snapshot or {},
                points_ledger=points_ledger,
                quote=quote,
            )
            self._last_feature_payload = feature_payload

            verdict = apply_hierarchical_probability_gate(
                sig=sig,
                feature_payload=feature_payload,
                peak_score=peak,
                threshold=threshold,
                epic=str(getattr(self, "_epic", "") or ""),
                market=str(getattr(self, "_market", "") or ""),
            )
            sig = annotate_signal_with_probability(sig, verdict, feature_payload)
            self._last_probability_verdict = verdict

            if verdict.veto:
                try:
                    from harmonization.trade_inhibitor_log import log_trade_inhibitor

                    log_trade_inhibitor(
                        epic=str(getattr(self, "_epic", "") or ""),
                        gate="ml_probability_veto",
                        reason=(
                            f"win_probability {verdict.win_probability:.3f} "
                            f"< veto_floor 0.40"
                        ),
                        metrics={
                            "confidence": f"{float(sig.adjusted_confidence):.1f}%",
                            "threshold": f"{float(threshold):.1f}%",
                        },
                    )
                except Exception:
                    pass
                try:
                    from apex.avionics_story import append_avionics_story
                    from apex.operational_transparency import record_gate_rejection

                    record_gate_rejection(
                        "probability_ml_veto",
                        f"ML_VETO_REJECTION win_p={verdict.win_probability:.2f}",
                    )
                    append_avionics_story(
                        f"⚠ ML_VETO_REJECTION — {self._epic} "
                        f"win_probability={verdict.win_probability:.1%} "
                        f"(RSI/EMA breakout suppressed)",
                        kind="ml_veto",
                        epic=str(getattr(self, "_epic", "") or ""),
                    )
                except Exception as exc:
                    log_guarded_exception("trading_loop", exc)
                blocked = GateResult(
                    name="signal_confidence",
                    passed=False,
                    value={
                        "signal": sig,
                        "direction": "WAIT",
                        "confidence": float(sig.adjusted_confidence),
                        "ml_probability": verdict.win_probability,
                        "block_reason": "ML_VETO_REJECTION",
                        "model_verdict": verdict.model_verdict,
                    },
                    detail=(
                        f"BLOCK — ML_VETO_REJECTION "
                        f"(win_probability={verdict.win_probability:.1%} < 50%)"
                    ),
                )
                return sig, threshold, blocked

            if verdict.promote:
                threshold = max(
                    10.0,
                    float(threshold) - float(verdict.threshold_relief),
                )
                try:
                    from apex.avionics_story import append_avionics_story

                    append_avionics_story(
                        f"▲ ML PROMOTE — {self._epic} "
                        f"win_probability={verdict.win_probability:.1%} "
                        f"→ threshold −{verdict.threshold_relief:.0f}%",
                        kind="promote",
                        epic=str(getattr(self, "_epic", "") or ""),
                    )
                except Exception as exc:
                    log_guarded_exception("trading_loop", exc)
                sig = promote_high_confidence_signal(sig, threshold)
        except Exception as exc:
            log_engine(
                f"probability_engine gate skipped: {type(exc).__name__}: {exc}"
            )
        return sig, threshold, None

    def _gate_signal_confidence(self, quote: Quote) -> GateResult:
        sig = self._get_gate_signal()
        threshold = float(self._points.trade_confidence_threshold(self._config))
        try:
            from system.gate_relaxation import effective_trade_confidence_threshold

            threshold = effective_trade_confidence_threshold(
                threshold,
                points_state=self._points.get_state(),
                instrument_threshold=float(self._config.signal_threshold),
                epic=str(getattr(self, "_epic", "") or ""),
            )
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)

        sig, threshold, prob_block = self._apply_hierarchical_probability_gate(
            sig, quote, threshold
        )
        if prob_block is not None:
            self._gate_signal_cache = sig
            return prob_block

        # ------------------------------------------------------------
        # Gate 10: dynamic confidence floor (volatility-aware)
        # ------------------------------------------------------------
        live_state_vector: dict[str, Any] = {}
        try:
            snap = sig.snapshot or {}
            last = snap.get("last") or {}
            if not last:
                last = self._tick_indicator_snapshot(quote)
            _atr = float(last.get("atr", 0) or 0)
            _stop = max(1.0, float(self._config.stop_distance_points))
            atr_multiplier = _atr / _stop if _stop > 0 else 0.0

            session_score = 0.0
            nominal_state: str | None = None
            try:
                pts_snap = self._points.snapshot()
                session_score = float(getattr(pts_snap, "session_score", 0.0) or 0.0)
                nominal_state = str(getattr(pts_snap, "nominal_state", None) or "")
                if not nominal_state:
                    nominal_state = self._points.get_state()
            except Exception:
                nominal_state = self._points.get_state()

            live_state_vector = self._build_live_state_vector(
                quote,
                {
                    "session_score": session_score,
                    "nominal_state": nominal_state,
                    "atr_multiplier": atr_multiplier,
                },
            )
            self._publish_ml_sizing_multiplier(1.0)

            prot = self._config.get("protective_learning") or {}
            session_score_floor = float(prot.get("session_score_floor") or -30.0)
            min_threshold = float(prot.get("signal_confidence_floor_min") or 10.0)
            high_vol_atr_mult = float(prot.get("high_vol_atr_multiplier") or 0.25)
            relax_strength = float(
                prot.get("volatility_threshold_relax_strength") or 0.95
            )

            quote_age_s = float(live_state_vector.get("quote_age_s") or 0.0)
            age_factor = 1.0
            if quote_age_s > 10.0:
                age_factor = max(0.2, 10.0 / quote_age_s)

            atr_mult = float(live_state_vector.get("atr_multiplier") or 0.0)
            atr_norm = 0.0
            if high_vol_atr_mult > 0:
                atr_norm = min(1.0, max(0.0, atr_mult / high_vol_atr_mult))

            session_score_val = float(live_state_vector.get("session_score") or 0.0)
            session_factor = 1.0 if session_score_val >= session_score_floor else 0.5

            relax = atr_norm * age_factor * session_factor * relax_strength
            # Lower threshold aggressively in high-volatility regimes.
            threshold = max(min_threshold, threshold * (1.0 - relax))

            try:
                from intelligence.pipeline_bridge import get_intelligence_layer
                from intelligence.premium_overnight import (
                    overnight_signal_confidence_relief,
                    premium_overnight_momentum_pass,
                )

                layer = get_intelligence_layer()
                mi = layer.microstructure_verdict(str(getattr(self, "_epic", "") or ""))
                if premium_overnight_momentum_pass(
                    str(getattr(self, "_epic", "") or ""),
                    str(mi.regime),
                    float(mi.confidence),
                    config=self._config,
                ):
                    relief = overnight_signal_confidence_relief(self._config)
                    threshold = max(min_threshold, threshold - relief)
                    live_state_vector["premium_overnight_relief_pts"] = relief
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)

        conf = float(sig.adjusted_confidence)
        rules_conf = conf
        ml_prob: float | None = None
        interim_active = False
        if bool(self._config.get("USE_ML_SIGNAL", False)):
            try:
                from ml.interim_scorer import (
                    get_interim_scorer,
                    ml_clean_training_rows,
                    ml_min_rows_for_model,
                )
                from trading.ml_scorer import get_ml_scorer

                scorer = get_ml_scorer()

                # Session-scoped count — refreshed on position open/close only.
                _ml_records = ml_clean_training_rows(self._config)
                min_model_rows = ml_min_rows_for_model(self._config)
                snap = sig.snapshot or {}
                if _ml_records < min_model_rows:
                    interim_active = True
                    log_engine("[INTERIM SCORER] active")
                    interim = get_interim_scorer().score(
                        cfg=self._config,
                        market=self._market,
                        direction=str(sig.signal or "WAIT"),
                        snapshot=snap,
                        store=self._store,
                    )
                    conf = float(interim.total)
                    ml_prob = conf / 100.0
                elif scorer.is_trained() and _ml_records >= min_model_rows:
                    log_engine("[ML MODEL] active")
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
                        f"(need {min_model_rows})"
                    )
            except Exception as e:
                log_engine(f"ML gate blend skipped: {type(e).__name__}: {e}")

        try:
            from system.ml.twin_engine_core import get_twin_engine_core

            snap_te = sig.snapshot or {}
            last_te = snap_te.get("last") or {}
            if not last_te:
                last_te = self._tick_indicator_snapshot(quote)
            _atr_te = float(last_te.get("atr", 0) or 0)
            _stop_te = max(1.0, float(self._config.stop_distance_points))
            twin_features = {
                "adjusted_score": rules_conf,
                "rsi": float(last_te.get("rsi", 0) or 0),
                "atr_ratio": _atr_te / _stop_te,
            }
            twin_prob = get_twin_engine_core().ingest_and_score(
                epic=str(getattr(self, "_epic", "") or ""),
                ts_utc=None,
                bid=float(quote.bid),
                offer=float(quote.offer),
                features=twin_features,
                direction=str(sig.signal or "WAIT"),
            )
            if twin_prob > 0.0:
                if ml_prob is None:
                    ml_prob = twin_prob
                else:
                    ml_prob = (float(ml_prob) * 0.7) + (twin_prob * 0.3)
        except Exception as exc:
            log_guarded_exception("trading_loop_twin_engine", exc)

        snap = sig.snapshot or {}
        h1_penalty = float(snap.get("h1_penalty") or 0)
        if h1_penalty > 0 and ml_prob is not None:
            from signals.signal_engine import (
                H1_EMA_SOFT_PENALTY,
                H1_ML_PENALTY_WAIVER_PROB,
            )

            if ml_prob >= H1_ML_PENALTY_WAIVER_PROB:
                conf = max(0.0, min(100.0, conf + H1_EMA_SOFT_PENALTY))
                rules_conf = max(0.0, min(100.0, rules_conf + H1_EMA_SOFT_PENALTY))
                log_engine(
                    f"1h EMA soft penalty waived (ml_prob={ml_prob:.3f} "
                    f">= {H1_ML_PENALTY_WAIVER_PROB:.2f})"
                )
        self._last_ml_prob = ml_prob
        self._last_sig_direction = str(sig.signal or "WAIT")
        vol_penalty_mult = 1.0
        vol_penalty_detail = ""
        try:
            from system.live_regime_gate import momentum_vol_penalty

            vol_penalty_mult, vol_penalty_detail = momentum_vol_penalty(
                str(getattr(self, "_epic", "") or ""),
                snap,
                signal_engine=self._signal_engine,
                market=self._market,
            )
            if vol_penalty_mult < 1.0:
                conf = max(0.0, min(100.0, conf * vol_penalty_mult))
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        try:
            from system.protective_learning import (
                apply_temporary_test_confidence_floor,
                log_temporary_test_gate_once,
            )

            log_temporary_test_gate_once()
            threshold = apply_temporary_test_confidence_floor(threshold)
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        threshold = self._apply_operational_confidence_threshold(threshold)
        try:
            from signals.regime_sentinel import get_macro_regime_sentinel

            rs = get_macro_regime_sentinel()
            regime_token = rs.current_regime
            mult = rs.threshold_multiplier()
            relief = rs.confidence_relief_points()
            floor = float(
                (self._config.get("protective_learning") or {}).get(
                    "signal_confidence_floor_min"
                )
                or 10.0
            )
            threshold = max(floor, threshold * mult - relief)
            if isinstance(live_state_vector, dict):
                live_state_vector["macro_regime"] = regime_token
                live_state_vector["macro_regime_multiplier"] = mult
                live_state_vector["macro_regime_relief_pts"] = relief
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        try:
            from harmonization.volatility_gate import no_trade_paradox_threshold

            snap_thr = sig.snapshot or {}
            last_thr = snap_thr.get("last") or self._tick_indicator_snapshot(quote)
            _atr_thr = float(last_thr.get("atr", 0) or 0)
            _rsi_thr = float(last_thr.get("rsi", 50) or 50)
            threshold = no_trade_paradox_threshold(
                threshold,
                atr=_atr_thr,
                atr_baseline=max(1.0, float(self._config.stop_distance_points or 10)),
                rsi=_rsi_thr,
            )
        except Exception as exc:
            log_guarded_exception("trading_loop_paradox_threshold", exc)
        passed = sig.signal in ("BUY", "SELL") and conf >= threshold
        detail, block_reason = signal_gate_explanation(sig, threshold)
        peak = _peak_confidence_from_signal(sig, conf)
        raw_dir = str((sig.snapshot or {}).get("raw_signal") or sig.signal or "").strip()
        if not passed and raw_dir in ("BUY", "SELL"):
            if peak >= HIGH_CONFIDENCE_OVERRIDE_THRESHOLD and peak >= float(threshold):
                passed = True
                sig = self._cache_promoted_signal(
                    promote_high_confidence_signal(sig, threshold)
                )
                if not detail.startswith("PASS"):
                    detail = (
                        f"PASS — {raw_dir} {peak:.1f}% "
                        f"(high-confidence override >= {threshold:.1f}%)"
                    )
                    block_reason = ""
        if vol_penalty_detail and peak < HIGH_CONFIDENCE_OVERRIDE_THRESHOLD:
            detail = f"{detail} | vol soft: {vol_penalty_detail}"
        if not passed and block_reason:
            try:
                from harmonization.trade_inhibitor_log import log_trade_inhibitor

                log_trade_inhibitor(
                    epic=str(getattr(self, "_epic", "") or ""),
                    gate="signal_confidence",
                    reason=f"Confidence {conf:.2f} < Target {threshold:.2f}",
                    metrics={
                        "ml_prob": f"{ml_prob:.3f}" if ml_prob is not None else "n/a",
                        "direction": str(sig.signal),
                    },
                )
            except Exception:
                pass
        pts_state = self._points.get_state()
        if pts_state == "WARNING" and threshold >= 90.0:
            detail = f"{detail} (points {pts_state} — need >={threshold:.0f}%)"
        risk_band_label = ""
        try:
            from system.risk_bands import bands_enabled, risk_band_for_confidence

            if bands_enabled():
                risk_band_label = risk_band_for_confidence(conf)
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
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
                "vol_penalty_mult": vol_penalty_mult,
                "vol_penalty_detail": vol_penalty_detail,
                "risk_band": risk_band_label,
                "threshold": threshold,
                "config_signal_threshold": float(self._config.signal_threshold),
                "points_confidence_floor": float(self._points.get_threshold()),
                "min_size_threshold": float(
                    self._points.min_size_confidence_threshold()
                ),
                "points_state": self._points.get_state(),
                "block_reason": block_reason,
                "setup": sig.setup_key,
                "live_state_vector": live_state_vector,
                "macro_regime": (
                    live_state_vector.get("macro_regime")
                    if isinstance(live_state_vector, dict)
                    else None
                ),
            },
            detail=detail,
        )

    def _gate_ml_veto(self) -> GateResult:
        # Gate 11 can set a sizing multiplier that Gate 7 must apply on the
        # same tick (ml_veto risk scaling → risk_validation sizing).
        self._publish_ml_sizing_multiplier(1.0)
        try:
            from system.gate_relaxation import soak_ml_veto_bypassed

            if soak_ml_veto_bypassed():
                return GateResult(
                    name="ml_veto",
                    passed=True,
                    value="soak_bypass",
                    detail="ml_veto bypassed (demo soak)",
                )
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
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
                    sig = self._get_gate_signal()
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

        ml_prob_f = float(ml_prob)
        min_p_f = float(min_p)

        # Risk-scaling gate:
        # - hard veto when model confidence is clearly below threshold
        # - otherwise pass the gate but downscale sizing to reduce exposure
        marginal_delta = float(cfg.get("marginal_prob_band") or 0.06)
        min_sizing_multiplier = float(cfg.get("min_sizing_multiplier") or 0.25)

        passed = True
        sizing_multiplier = 1.0
        marginal_scaled = False
        if ml_prob_f >= min_p_f:
            passed = True
            sizing_multiplier = 1.0
        elif ml_prob_f >= (min_p_f - marginal_delta):
            passed = True
            marginal_scaled = True
            if marginal_delta > 0:
                t = (ml_prob_f - (min_p_f - marginal_delta)) / marginal_delta
            else:
                t = 1.0
            t = max(0.0, min(1.0, float(t)))
            sizing_multiplier = min_sizing_multiplier + t * (1.0 - min_sizing_multiplier)
        else:
            passed = False
            sizing_multiplier = min_sizing_multiplier

        self._publish_ml_sizing_multiplier(float(sizing_multiplier))
        live_vec = self._tick_live_state_vector if isinstance(
            self._tick_live_state_vector, dict
        ) else {}
        return GateResult(
            name="ml_veto",
            passed=passed,
            value={
                "ml_probability": ml_prob_f,
                "min_probability": min_p_f,
                "source": ml_source,
                "sizing_multiplier": float(sizing_multiplier),
                "marginal_scaled": bool(marginal_scaled),
                "live_state_vector": live_vec,
            },
            detail=(
                f"{ml_source} prob {ml_prob_f:.3f} ≥ {min_p_f:.3f}"
                if ml_prob_f >= min_p_f
                else f"{ml_source} prob {ml_prob_f:.3f} marginal < {min_p_f:.3f} — scaling ×{sizing_multiplier:.2f}"
                if marginal_scaled
                else f"{ml_source} prob {ml_prob_f:.3f} < {min_p_f:.3f} (veto)"
            ),
        )

    def _daily_loss_gbp(self) -> float:
        try:
            from system.daily_loss_policy import effective_daily_loss_gbp

            return effective_daily_loss_gbp(self._store)
        except Exception:
            return 0.0

    def _atr_estimate(self, quote: Quote) -> float:
        try:
            row = self._tick_indicator_snapshot(quote)
            if row:
                return float(row.get("atr", 0) or 0)
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        return 0.0

    def _friday_flatten_if_needed(self) -> None:
        try:
            from trading.friday_flatten import run_friday_flatten_tick

            at = quote_time(self._clock())
            store = getattr(self, "_store", None)

            def _list_positions() -> list[dict]:
                if store is not None and hasattr(store, "active_trades"):
                    return list(store.active_trades())
                return []

            run_friday_flatten_tick(
                cfg=self._config,
                now=at,
                execute_close=self._execute_flatten_close,
                verify_close=self._verify_flatten_after_close,
                open_count_fn=self._ig_open_position_count,
                list_positions_fn=_list_positions,
            )
        except Exception as e:
            log_engine(f"friday_flatten tick failed: {type(e).__name__}: {e}")

    def _flatten_if_needed(self) -> None:
        at = quote_time(self._clock())
        try:
            from trading.flatten_retry import (
                check_slow_monitor_alerts,
                mark_flatten_retry_attempt,
                on_flatten_confirmed,
                should_run_flatten_retry,
            )

            if should_run_flatten_retry():
                mark_flatten_retry_attempt()
                log_engine("flatten retry — scheduled re-attempt")
                try:
                    n = self._execute_flatten_close()
                    log_engine(f"flatten retry close sent — {n} position(s)")
                except Exception as e:
                    log_engine(f"flatten retry close failed: {type(e).__name__}: {e}")
                self._verify_flatten_after_close(at)
                return

            open_count = self._ig_open_position_count()
            if open_count > 0:
                check_slow_monitor_alerts(self._epic, open_count)

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
            try:
                from trading.flatten_retry import on_flatten_verify_failed

                on_flatten_verify_failed(
                    self._epic, self._ig_open_position_count(), cfg=self._config
                )
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)
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
            try:
                from trading.flatten_retry import on_flatten_confirmed

                on_flatten_confirmed()
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)
            self._session.flatten_confirmed()
            self._write_session_summary_if_needed(at)
            return
        try:
            from trading.flatten_retry import (
                check_slow_monitor_alerts,
                flatten_max_retries,
                flatten_backoff_seconds,
                get_flatten_retry_state,
                on_flatten_verify_failed,
            )

            st = on_flatten_verify_failed(
                self._epic,
                open_count,
                cfg=self._config,
            )
            cap = flatten_max_retries(self._config)
            log_engine(
                f"flatten verify failed — {open_count} position(s) still open "
                f"(failure {st.retry_count}/{cap})"
            )
            if st.abandoned:
                check_slow_monitor_alerts(
                    self._epic,
                    open_count,
                )
            return
        except Exception as e:
            log_engine(
                f"flatten verify failed — {open_count} position(s) still open "
                f"flatten_retry error: {e}"
            )
            return

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
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)
        engine = self._execution_loop.execution_engine
        tracker = getattr(engine, "trade_tracker", None)
        if tracker is not None and hasattr(tracker, "count_open_for_epic"):
            try:
                return int(tracker.count_open_for_epic(self._epic))
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)
        store = getattr(engine, "store", None) or self._store
        if store is not None and hasattr(store, "count_open_trades"):
            try:
                return int(store.count_open_trades(self._epic))
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)
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
        from ai.operational.profiler_hooks import probe_hot_path

        with probe_hot_path("probe_snapshot_publish", epic=self._epic):
            try:
                payload = self._build_snapshot_payload(ctx)
                if self._on_snapshot is not None:
                    self._on_snapshot(payload)
                elif self._publish_snapshots:
                    publish_tick(payload)
            except Exception as e:
                log_engine(f"publish_tick failed: {type(e).__name__}: {e}")

    def force_position_view_refresh(self, quote: Quote | None = None) -> bool:
        """Push open-position marks immediately from a live quote (bypasses refresh_seconds)."""
        q = quote
        if q is None:
            try:
                q = self.quote_source()
            except Exception:
                q = None
        if q is None or float(q.bid) <= 0 or float(q.offer) <= 0:
            return False
        tick_age_s: float | None = None
        try:
            from system.market_data_hub import get_market_data_hub

            snap = get_market_data_hub().get_snapshot(self._epic)
            if snap is not None:
                tick_age_s = float(snap.age_seconds())
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        try:
            from api.snapshot_store import force_position_view_refresh as _store_refresh

            return _store_refresh(
                self._epic,
                float(q.bid),
                float(q.offer),
                tick_age_s=tick_age_s,
            )
        except Exception:
            return False

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
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
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
                from system.market_integrity import (
                    LIVE_QUOTE_MAX_AGE_SEC,
                    UI_STALE_AFTER_SEC,
                    epic_market_open,
                )
                from system.stream_ready import is_stream_ready

                if not epic_market_open(self._epic):
                    stream_status = "CLOSED"
                else:
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
                    hot_stale = LIVE_QUOTE_MAX_AGE_SEC
                    if snap and snap.age_seconds() <= hot_stale:
                        stream_status = "LIVE"
                    elif snap and snap.age_seconds() <= min(stale_after, UI_STALE_AFTER_SEC):
                        stream_status = "STALE"
                    elif snap and snap.age_seconds() <= stale_after:
                        stream_status = "STALE"
                    else:
                        stream_status = "STALE"
            except Exception:
                stream_status = "DISCONNECTED"
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
        realized_daily_pnl = self._daily_pnl_signed_gbp(open_positions)

        session_open = bool(self._session.is_session_open(at=quote_time(self._clock())))

        hub_maint, session_maint = self._snapshot_maintenance_flags()

        if hub_maint or session_maint:
            market_state = "MAINTENANCE"
        elif self.entry_circuit_breaker() == OFFLINE_BROKER_FEED_REJECTED:
            market_state = "OFFLINE"
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

        strictness = resolve_strictness(
            self._config, signal_engine=self._signal_engine, market=self._market
        )
        readiness = compute_trade_readiness(
            ctx.gates,
            fitness_min=strictness.fitness_floor,
        )
        badge_text = format_health_badge_text(badge, readiness)

        quote_ts = quote.time if isinstance(quote.time, datetime) else self._clock()
        now_ts = self._clock()
        if isinstance(quote_ts, datetime) and isinstance(now_ts, datetime):
            q = (
                quote_ts.replace(tzinfo=timezone.utc)
                if quote_ts.tzinfo is None
                else quote_ts.astimezone(timezone.utc)
            )
            n = (
                now_ts.replace(tzinfo=timezone.utc)
                if now_ts.tzinfo is None
                else now_ts.astimezone(timezone.utc)
            )
            tick_age_s = max(0.0, (n - q).total_seconds())
        else:
            tick_age_s = 0.0
        if hub_maint or session_maint:
            try:
                from system.market_data_hub import get_market_data_hub

                snap = get_market_data_hub().get_snapshot(self._epic)
                if snap and snap.bid > 0:
                    tick_age_s = max(tick_age_s, snap.age_seconds())
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)

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
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)
        elif stream_status == "LIVE":
            try:
                from system.telegram_notifier import get_telegram_notifier

                notifier = get_telegram_notifier()
                if notifier is not None:
                    notifier.clear_stream_stale(self._epic)
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)

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
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)

        spread_stats: dict[str, float] = {}
        try:
            from system.market_data_hub import get_market_data_hub

            spread_stats = get_market_data_hub().spread_stats(
                self._epic, fallback=float(self._config.max_spread_points)
            )
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)

        sentiment_factor: dict[str, Any] = {}
        try:
            sentiment_factor = self._env.get_sentiment_factor(self._market)
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)

        risk_band = ""
        probe_risk_target: float | None = None
        sizing_risk_gbp: float | None = None
        for g in ctx.gates:
            if g.name == "signal_confidence" and isinstance(g.value, dict):
                risk_band = str(g.value.get("risk_band") or risk_band)
            if g.name == "risk_validation" and isinstance(g.value, dict):
                risk_band = str(g.value.get("risk_band") or risk_band)
                try:
                    sizing_risk_gbp = float(g.value.get("risk_gbp"))
                except (TypeError, ValueError):
                    sizing_risk_gbp = None
        threshold_pass: dict[str, bool] = {}
        try:
            from system.risk_bands import (
                bands_enabled,
                probe_risk_target_gbp,
                threshold_pass_map,
            )

            if bands_enabled():
                threshold_pass = threshold_pass_map(confidence, direction)
                if risk_band == "probe":
                    probe_risk_target = probe_risk_target_gbp(confidence)
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)

        from trading.open_position_view import epic_market_label

        market_label = epic_market_label(self._epic)
        signal_core_score = int(round(confidence))
        display_confidence = float(confidence)
        payload: dict[str, Any] = {
            "type": "tick",
            "epic": self._epic,
            "market": market_label,
            "market_name": market_label,
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
                "signal_core_score": signal_core_score,
                "confidence": int(round(display_confidence)),
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
                "fitness_threshold": int(round(self._effective_fitness_gate_min())),
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
                "risk_band": risk_band or None,
                "threshold_pass": threshold_pass or None,
                "probe_risk_gbp_target": (
                    round(probe_risk_target, 0)
                    if probe_risk_target is not None
                    else None
                ),
                "sizing_risk_gbp": (
                    round(sizing_risk_gbp, 0) if sizing_risk_gbp is not None else None
                ),
            },
            "price_trend": price_trend,
            "trade_eligibility": countdown,
            "points": {
                "state": points_state,
                "cumulative": float(ps.cumulative),
                "session": float(ps.session_score),
                "last_trade": float(ps.last_trade_score),
                "size_multiplier": float(self._points.get_size_multiplier(confidence)),
                "next_tier": self._points.get_next_tier(),
            },
            "positions": open_positions,
            "position_map": position_map_from_rows(open_positions),
            "realized_daily_pnl_gbp": realized_daily_pnl,
            "daily_pnl_gbp": realized_daily_pnl,
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
        try:
            from risk.economic_calendar import get_economic_calendar
            from trading.friday_flatten import friday_flatten_snapshot

            payload["friday_flatten"] = friday_flatten_snapshot(self._config)
            blackouts = get_economic_calendar(self._config).active_blackouts(
                self._epic
            )
            payload["calendar_blackout"] = blackouts
            payload["calendar_blackout_active"] = bool(blackouts)
        except Exception:
            payload["friday_flatten"] = {"active": False}
            payload["calendar_blackout"] = []
            payload["calendar_blackout_active"] = False
        try:
            from system.env_loader import load_dotenv

            load_dotenv()
            payload["ig_account_id"] = os.environ.get("IG_ACCOUNT_ID", "").strip()
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        try:
            from trading.open_position_view import attach_position_map

            attach_position_map(payload)
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        return payload

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
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)

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
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
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
        if self._store is None or not hasattr(
            self._store, "recent_confirmed_closed_trades"
        ):
            return None
        try:
            rows = self._store.recent_confirmed_closed_trades(20)
            if not rows:
                return None
            wins = 0
            for row in rows:
                result = str(row.get("result") or "").upper()
                if not result:
                    pnl = row.get("ig_pnl_currency")
                    if pnl is None:
                        pnl = row.get("pnl")
                    try:
                        pnl_f = float(pnl)
                        result = (
                            "WIN" if pnl_f > 0 else "LOSS" if pnl_f < 0 else "BREAKEVEN"
                        )
                    except (TypeError, ValueError):
                        result = ""
                if result == "WIN":
                    wins += 1
            return int(round((wins / len(rows)) * 100))
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

            rows = self._store.recent_agent_closed_trades(limit=100)
            filtered = [r for r in rows if not is_excluded_display_row(r)]
            deduped = deduplicate_ig_imports(filtered)
            deduped.sort(key=lambda r: str(r.get("closed_at") or ""), reverse=True)
            out: list[dict[str, Any]] = []
            from trading.open_position_view import (
                epic_market_label,
                row_belongs_to_epic,
            )

            for row in deduped:
                if not row_belongs_to_epic(row, self._epic):
                    continue
                row_epic = str(row.get("epic") or self._epic or "").strip()
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
                deal_ref = (
                    row.get("deal_reference")
                    or row.get("ig_deal_id")
                    or row.get("deal_id")
                )
                dry_run = row.get("dry_run")
                out.append(
                    {
                        "deal_id": row.get("deal_id") or row.get("ig_deal_id"),
                        "deal_reference": deal_ref,
                        "ig_deal_id": row.get("ig_deal_id"),
                        "dry_run": bool(dry_run) if dry_run is not None else False,
                        "market": epic_market_label(row_epic),
                        "epic": row_epic,
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
            if self._store is None or not hasattr(
                self._store, "recent_confirmed_closed_trades"
            ):
                return []
            rows = self._store.recent_confirmed_closed_trades(20)
            out: list[dict[str, Any]] = []
            for row in rows:
                pnl_gbp = row.get("ig_pnl_currency")
                if pnl_gbp is not None:
                    result = "WIN" if float(pnl_gbp) > 0 else "LOSS"
                else:
                    pnl_pts = float(row.get("pnl_points") or row.get("pnl") or 0)
                    result = "WIN" if pnl_pts > 0 else "LOSS"
                out.append({"result": result})
            return out
        except Exception:
            return []

    def _pnl_history_payload(self) -> list[dict[str, Any]]:
        try:
            if self._store is None or not hasattr(self._store, "recent_closed_trades"):
                return []
            from system.closed_trades_display import is_excluded_display_row
            from system.learning_trade_policy import is_agent_learning_row

            rows = self._store.recent_agent_closed_trades(100)
            rows_sorted = sorted(
                (
                    r
                    for r in rows
                    if r.get("closed_at")
                    and not is_excluded_display_row(r)
                    and is_agent_learning_row(r)
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
        if self._store is not None:
            try:
                from system.daily_loss_policy import effective_daily_pnl

                return float(effective_daily_pnl(self._store))
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)
        return 0.0

    def _positions_payload(self, quote: Quote | None = None) -> list[dict[str, Any]]:
        # Legacy GBP fallback; USD epics use INSTRUMENT_PNL_SPEC + FX in open_position_view.
        point_value = float(self._config.get("ig_point_value_gbp", 1.0))
        raw: list[dict[str, Any]] = []

        def _append_raw(pos: dict[str, Any]) -> None:
            if not isinstance(pos, dict):
                return
            pos_epic = str(pos.get("epic") or "")
            if self._epic and pos_epic and pos_epic != self._epic:
                return
            merged = dict(pos)
            deal_id = str(merged.get("deal_id") or merged.get("dealId") or "")
            if deal_id and self._store is not None:
                try:
                    for tr in self._store.active_trades(pos_epic or self._epic):
                        tr_keys = tr.keys()
                        tr_deal = (
                            str(tr["ig_deal_id"] or "")
                            if "ig_deal_id" in tr_keys
                            else ""
                        )
                        if tr_deal != deal_id:
                            continue
                        if "notes" in tr_keys and tr["notes"]:
                            merged["notes"] = tr["notes"]
                        if merged.get("stop") in (None, 0) and tr.get("stop"):
                            merged["stop"] = float(tr["stop"])
                        if merged.get("target") in (None, 0) and tr.get("target"):
                            merged["target"] = float(tr["target"])
                        break
                except Exception as exc:
                    log_guarded_exception("trading_loop", exc)
            raw.append(normalize_sync_position(merged))

        try:
            snap = self._execution_loop.execution_engine.trade_tracker.snapshot()
            for pos in snap.get("positions") or []:
                _append_raw(pos)
        except Exception as exc:
            log_guarded_exception("trading_loop", exc)
        if not raw:
            sync = getattr(self, "_position_sync", None)
            if sync is not None and hasattr(sync, "snapshot_dict"):
                try:
                    for pos in sync.snapshot_dict().get("positions") or []:
                        _append_raw(pos)
                except Exception as exc:
                    log_guarded_exception("trading_loop", exc)
        if not raw and self._store is not None:
            try:
                rows = self._store.active_trades(self._epic)
                raw = positions_from_store_rows(
                    rows, quote, point_value_gbp=point_value
                )
            except Exception as exc:
                log_guarded_exception("trading_loop", exc)
        return enrich_positions_with_quote(
            positions_list_from_map(position_map_from_rows(raw)),
            quote,
            point_value_gbp=point_value,
            epic=self._epic,
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
