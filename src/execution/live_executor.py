"""DEMO/LIVE mode — real IG order placement via REST."""

from __future__ import annotations

import threading
from typing import Any

from execution.cooldown_tracker import CooldownTracker
from execution.trade_manager import TradeManager
from execution.types import ExecutionMode, ExecutionResult, TradeSignal
from ig_api.exceptions import IGAPIError, IGOrderError
from system.config import Config
from system.config_loader import get_config as _get_live_config
from ig_api.exceptions import RateLimitError
from ig_api.mock_clients import MockIGRest
from system.engine_log import log_engine
from system.rate_limit_manager import get_rate_limit_manager
from system.demo_execution_trace import log_simulator_fallback_warning, trace_execution, update_demo_diagnostics
from system.trade_lifecycle_bus import (
    STAGE_EXECUTION_REQUEST,
    STAGE_IG_RESPONSE,
    STAGE_POSITION_OPENED,
    STATUS_FAIL,
    STATUS_OK,
    get_lifecycle_bus,
)

from execution.entry_inflight import (
    clear_entry,
    has_entry_in_flight,
    set_entry_deal_reference,
    try_begin_entry,
)
from execution.japan225_daily_risk import (
    is_paused as japan225_daily_risk_paused,
    pause_reason as japan225_daily_risk_reason,
)
from execution.pending_order_reconcile import (
    ORDER_TYPE_ENTRY,
    has_pending,
    log_unresolved_if_due,
    mark_pending,
    resolve_pending,
)


def epic_has_pending_open(epic: str) -> bool:
    return has_entry_in_flight(epic)


def mark_epic_pending_open(epic: str) -> bool:
    """Reserve epic while async confirm runs. Returns False if already pending."""
    return try_begin_entry(epic, "", 0.0)


def clear_epic_pending_open(epic: str) -> None:
    clear_entry(epic)


class LiveExecutor:
    def __init__(self, config: Config, rest_client: Any) -> None:
        self._cfg = config
        self._client = rest_client
        self._workers_lock = threading.Lock()
        self._pending_workers: list[threading.Thread] = []

    @property
    def config(self) -> Config:
        return _get_live_config()

    def wait_pending_orders(self, *, timeout: float = 30.0) -> None:
        """Block until background order workers finish (E2E / tests)."""
        deadline = threading.Event()
        import time as _t

        end = _t.time() + max(0.1, float(timeout))
        while _t.time() < end:
            with self._workers_lock:
                alive = [t for t in self._pending_workers if t.is_alive()]
                self._pending_workers = alive
                if not alive:
                    return
            _t.sleep(0.05)

    def execute(
        self,
        signal: TradeSignal,
        execution_params: dict[str, Any],
        trade_manager: TradeManager,
        cooldown: CooldownTracker,
        *,
        mode: ExecutionMode = ExecutionMode.LIVE,
    ) -> ExecutionResult:
        cfg = self._cfg
        client_type = getattr(self._client, "account_type", cfg.account_type)
        is_demo = client_type == "DEMO" or mode == ExecutionMode.DEMO

        try:
            get_rate_limit_manager().check_rest_allowed()
        except RateLimitError as e:
            update_demo_diagnostics(last_rejection=str(e), rest_status="rate limited")
            return ExecutionResult(
                success=False,
                action="REJECTED",
                rejection_reason=str(e),
                execution_params=execution_params,
            )

        if isinstance(self._client, MockIGRest):
            log_simulator_fallback_warning("MockIGRest detected in LiveExecutor — blocked")
            return ExecutionResult(
                success=False,
                action="REJECTED",
                rejection_reason="DEMO mode cannot use mock REST client",
                execution_params=execution_params,
            )

        if not cfg.allow_live_trading and not is_demo:
            reason = "Live trading not armed in config"
            update_demo_diagnostics(last_rejection=reason)
            trace_execution("ORDER", "LiveExecutor.execute", decision=f"REJECTED: {reason}")
            return ExecutionResult(
                success=False,
                action="REJECTED",
                rejection_reason=reason,
                execution_params=execution_params,
            )

        if cfg.dry_run:
            return self._execute_dry_run(signal, execution_params, trade_manager)

        if has_pending(signal.epic):
            log_unresolved_if_due(signal.epic)
            reason = (
                f"Order confirmation unresolved for {signal.epic} — "
                f"trading paused until reconciliation"
            )
            update_demo_diagnostics(last_rejection=reason)
            trace_execution("ORDER", "LiveExecutor.execute", decision=f"REJECTED: {reason}")
            return ExecutionResult(
                success=False,
                action="REJECTED",
                rejection_reason=reason,
                execution_params=execution_params,
            )

        if japan225_daily_risk_paused(signal.epic):
            detail = japan225_daily_risk_reason(signal.epic)
            reason = (
                "Daily risk limit hit — entries paused until next JST session"
                + (f" ({detail})" if detail else "")
            )
            update_demo_diagnostics(last_rejection=reason)
            trace_execution("ORDER", "LiveExecutor.execute", decision=f"REJECTED: {reason}")
            return ExecutionResult(
                success=False,
                action="REJECTED",
                rejection_reason=reason,
                execution_params=execution_params,
            )

        size = float(execution_params.get("size", cfg.trade_size))
        if not try_begin_entry(signal.epic, signal.direction, size):
            reason = f"Entry already in flight for {signal.epic} — skipped duplicate"
            update_demo_diagnostics(last_rejection=reason)
            trace_execution("ORDER", "LiveExecutor.execute", decision=f"REJECTED: {reason}")
            return ExecutionResult(
                success=False,
                action="REJECTED",
                rejection_reason=reason,
                execution_params=execution_params,
            )

        trace_execution(
            "ORDER",
            "LiveExecutor.execute",
            decision="async submit",
            next_fn="LiveExecutor._order_worker",
            params={
                "mode": mode.value,
                "epic": signal.epic,
                "direction": signal.direction,
            },
        )

        worker = threading.Thread(
            target=self._order_worker,
            args=(signal, execution_params, trade_manager, cooldown, mode),
            daemon=True,
            name="OrderConfirmWorker",
        )
        with self._workers_lock:
            self._pending_workers = [t for t in self._pending_workers if t.is_alive()]
            self._pending_workers.append(worker)
        try:
            worker.start()
        except Exception:
            clear_entry(signal.epic)
            raise
        log_engine("Order submitted — background worker handling confirm")
        return ExecutionResult(
            success=True,
            action="SUBMITTED",
            execution_params=execution_params,
            messages=["Order confirm running in background worker"],
        )

    def _execute_dry_run(
        self,
        signal: TradeSignal,
        execution_params: dict[str, Any],
        trade_manager: TradeManager,
    ) -> ExecutionResult:
        cfg = self._cfg
        trace_execution(
            "ORDER",
            "LiveExecutor.execute",
            decision="dry_run — order not sent to IG",
            params={"epic": signal.epic, "direction": signal.direction},
        )
        update_demo_diagnostics(
            last_rejection="dry_run enabled in config — no broker order",
            rest_status="dry_run",
        )
        trade_manager.open_trade_from_execution(
            market=signal.market,
            epic=signal.epic,
            side=signal.direction,
            quote=signal.quote,
            raw_confidence=signal.raw_confidence,
            adjusted_confidence=signal.adjusted_confidence,
            setup_key=signal.setup_key,
            deal_reference="DRY-RUN",
            notes=f"{signal.notes} | dry_run",
            execution=execution_params,
            dry_run=True,
        )
        return ExecutionResult(
            success=True,
            action="DRY_RUN",
            execution_params=execution_params,
            messages=["dry_run=true — simulated fill, no IG order"],
        )

    def _order_worker(
        self,
        signal: TradeSignal,
        execution_params: dict[str, Any],
        trade_manager: TradeManager,
        cooldown: CooldownTracker,
        mode: ExecutionMode,
    ) -> None:
        from system.rest_api_budget import begin_order_in_flight, end_order_in_flight

        begin_order_in_flight()
        try:
            result = self._execute_order_blocking(
                signal, execution_params, trade_manager, cooldown, mode=mode
            )
            if result.success:
                resolve_pending(signal.epic, reason="entry confirmed by broker")
                log_engine(
                    f"Order confirmed: deal={result.deal_id or '—'} "
                    f"ref={result.deal_reference or '—'}"
                )
            else:
                log_engine(f"Order failed: reason={result.rejection_reason or result.action}")
        except Exception as e:
            mark_pending(
                signal.epic,
                side=signal.direction,
                order_type=ORDER_TYPE_ENTRY,
            )
            log_engine(f"Order failed: reason={type(e).__name__}: {e}")
        finally:
            clear_entry(signal.epic)
            end_order_in_flight()

    def _execute_order_blocking(
        self,
        signal: TradeSignal,
        execution_params: dict[str, Any],
        trade_manager: TradeManager,
        cooldown: CooldownTracker,
        *,
        mode: ExecutionMode = ExecutionMode.LIVE,
    ) -> ExecutionResult:
        cfg = self._cfg
        client_type = getattr(self._client, "account_type", cfg.account_type)
        is_demo = client_type == "DEMO" or mode == ExecutionMode.DEMO
        base = getattr(self._client, "_base", "")
        account_id = getattr(self._client, "account_id", "")

        trace_execution(
            "ORDER",
            "LiveExecutor._execute_order_blocking",
            decision="entered",
            next_fn="IGRestClient.place_market_order",
            params={
                "mode": mode.value,
                "client_type": client_type,
                "endpoint": base,
                "account_id": account_id,
                "epic": signal.epic,
                "direction": signal.direction,
            },
        )
        update_demo_diagnostics(
            endpoint=base,
            account_id=str(account_id),
            executor_selected=f"live_executor ({'DEMO' if is_demo else 'LIVE'})",
        )

        size = float(execution_params.get("size", cfg.trade_size))
        stop_distance = float(execution_params.get("risk", cfg.stop_distance_points))
        limit_distance = float(execution_params.get("limit", cfg.limit_distance_points))

        if hasattr(self._client, "normalize_order_params"):
            size, stop_distance, limit_distance, currency_code = self._client.normalize_order_params(
                signal.epic,
                size=size,
                stop_distance=stop_distance,
                limit_distance=limit_distance,
                currency_code=cfg.currency_code,
            )
            execution_params = {
                **execution_params,
                "size": size,
                "risk": stop_distance,
                "limit": limit_distance or 0.0,
                "currency_code": currency_code,
            }

        import time as _time_exec

        payload = {
            "epic": signal.epic,
            "direction": signal.direction,
            "size": size,
            "stopDistance": stop_distance,
            "limitDistance": limit_distance,
            "currencyCode": cfg.currency_code,
        }
        update_demo_diagnostics(last_order_payload=payload)

        bus = get_lifecycle_bus()
        max_retries = int(cfg.max_retries) if hasattr(cfg, "max_retries") else 2
        retry_delay = float(cfg.retry_delay_seconds) if hasattr(cfg, "retry_delay_seconds") else 2.5

        result: dict | None = None
        ref = ""
        confirm: dict = {}
        last_error: str = ""

        for attempt in range(1, max_retries + 2):
            if ref:
                confirm = (
                    self._client.confirm_deal(ref)
                    if hasattr(self._client, "confirm_deal")
                    else {"accepted": False, "rejected": True, "reason": "no confirm_deal"}
                )
                update_demo_diagnostics(last_ig_response={"confirm": confirm})
                trace_execution(
                    "REST",
                    "LiveExecutor.idempotency_check",
                    decision=(
                        f"poll /confirms attempt {attempt}: "
                        f"accepted={confirm.get('accepted')} rejected={confirm.get('rejected')}"
                    ),
                    params={"dealReference": ref, "deal_id": confirm.get("deal_id")},
                )
                if confirm.get("accepted"):
                    break
                if confirm.get("rejected"):
                    reason = str(confirm.get("reason") or "Order rejected by IG")
                    bus.emit(STAGE_IG_RESPONSE, STATUS_FAIL, reason, confirm=confirm)
                    bus.finalize_failure(reason=reason)
                    update_demo_diagnostics(last_rejection=reason, rest_status="confirm rejected")
                    resolve_pending(signal.epic, reason="entry rejected by broker")
                    return ExecutionResult(
                        success=False,
                        action="REJECTED",
                        rejection_reason=reason,
                        deal_reference=ref,
                        execution_params=execution_params,
                    )
                if attempt <= max_retries:
                    trace_execution(
                        "REST",
                        "LiveExecutor.confirm_timeout",
                        decision=(
                            f"confirm pending/timeout attempt {attempt} "
                            f"— polling again after {retry_delay}s (no re-post)"
                        ),
                    )
                    _time_exec.sleep(retry_delay)
                    continue
                break

            bus.emit(
                STAGE_EXECUTION_REQUEST,
                STATUS_OK,
                f"POST /positions/otc (attempt {attempt})",
                payload=payload,
            )
            trace_execution(
                "REST",
                "IGRestClient.place_market_order",
                decision=f"calling IG (attempt {attempt}/{max_retries + 1})",
                next_fn="IGRestClient.place_market_order",
                params=payload,
            )
            try:
                result = self._client.place_market_order(
                    epic=signal.epic,
                    direction=signal.direction,
                    size=size,
                    stop_distance=stop_distance,
                    limit_distance=limit_distance,
                    currency_code=cfg.currency_code,
                )
                update_demo_diagnostics(
                    last_ig_response=result,
                    rest_status=f"order submitted (attempt {attempt})",
                )
                ref = str(result.get("dealReference", ""))
                if ref:
                    set_entry_deal_reference(signal.epic, ref)
                trace_execution(
                    "REST",
                    "IGRestClient.place_market_order",
                    decision=f"IG response received (attempt {attempt})",
                    next_fn="IGRestClient.confirm_deal",
                    params={"dealReference": ref},
                )
            except RateLimitError as e:
                bus.emit(STAGE_IG_RESPONSE, STATUS_FAIL, str(e))
                bus.finalize_failure(reason=str(e))
                update_demo_diagnostics(
                    last_rejection=str(e),
                    rest_status="rate limited",
                    fallback_reason="none — broker path only",
                )
                trace_execution("REST", "IGRestClient.place_market_order", decision=f"RATE LIMIT: {e}")
                return ExecutionResult(
                    success=False,
                    action="REJECTED",
                    rejection_reason=str(e),
                    execution_params=execution_params,
                )
            except (IGAPIError, IGOrderError) as e:
                status_code = getattr(e, "status_code", None)
                last_error = str(e)
                update_demo_diagnostics(
                    last_rejection=last_error,
                    rest_status=f"order failed HTTP {status_code}" if status_code else "order failed",
                    fallback_reason="none — broker path only",
                )
                trace_execution(
                    "REST",
                    "IGRestClient.place_market_order",
                    decision=f"ERROR attempt {attempt}: {e}",
                )
                if attempt <= max_retries:
                    trace_execution(
                        "REST",
                        "LiveExecutor.retry",
                        decision=f"retrying in {retry_delay}s (attempt {attempt}/{max_retries + 1})",
                    )
                    _time_exec.sleep(retry_delay)
                    continue
                bus.emit(STAGE_IG_RESPONSE, STATUS_FAIL, last_error)
                bus.finalize_failure(reason=last_error)
                mark_pending(
                    signal.epic,
                    side=signal.direction,
                    order_type=ORDER_TYPE_ENTRY,
                    deal_reference=ref,
                )
                return ExecutionResult(
                    success=False,
                    action="REJECTED",
                    rejection_reason=last_error,
                    execution_params=execution_params,
                )

            if not ref:
                reason = "no dealReference in IG order response"
                bus.emit(STAGE_IG_RESPONSE, STATUS_FAIL, reason)
                bus.finalize_failure(reason=reason)
                return ExecutionResult(
                    success=False,
                    action="REJECTED",
                    rejection_reason=reason,
                    execution_params=execution_params,
                )

            confirm = (
                self._client.confirm_deal(ref)
                if ref
                else {"accepted": False, "rejected": True, "reason": "no dealReference"}
            )
            update_demo_diagnostics(last_ig_response={"place": result, "confirm": confirm})
            trace_execution(
                "REST",
                "IGRestClient.confirm_deal",
                decision=(
                    f"accepted={confirm.get('accepted')} rejected={confirm.get('rejected')} "
                    f"status={confirm.get('status')}"
                ),
                params={"reason": confirm.get("reason"), "deal_id": confirm.get("deal_id")},
            )

            if confirm.get("accepted"):
                break

            if confirm.get("rejected"):
                break

            if attempt <= max_retries:
                trace_execution(
                    "REST",
                    "LiveExecutor.confirm_timeout",
                    decision=(
                        f"confirm pending/timeout attempt {attempt} "
                        f"— polling again after {retry_delay}s (no re-post)"
                    ),
                )
                _time_exec.sleep(retry_delay)

        if not confirm.get("accepted") and ref and hasattr(self._client, "has_open_position"):
            try:
                if self._client.has_open_position(signal.epic):
                    retry_confirm = self._client.confirm_deal(ref)
                    if retry_confirm.get("accepted"):
                        confirm = retry_confirm
            except Exception:
                pass

        if not confirm.get("accepted"):
            reason = str(confirm.get("reason") or confirm.get("error") or last_error or "Order rejected")
            bus.emit(STAGE_IG_RESPONSE, STATUS_FAIL, reason, confirm=confirm)
            bus.finalize_failure(reason=reason)
            update_demo_diagnostics(last_rejection=reason, rest_status="confirm rejected")
            if confirm.get("rejected"):
                resolve_pending(signal.epic, reason="entry rejected by broker")
            else:
                mark_pending(
                    signal.epic,
                    side=signal.direction,
                    order_type=ORDER_TYPE_ENTRY,
                    deal_reference=ref,
                )
            return ExecutionResult(
                success=False,
                action="REJECTED",
                rejection_reason=reason,
                deal_reference=ref,
                execution_params=execution_params,
            )

        bus.emit(
            STAGE_IG_RESPONSE,
            STATUS_OK,
            f"accepted deal={confirm.get('deal_id')}",
            deal_reference=ref,
            deal_id=confirm.get("deal_id"),
        )
        bus.emit(
            STAGE_POSITION_OPENED,
            STATUS_OK,
            f"{signal.direction} {signal.epic} size={size}",
            epic=signal.epic,
            deal_id=confirm.get("deal_id"),
        )
        cooldown.record(signal.epic)
        trade_manager.open_trade_from_execution(
            market=signal.market,
            epic=signal.epic,
            side=signal.direction,
            quote=signal.quote,
            raw_confidence=signal.raw_confidence,
            adjusted_confidence=signal.adjusted_confidence,
            setup_key=signal.setup_key,
            deal_reference=ref,
            notes=signal.notes,
            execution=execution_params,
            dry_run=False,
            ig_deal_id=str(confirm.get("deal_id") or ""),
        )
        deal_id = str(confirm.get("deal_id") or "")
        if deal_id and hasattr(self._client, "ensure_protective_stops"):
            self._client.ensure_protective_stops(
                deal_id,
                epic=signal.epic,
                stop_distance=float(stop_distance),
                limit_distance=float(limit_distance),
            )

        label = "DEMO" if is_demo else "LIVE"
        return ExecutionResult(
            success=True,
            action="EXECUTED",
            deal_reference=ref,
            deal_id=confirm.get("deal_id"),
            execution_params=execution_params,
            messages=[
                f"{label} IG order size={size} stop={stop_distance} limit={limit_distance}"
            ],
        )
