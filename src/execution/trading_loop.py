"""Main tick loop — market data → signals → validation → execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from data.journal import DecisionJournal
from data.models import Quote
from execution.execution_engine import ExecutionEngine
from execution.live_trade_gate import LiveTradeGate
from execution.order_validator import ValidationResult
from execution.types import (
    ExecutionResult,
    TradeSignal,
    normalize_gate_execution_params,
)
from signals.signal_engine import SignalEngine, SignalResult
from system.demo_execution_trace import trace_execution, update_demo_diagnostics
from system.engine_log import log_engine
from system.trade_audit import log_trade_audit
from system.trade_lifecycle_bus import (
    STAGE_RISK,
    STAGE_SIGNAL,
    STAGE_VALIDATION,
    STATUS_OK,
    STATUS_SKIP,
    get_lifecycle_bus,
)


@dataclass
class TickOutcome:
    quote: Quote
    signal: SignalResult
    trade_signal: TradeSignal
    validation: ValidationResult
    execution: ExecutionResult | None = None
    position_messages: list[str] = field(default_factory=list)
    block_reason: str | None = None


class TradingLoop:
    def __init__(
        self,
        *,
        signal_engine: SignalEngine,
        execution_engine: ExecutionEngine,
        journal: DecisionJournal | None = None,
        auto_trade: bool = True,
        live_gate: LiveTradeGate | None = None,
        broker_connected: Callable[[], bool] | None = None,
        broker_gate: Callable[[], tuple[bool, str]] | None = None,
    ) -> None:
        self.signal_engine = signal_engine
        self.execution_engine = execution_engine
        self.journal = journal
        self.auto_trade = auto_trade
        self.live_gate = live_gate
        self.broker_connected = broker_connected
        self.broker_gate = broker_gate
        self.tick_count = 0
        self.last_outcome: TickOutcome | None = None
        self.session_executions: list[str] = []

    def _fetch_account_available(self) -> float | None:
        client = getattr(self.execution_engine, "_rest_client", None)
        if client is None:
            return None
        try:
            if hasattr(client, "maybe_refresh_account_summary"):
                summary = client.maybe_refresh_account_summary(min_interval=60.0)
                avail = summary.get("available")
                if avail is not None:
                    return float(avail)
            if hasattr(client, "fetch_account_balance"):
                balance = client.fetch_account_balance()
                if balance is not None:
                    return float(balance)
        except Exception:
            return None
        return None

    def process_tick(
        self,
        market: str,
        epic: str,
        quote: Quote,
        *,
        prefetched_signal: SignalResult | None = None,
        gate_execution_params: dict[str, Any] | None = None,
    ) -> TickOutcome:
        from system.market_watch.japan225_session import (
            japan225_strategy_paused,
            log_japan225_session_closed,
        )

        paused, pause_msg = japan225_strategy_paused(epic)
        if paused:
            if pause_msg.startswith("Japan225 closed"):
                log_japan225_session_closed()
            closed_msg = pause_msg
            wait_sig = SignalResult(
                signal="WAIT",
                raw_confidence=0.0,
                adjusted_confidence=0.0,
                learning_delta=0.0,
                setup_key="",
                notes=closed_msg,
                snapshot={},
            )
            wait_trade = TradeSignal(
                market=market,
                epic=epic,
                direction="WAIT",
                raw_confidence=0.0,
                adjusted_confidence=0.0,
                setup_key="",
                quote=quote,
                snapshot={},
                notes=closed_msg,
            )
            outcome = TickOutcome(
                quote=quote,
                signal=wait_sig,
                trade_signal=wait_trade,
                validation=ValidationResult(
                    allowed=False,
                    reasons=[closed_msg],
                    checks={"japan225_session": False},
                ),
                block_reason=closed_msg,
            )
            self.last_outcome = outcome
            return outcome

        self.tick_count += 1
        cfg = self.execution_engine.config
        tracker = self.execution_engine.trade_tracker
        open_epic = int(tracker.count_open_for_epic(epic))
        total_raw = tracker.count_open_total()
        open_total = (
            max(open_epic, int(total_raw))
            if isinstance(total_raw, (int, float))
            else open_epic
        )
        trace_execution(
            "TICK",
            "TradingLoop.process_tick",
            decision="tick started",
            next_fn="SignalEngine.evaluate",
            params={
                "market": market,
                "epic": epic,
                "bid": quote.bid,
                "offer": quote.offer,
            },
        )
        self.signal_engine.add_quote(market, quote)
        position_messages = self.execution_engine.update_positions(market, epic, quote)
        if prefetched_signal is not None:
            sig = prefetched_signal
            trace_execution(
                "SIGNAL",
                "SignalEngine.evaluate",
                decision=f"using gate signal={sig.signal} conf={sig.adjusted_confidence:.1f}%",
                next_fn="OrderValidator.validate"
                if sig.signal in ("BUY", "SELL")
                else "end",
                params={"market": market, "prefetched": True},
            )
        else:
            trace_execution(
                "SIGNAL",
                "SignalEngine.evaluate",
                decision="entering",
                next_fn="SignalEngine.evaluate",
                params={"market": market},
            )
            sig = self.signal_engine.evaluate(market)
        log_engine(
            f"GATE CHECK {epic}: confidence={sig.adjusted_confidence:.1f} "
            f"threshold={cfg.signal_threshold} fitness=— "
            f"allow_live={cfg.allow_live_trading} dry_run={cfg.dry_run} "
            f"size={cfg.trade_size} direction={sig.signal} "
            f"setup={sig.setup_key or '—'} open_epic={open_epic} "
            f"open_total={open_total} auto_trade={self.auto_trade} "
            f"prefetched={prefetched_signal is not None}"
        )
        bus = get_lifecycle_bus()
        trace_execution(
            "SIGNAL",
            "SignalEngine.evaluate",
            decision=f"signal={sig.signal} conf={sig.adjusted_confidence:.1f}%",
            next_fn="OrderValidator.validate"
            if sig.signal in ("BUY", "SELL")
            else "end",
            params={
                "signal": sig.signal,
                "setup_key": sig.setup_key,
                "notes": sig.notes[:120],
            },
        )
        update_demo_diagnostics(
            last_signal=sig.signal, auto_trade_enabled=self.auto_trade
        )

        trade_signal = TradeSignal(
            market=market,
            epic=epic,
            direction=sig.signal,
            raw_confidence=sig.raw_confidence,
            adjusted_confidence=sig.adjusted_confidence,
            setup_key=sig.setup_key,
            quote=quote,
            snapshot=sig.snapshot,
            notes=sig.notes,
            gate_execution_params=normalize_gate_execution_params(
                gate_execution_params
            ),
        )
        trace_execution(
            "VALIDATION",
            "OrderValidator.validate",
            decision="entering (validate_only)",
            next_fn="OrderValidator.validate",
            params={"direction": trade_signal.direction},
        )
        validation = self.execution_engine.validate_only(trade_signal)
        if sig.signal in ("BUY", "SELL"):
            if validation.allowed:
                bus.begin_trade(epic=epic, direction=sig.signal, market=market)
                bus.emit(
                    STAGE_SIGNAL,
                    STATUS_OK,
                    f"{sig.signal} conf={sig.adjusted_confidence:.1f}%",
                    epic=epic,
                    direction=sig.signal,
                    setup_key=sig.setup_key,
                )
                bus.emit(
                    STAGE_VALIDATION,
                    STATUS_OK,
                    "All checks passed",
                    checks=validation.checks,
                )
            else:
                bus.record_validation_block(
                    epic=epic,
                    direction=sig.signal,
                    market=market,
                    signal_message=f"{sig.signal} conf={sig.adjusted_confidence:.1f}%",
                    reasons=validation.reasons or ["Validation failed"],
                )
        else:
            bus.emit(
                STAGE_SIGNAL,
                STATUS_SKIP,
                f"No actionable signal ({sig.signal})",
                epic=epic,
                direction=sig.signal,
            )
        trace_execution(
            "VALIDATION",
            "OrderValidator.validate",
            decision=f"allowed={validation.allowed}",
            next_fn="ExecutionEngine.execute_trade" if validation.allowed else "stop",
            params={"reasons": validation.reasons, "checks": validation.checks},
        )
        if not validation.allowed:
            update_demo_diagnostics(last_rejection="; ".join(validation.reasons))
            if sig.signal in ("BUY", "SELL"):
                log_engine(
                    f"EXEC BLOCKED market={market} epic={epic} validation — "
                    f"{'; '.join(validation.reasons) or 'failed'}"
                )
                log_trade_audit(
                    "validation_fail",
                    market=market,
                    epic=epic,
                    signal=sig.signal,
                    signal_time=quote.time.isoformat(),
                    reasons=validation.reasons,
                )
        execution: ExecutionResult | None = None
        block_reason: str | None = None

        if sig.signal in ("BUY", "SELL"):
            log_engine(
                f"signal generated market={market} dir={sig.signal} "
                f"conf={sig.adjusted_confidence:.1f}% setup={sig.setup_key}"
            )
            log_trade_audit(
                "signal",
                market=market,
                epic=epic,
                signal=sig.signal,
                signal_time=quote.time.isoformat(),
                confidence=sig.adjusted_confidence,
            )

        if self.broker_gate is not None:
            ok, gate_reason = self.broker_gate()
            if not ok:
                block_reason = (
                    gate_reason or "IG connection unavailable — trading disabled"
                )
                update_demo_diagnostics(last_rejection=block_reason)
        elif self.broker_connected is not None and not self.broker_connected():
            block_reason = "IG connection unavailable — trading disabled"
            update_demo_diagnostics(last_rejection=block_reason)

        if self.live_gate is not None and block_reason is None:
            open_count = self.execution_engine.trade_tracker.count_open_for_epic(epic)
            max_pos = self.execution_engine.config.max_positions_per_epic
            if (
                sig.signal in ("BUY", "SELL")
                and self.execution_engine.mode.uses_broker()
            ):
                margin_ok, margin_reason = self.execution_engine.margin_preflight(
                    account_available=self._fetch_account_available(),
                    open_count=open_count,
                    max_positions=max_pos,
                )
                if not margin_ok:
                    block_reason = margin_reason
                    update_demo_diagnostics(last_rejection=margin_reason)
            if block_reason is None:
                allowed_edge, gate_reason = self.live_gate.allow_execution(
                    sig.signal,
                    quote.time,
                    open_count=open_count,
                    max_positions=max_pos,
                )
                if sig.signal in ("BUY", "SELL") and not allowed_edge:
                    block_reason = gate_reason
                    update_demo_diagnostics(last_rejection=gate_reason)
                elif (
                    allowed_edge
                    and sig.signal in ("BUY", "SELL")
                    and validation.allowed
                ):
                    log_engine(f"live gate OPEN — {gate_reason}")
                    log_trade_audit(
                        "live_gate_open",
                        market=market,
                        epic=epic,
                        signal=sig.signal,
                        reason=gate_reason,
                    )

        if sig.signal in ("BUY", "SELL") and not self.auto_trade:
            log_engine(
                f"EXEC BLOCKED market={market} epic={epic} — auto_trade disabled"
            )
            trace_execution(
                "EXECUTION",
                "TradingLoop.process_tick",
                decision="SKIPPED execute_trade — auto_trade disabled",
                params={"auto_trade": self.auto_trade},
            )
            update_demo_diagnostics(
                execute_trade_called=False, last_rejection="auto_trade disabled"
            )
        elif sig.signal not in ("BUY", "SELL"):
            trace_execution(
                "EXECUTION",
                "TradingLoop.process_tick",
                decision=f"SKIPPED execute_trade — signal is {sig.signal}",
                next_fn="end",
            )
            update_demo_diagnostics(execute_trade_called=False)

        can_execute = (
            self.auto_trade
            and sig.signal in ("BUY", "SELL")
            and validation.allowed
            and block_reason is None
        )

        if can_execute and self.execution_engine.mode.uses_broker():
            from execution.live_executor import epic_has_pending_open

            if epic_has_pending_open(epic):
                block_reason = "Order confirm in progress — awaiting IG confirm"
                can_execute = False
                update_demo_diagnostics(last_rejection=block_reason)

        if sig.signal in ("BUY", "SELL") and validation.allowed and block_reason:
            log_engine(f"EXEC BLOCKED market={market} epic={epic} — {block_reason}")
            trace_execution(
                "EXECUTION",
                "TradingLoop.process_tick",
                decision=f"SKIPPED execute_trade — {block_reason}",
            )
            bus.finalize_rejected(block_reason, stage=STAGE_RISK)
            if (
                self.live_gate is not None
                and self.live_gate.armed
                and sig.adjusted_confidence >= 90.0
                and "arming" not in block_reason.lower()
            ):
                log_engine(
                    f"WARN: high-confidence signal blocked post-gate — {block_reason}"
                )
                log_trade_audit(
                    "post_gate_block",
                    market=market,
                    epic=epic,
                    signal=sig.signal,
                    signal_time=quote.time.isoformat(),
                    confidence=sig.adjusted_confidence,
                    reason=block_reason,
                )

        if can_execute:
            cfg = self.execution_engine.config
            gate_exec = trade_signal.gate_execution_params
            from execution.economic_check import integrity_gate_sourced_required

            gate_norm = normalize_gate_execution_params(gate_exec)
            gate_sourced = gate_norm is not None and bool(
                (gate_norm or {}).get("gate_sourced")
            )
            if integrity_gate_sourced_required() and not gate_sourced:
                block_reason = (
                    "INTEGRITY_ABORT: gate_execution_params missing or invalid "
                    "(Profile B requires gate-sourced sizing)"
                )
                log_engine(f"EXEC BLOCKED market={market} epic={epic} — {block_reason}")
                can_execute = False
            if can_execute:
                exec_size = float(
                    (gate_norm or {}).get("actual_size")
                    or self.execution_engine.config.trade_size
                )
                stop_pts = float((gate_norm or {}).get("stop_points") or 0)
                try:
                    from system.learning_demo_policy import effective_policy_snapshot
                    from trading.strictness_resolver import resolve_strictness

                    policy = effective_policy_snapshot(
                        getattr(self.execution_engine, "store", None)
                    )
                    strict = resolve_strictness(
                        cfg, signal_engine=self.signal_engine, market=market
                    )
                    strictness_profile = strict.profile
                except Exception:
                    policy = {}
                    strictness_profile = "unknown"
                risk_gbp_gate = float((gate_norm or {}).get("risk_gbp") or 0)
                log_engine(
                    f"SUBMIT_TRUTH epic={epic} market={market} "
                    f"dir={sig.signal} conf={sig.adjusted_confidence:.1f}% "
                    f"gate_sourced={gate_sourced} size={exec_size} stop={stop_pts:.1f} "
                    f"risk_gbp_gate={risk_gbp_gate:.2f} "
                    f"cap={float((gate_norm or {}).get('risk_cap_gbp') or cfg.get('risk_cap_gbp') or 150):.0f} "
                    f"band={str((gate_norm or {}).get('risk_band') or '')} "
                    f"policy_id={policy.get('policy_id', '')} "
                    f"profile={policy.get('profile', '')} "
                    f"strictness={strictness_profile}"
                )
                log_engine(
                    f"EXEC SUBMIT market={market} epic={epic} "
                    f"dir={sig.signal} conf={sig.adjusted_confidence:.1f}% "
                    f"size={exec_size} gate_sourced={gate_sourced} "
                    f"allow_live_trading={cfg.allow_live_trading} "
                    f"dry_run={cfg.dry_run}"
                )
            if can_execute:
                trace_execution(
                    "EXECUTION",
                    "ExecutionEngine.execute_trade",
                    decision="calling execute_trade",
                    next_fn="ExecutionEngine.execute_trade",
                    params={"direction": sig.signal, "epic": epic},
                )
                update_demo_diagnostics(execute_trade_called=True)
                log_trade_audit(
                    "execution_request",
                    market=market,
                    epic=epic,
                    signal=sig.signal,
                    signal_time=quote.time.isoformat(),
                )
                execution = self.execution_engine.execute_trade(
                    trade_signal, prevalidated=True
                )
            else:
                execution = None
                block_reason = block_reason or (
                    "INTEGRITY_ABORT: gate_execution_params missing or invalid"
                )
            if execution is not None:
                trace_execution(
                    "EXECUTION",
                    "ExecutionEngine.execute_trade",
                    decision=f"result success={execution.success} action={execution.action}",
                    next_fn="LiveExecutor.execute" if execution.success else "rejected",
                    params={
                        "rejection": execution.rejection_reason,
                        "deal_reference": execution.deal_reference,
                    },
                )
            if execution is not None and execution.action == "SUBMITTED":
                log_engine("Order submitted — background worker handling confirm")
            elif execution is not None and not execution.success:
                update_demo_diagnostics(
                    last_rejection=execution.rejection_reason or execution.action
                )
                if self.live_gate is not None and execution.rejection_reason:
                    self.live_gate.note_broker_rejection(
                        execution.rejection_reason,
                        open_count=self.execution_engine.trade_tracker.count_open_for_epic(
                            epic
                        ),
                    )
                if sig.signal in ("BUY", "SELL") and not validation.reasons:
                    bus.finalize_failure(
                        reason=execution.rejection_reason or execution.action
                    )
                log_engine(
                    f"trade rejected reason={execution.rejection_reason or execution.action}"
                )
                log_trade_audit(
                    "execution_rejected",
                    market=market,
                    epic=epic,
                    signal=sig.signal,
                    reason=execution.rejection_reason or execution.action,
                )
            elif execution is not None and execution.action != "SUBMITTED":
                ref = execution.deal_reference or execution.deal_id or ""
                if ref and ref not in self.session_executions:
                    self.session_executions.append(ref)
                log_engine(
                    f"trade executed action={execution.action} "
                    f"deal={execution.deal_reference or execution.deal_id}"
                )
                log_trade_audit(
                    "position_opened",
                    market=market,
                    epic=epic,
                    signal=sig.signal,
                    deal_reference=execution.deal_reference,
                    deal_id=execution.deal_id,
                    action=execution.action,
                )
            if self.journal:
                self.journal.write(
                    market=market,
                    epic=epic,
                    signal=sig.signal,
                    quote=quote,
                    raw_confidence=sig.raw_confidence,
                    adjusted_confidence=sig.adjusted_confidence,
                    learning_delta=sig.learning_delta,
                    setup_key=sig.setup_key,
                    action=execution.action,
                    deal_reference=execution.deal_reference or "",
                    notes=sig.notes,
                )

        outcome = TickOutcome(
            quote=quote,
            signal=sig,
            trade_signal=trade_signal,
            validation=validation,
            execution=execution,
            position_messages=position_messages,
            block_reason=block_reason,
        )
        self.last_outcome = outcome
        return outcome
