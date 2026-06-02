"""Unified execution router — config-driven."""

from __future__ import annotations

from typing import Any, Callable

from data.learning_store import LearningStore
from data.models import Quote
from execution.adaptive_engine import AdaptiveEngine
from execution.cooldown_tracker import CooldownTracker
from execution.order_validator import OrderValidator, ValidationResult
from execution.risk_manager import RiskManager
from execution.test_simulator import TestSimulator
from execution.trade_manager import TradeManager
from execution.trade_tracker import TradeTracker
from execution.types import ExecutionMode, ExecutionResult, TradeSignal
from system.config import Config
from system.demo_execution_trace import (
    log_simulator_fallback_warning,
    trace_execution,
    update_demo_diagnostics,
)
from ig_api.exceptions import RateLimitError
from system.rate_limit_manager import get_rate_limit_manager
from system.trade_lifecycle_bus import (
    STAGE_EXECUTION_REQUEST,
    STAGE_RISK,
    STAGE_VALIDATION,
    STATUS_FAIL,
    STATUS_OK,
    get_lifecycle_bus,
)


class ExecutionEngine:
    def __init__(
        self,
        *,
        mode: ExecutionMode,
        config: Config,
        store: LearningStore,
        rest_client: Any | None = None,
        has_broker_position: Callable[[str], bool] | None = None,
        trade_tracker: TradeTracker | None = None,
        points_engine: Any | None = None,
        environment_scorer: Any | None = None,
        ml_training_store: Any | None = None,
    ) -> None:
        self.mode = mode
        self.config = config
        self.store = store
        self._has_broker_position = has_broker_position
        self._tracker = trade_tracker or TradeTracker(store, prefer_ig=False)

        self._adaptive = AdaptiveEngine(config, store)
        self._cooldown = CooldownTracker(config.cooldown_seconds)
        # Restore persisted cooldowns from the learning store so restarts
        # don't let the bot re-enter the same setup immediately.
        self._cooldown.attach_store(store)
        self._points = points_engine
        self._env_scorer = environment_scorer
        from execution.ml_training_hooks import configure_ml_training
        from data.ml_training_store import MLTrainingStore

        configure_ml_training(
            ml_store=ml_training_store or MLTrainingStore(),
            points_engine=points_engine,
            environment_scorer=environment_scorer,
        )
        self._validator = OrderValidator(
            config,
            self._adaptive,
            self._cooldown,
            store=store,
            points_engine=points_engine,
        )
        self._risk = RiskManager(config, store)
        skip_ig_exits = mode.uses_broker()
        self._trade_manager = TradeManager(
            config,
            store,
            skip_ig_synced_exits=skip_ig_exits,
            rest_client=rest_client if mode.uses_broker() else None,
            broker_stop_management=mode.uses_broker(),
        )
        self._test = TestSimulator(config, store, self._trade_manager, self._cooldown)
        self._rest_client = rest_client
        self._position_sync: Any | None = None
        self._live = None
        if rest_client:
            from execution.live_executor import LiveExecutor
            self._live = LiveExecutor(config, rest_client)

        executor_name = (
            "simulator_executor (TEST)"
            if mode.uses_simulator()
            else f"live_executor ({mode.value})"
        )
        trace_execution(
            "MODE",
            "ExecutionEngine.__init__",
            decision=f"executor={executor_name}",
            params={"mode": mode.value, "has_rest": rest_client is not None, "has_live": self._live is not None},
        )

    def set_mode(self, mode: ExecutionMode) -> None:
        self.mode = mode

    def refresh_config(self, config: Config) -> None:
        """Apply updated config to validator/risk/adaptive without restarting the session."""
        self.config = config
        for component in (
            self._adaptive,
            self._validator,
            self._risk,
            self._trade_manager,
            self._test,
        ):
            if hasattr(component, "_cfg"):
                component._cfg = config
        if self._live is not None and hasattr(self._live, "_cfg"):
            self._live._cfg = config

    @property
    def trade_tracker(self) -> TradeTracker:
        return self._tracker

    def attach_trade_tracker(self, tracker: TradeTracker) -> None:
        self._tracker = tracker

    def attach_position_sync(self, sync: Any) -> None:
        """Cached IG position counts for pre-entry double-check (no extra REST)."""
        self._position_sync = sync

    def update_positions(self, market: str, epic: str, quote: Quote) -> list[str]:
        return self._trade_manager.update_from_quote(market, epic, quote)

    def margin_preflight(
        self,
        *,
        account_available: float | None,
        open_count: int,
        max_positions: int,
    ) -> tuple[bool, str]:
        return self._risk.margin_preflight(
            account_available=account_available,
            open_count=open_count,
            max_positions=max_positions,
        )

    def validate_only(self, signal: TradeSignal) -> ValidationResult:
        trace_execution(
            "VALIDATION",
            "OrderValidator.validate",
            decision="entering",
            params={"direction": signal.direction, "epic": signal.epic},
        )
        result = self._validator.validate(
            signal,
            open_position_count=self._tracker.count_open_for_epic,
            has_open_position=self._has_broker_position,
            store_has_position=self.store.has_open_trade,
        )
        trace_execution(
            "VALIDATION",
            "OrderValidator.validate",
            decision=f"allowed={result.allowed}",
            params={"reasons": result.reasons},
        )
        return result

    def get_execution_settings(self, signal: TradeSignal) -> dict[str, Any]:
        trace_execution(
            "ADAPTIVE",
            "AdaptiveEngine.settings",
            decision="entering",
            next_fn="AdaptiveEngine.settings",
            params={"setup_key": signal.setup_key, "confidence": signal.adjusted_confidence},
        )
        settings = self._adaptive.settings(
            signal.setup_key, signal.adjusted_confidence, signal.snapshot
        )
        if self._points is not None:
            mult = float(
                self._points.get_size_multiplier(signal.adjusted_confidence)
            )
            base_size = float(settings.get("size", self.config.trade_size))
            cfg = self.config
            sized = base_size * mult
            settings["size"] = max(
                cfg.adaptive_min_trade_size,
                min(cfg.adaptive_max_trade_size, sized),
            )
            notes = str(settings.get("notes") or "")
            settings["notes"] = (
                f"{notes}, points {self._points.get_state()} ×{mult:.2f}"
                if notes
                else f"points {self._points.get_state()} ×{mult:.2f}"
            )
        if self._env_scorer is not None:
            try:
                settings["fitness_score"] = float(
                    self._env_scorer.score(signal.market, quote=signal.quote)
                )
            except Exception:
                settings["fitness_score"] = 0.0
        trace_execution(
            "ADAPTIVE",
            "AdaptiveEngine.settings",
            decision="rules applied",
            next_fn="RiskManager.assess",
            params={k: settings.get(k) for k in ("size", "risk", "limit", "reward") if k in settings},
        )
        return settings

    def execute_trade(self, signal: TradeSignal, *, prevalidated: bool = False) -> ExecutionResult:
        try:
            get_rate_limit_manager().check_rest_allowed()
        except RateLimitError as e:
            return ExecutionResult(
                success=False,
                action="REJECTED",
                rejection_reason=str(e),
                execution_params={},
            )

        trace_execution(
            "EXECUTION",
            "ExecutionEngine.execute_trade",
            decision="entered",
            next_fn="OrderValidator.validate",
            params={"mode": self.mode.value, "direction": signal.direction},
        )
        if prevalidated:
            validation = ValidationResult(allowed=True)
        else:
            validation = self.validate_only(signal)
        if not validation.allowed:
            reason = "; ".join(validation.reasons) or "Validation failed"
            bus = get_lifecycle_bus()
            bus.emit(STAGE_VALIDATION, STATUS_FAIL, reason)
            bus.finalize_rejected(reason, stage=STAGE_VALIDATION)
            return ExecutionResult(
                success=False,
                action="REJECTED",
                rejection_reason=reason,
                execution_params=self.get_execution_settings(signal),
                messages=validation.reasons,
            )

        execution_params = self.get_execution_settings(signal)
        trace_execution(
            "RISK",
            "RiskManager.assess",
            decision="entering",
            next_fn="RiskManager.assess",
            params={"direction": signal.direction},
        )
        account_balance: float | None = None
        account_available: float | None = None
        if self.mode.uses_broker() and self._rest_client is not None:
            try:
                if hasattr(self._rest_client, "maybe_refresh_account_summary"):
                    summary = self._rest_client.maybe_refresh_account_summary(min_interval=60.0)
                    account_balance = summary.get("balance")
                    account_available = summary.get("available")
                if account_available is None and hasattr(self._rest_client, "fetch_account_balance"):
                    account_balance = self._rest_client.fetch_account_balance()
                    account_available = account_balance
            except Exception as e:
                err = f"Account summary unavailable: {e}"
                update_demo_diagnostics(account_fetch_error=err)
                trace_execution(
                    "RISK",
                    "ExecutionEngine.execute_trade",
                    decision=err,
                    params={"margin_checks": "skipped"},
                )
        risk = self._risk.assess(
            direction=signal.direction,
            execution_params=execution_params,
            account_balance=account_balance,
            account_available=account_available,
        )
        trace_execution(
            "RISK",
            "RiskManager.assess",
            decision=f"approved={risk.approved}",
            next_fn="LiveExecutor.execute" if risk.approved else "rejected",
            params={"size": risk.size, "stop": risk.stop_distance, "reason": risk.reason},
        )
        if not risk.approved:
            bus = get_lifecycle_bus()
            bus.emit(STAGE_RISK, STATUS_FAIL, risk.reason)
            update_demo_diagnostics(last_rejection=risk.reason)
            bus.finalize_rejected(risk.reason, stage=STAGE_RISK)
            return ExecutionResult(
                success=False,
                action="REJECTED",
                rejection_reason=risk.reason,
                execution_params=execution_params,
            )

        execution_params = {
            **execution_params,
            "size": risk.size,
            "risk": risk.stop_distance,
            "limit": risk.limit_distance,
        }

        if self.mode.uses_broker():
            from execution.market_suspension import gate_detail as suspend_detail
            from execution.market_suspension import is_blocked
            from execution.margin_preflight import apply_margin_preflight

            if is_blocked():
                reason = suspend_detail() or "Market suspended"
                bus = get_lifecycle_bus()
                bus.emit(STAGE_RISK, STATUS_FAIL, reason)
                bus.finalize_rejected(reason, stage=STAGE_RISK)
                return ExecutionResult(
                    success=False,
                    action="REJECTED",
                    rejection_reason=reason,
                    execution_params=execution_params,
                )
            execution_params = apply_margin_preflight(
                self.config, execution_params, account_available
            )
            if execution_params.pop("_margin_skip", False):
                reason = "Insufficient margin for minimum size"
                bus = get_lifecycle_bus()
                bus.emit(STAGE_RISK, STATUS_FAIL, reason)
                bus.finalize_rejected(reason, stage=STAGE_RISK)
                return ExecutionResult(
                    success=False,
                    action="REJECTED",
                    rejection_reason=reason,
                    execution_params=execution_params,
                )
            blocked, pos_reason = self._pre_entry_position_check(signal)
            if blocked:
                bus = get_lifecycle_bus()
                bus.emit(STAGE_RISK, STATUS_FAIL, pos_reason)
                bus.finalize_rejected(pos_reason, stage=STAGE_RISK)
                return ExecutionResult(
                    success=False,
                    action="REJECTED",
                    rejection_reason=pos_reason,
                    execution_params=execution_params,
                )

        get_lifecycle_bus().emit(
            STAGE_RISK,
            STATUS_OK,
            f"size={risk.size} stop={risk.stop_distance:.1f}",
            size=risk.size,
            stop=risk.stop_distance,
        )
        get_lifecycle_bus().emit(
            STAGE_EXECUTION_REQUEST,
            STATUS_OK,
            f"Routing {self.mode.value}",
            mode=self.mode.value,
        )

        if self.mode.uses_simulator():
            if self.mode == ExecutionMode.DEMO:
                log_simulator_fallback_warning("ExecutionMode.DEMO routed to TestSimulator")
            trace_execution(
                "EXECUTION",
                "TestSimulator.execute",
                decision="routing to simulator",
                next_fn="TestSimulator.execute",
                params={"mode": self.mode.value},
            )
            update_demo_diagnostics(executor_selected="simulator_executor (TEST)")
            return self._test.execute(signal, execution_params)

        if self._live is None:
            reason = f"{self.mode.value} mode requires REST client"
            update_demo_diagnostics(last_rejection=reason, executor_selected="none")
            trace_execution("EXECUTION", "ExecutionEngine.execute_trade", decision=f"REJECTED: {reason}")
            return ExecutionResult(
                success=False,
                action="REJECTED",
                rejection_reason=reason,
                execution_params=execution_params,
            )
        update_demo_diagnostics(executor_selected=f"live_executor ({self.mode.value})")
        trace_execution(
            "EXECUTION",
            "LiveExecutor.execute",
            decision=f"DEMO/LIVE broker path mode={self.mode.value}",
            next_fn="LiveExecutor.execute",
        )
        return self._live.execute(
            signal, execution_params, self._trade_manager, self._cooldown, mode=self.mode
        )

    def _pre_entry_position_check(self, signal: TradeSignal) -> tuple[bool, str]:
        sync = self._position_sync
        if sync is None:
            return False, ""
        try:
            ig = int(sync.count_for_epic(signal.epic))
            local = int(self._tracker.count_open_for_epic(signal.epic))
            if ig != local and hasattr(sync, "sync_once"):
                sync.sync_once()
                ig = int(sync.count_for_epic(signal.epic))
                local = int(self._tracker.count_open_for_epic(signal.epic))
            if ig > 0:
                return True, f"IG confirms {ig} open position(s) on {signal.epic}"
        except Exception:
            pass
        return False, ""

    def wait_pending_orders(self, *, timeout: float = 30.0) -> None:
        """Wait for background broker order workers (tests / E2E)."""
        if self._live is not None and hasattr(self._live, "wait_pending_orders"):
            self._live.wait_pending_orders(timeout=timeout)

    def health_check(self) -> dict[str, bool]:
        return {
            "store_writable": self.store.is_writable(),
            "adaptive_enabled": self.config.adaptive_execution_enabled,
            "test_mode": self.mode.uses_simulator(),
            "broker_mode": self.mode.uses_broker(),
            "live_client": self._live is not None,
        }
