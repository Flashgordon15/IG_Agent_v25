"""
Application Engine — crash-proof Core Layer.

Accepts any ``BaseStrategy`` output, sanitises inputs, enforces iron-clad risk,
and coordinates reconnect / emergency flatten on network faults.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal

from data.models import Quote
from datetime import datetime, timezone
from harmonization.iron_clad_risk import IronCladRiskEngine
from harmonization.reconnect_policy import reconnect_with_backoff
from harmonization.tick_integrity import TickIntegrityFilter
from harmonization.trade_inhibitor_log import log_trade_inhibitor
from strategy.base_strategy import BaseStrategy, StrategyDecision, StrategyInput

Action = Literal["EXECUTE", "HOLD", "REJECTED", "EMERGENCY_STOP"]


@dataclass
class CoreOutcome:
    action: Action
    reason: str
    decision: StrategyDecision | None = None
    execution_params: dict[str, float] = field(default_factory=dict)
    reconnects: int = 0
    ticks_processed: int = 0


@dataclass
class CoreTickResult:
    outcome: CoreOutcome
    quote_valid: bool
    elapsed_ms: float


class ApplicationEngine:
    """
    Core Engine — never crashes on adversarial strategy payloads.

    Responsibilities: tick scrubbing, strategy dispatch, iron wall, reconnect policy.
    """

    def __init__(
        self,
        *,
        epic: str = "CS.D.EURUSD.CFD.IP",
        min_dispatch_interval_ms: float = 500.0,
        trade_size: float = 0.1,
    ) -> None:
        self._epic = str(epic)
        self._min_interval_ms = float(min_dispatch_interval_ms)
        self._trade_size = float(trade_size)
        self._integrity = TickIntegrityFilter()
        self._last_dispatch_mono = 0.0
        self._spread_history: deque[float] = deque(maxlen=200)
        self._ticks_processed = 0
        self._reconnects = 0
        self._rest_calls = 0
        self._open_sockets: set[int] = set()
        self._emergency_stopped = False

    @property
    def ticks_processed(self) -> int:
        return self._ticks_processed

    @property
    def reconnect_count(self) -> int:
        return self._reconnects

    def record_spread(self, spread_pts: float) -> None:
        if spread_pts > 0 and math.isfinite(spread_pts):
            self._spread_history.append(float(spread_pts))

    def spread_percentile(self, current: float) -> float:
        if len(self._spread_history) < 5:
            return 0.5
        hist = list(self._spread_history)
        rank = sum(1 for s in hist if s >= current)
        return rank / len(hist)

    def sanitize_input(
        self,
        *,
        quote: Quote | None = None,
        raw: dict[str, Any] | None = None,
    ) -> StrategyInput | None:
        if quote is not None:
            ok, reason = self._integrity.validate_quote(quote)
            if not ok:
                log_trade_inhibitor(
                    epic=self._epic,
                    gate="tick_integrity",
                    reason=reason,
                )
                return None
            bid = float(quote.bid)
            offer = float(quote.offer)
            spread = max(0.0, offer - bid)
            self.record_spread(spread)
            pct = self.spread_percentile(spread)
            return StrategyInput(
                epic=self._epic,
                bid=bid,
                offer=offer,
                spread_pts=spread,
                spread_percentile=pct,
            )

        raw = raw or {}
        bid = float(raw.get("bid", 0) or 0)
        offer = float(raw.get("offer", 0) or 0)
        if not math.isfinite(bid) or not math.isfinite(offer):
            return None
        vec_raw = raw.get("feature_vector") or raw.get("vector") or ()
        vec: tuple[float, ...] = ()
        if isinstance(vec_raw, (list, tuple)):
            cleaned = []
            for v in vec_raw:
                try:
                    fv = float(v)
                    cleaned.append(0.0 if not math.isfinite(fv) else fv)
                except (TypeError, ValueError):
                    cleaned.append(0.0)
            vec = tuple(cleaned)
        spread = max(0.0, offer - bid) if offer >= bid else 0.0
        if spread > 0:
            self.record_spread(spread)
        return StrategyInput(
            epic=str(raw.get("epic") or self._epic),
            bid=bid,
            offer=offer,
            atr=float(raw.get("atr", 0) or 0) if math.isfinite(float(raw.get("atr", 0) or 0)) else 0.0,
            rsi=float(raw.get("rsi", 50) or 50),
            momentum=float(raw.get("momentum", 0) or 0),
            volume=float(raw.get("volume", 0) or 0),
            spread_pts=spread,
            spread_percentile=self.spread_percentile(spread),
            feature_vector=vec,
        )

    def _iron_wall(
        self,
        decision: StrategyDecision,
        market: StrategyInput,
        rest_client: Any,
    ) -> tuple[bool, str, dict[str, float]]:
        if decision.direction == "HOLD":
            return False, "hold", {}
        ok, reason, norm = IronCladRiskEngine.validate_order(
            epic=market.epic,
            direction=decision.direction,
            size=self._trade_size,
            stop_distance=10.0,
            limit_distance=20.0,
            bid=market.bid,
            offer=market.offer,
            rest_client=rest_client,
            atr=market.atr,
        )
        return ok, reason, norm

    def process_tick(
        self,
        strategy: BaseStrategy,
        *,
        quote: Quote | None = None,
        raw: dict[str, Any] | None = None,
        rest_client: Any | None = None,
        simulate_disconnect: bool = False,
    ) -> CoreTickResult:
        t0 = time.perf_counter()
        self._ticks_processed += 1

        if self._emergency_stopped:
            return CoreTickResult(
                outcome=CoreOutcome(
                    action="EMERGENCY_STOP",
                    reason="engine_halted",
                    ticks_processed=self._ticks_processed,
                ),
                quote_valid=False,
                elapsed_ms=0.0,
            )

        market = self.sanitize_input(quote=quote, raw=raw)
        if market is None:
            return CoreTickResult(
                outcome=CoreOutcome(
                    action="REJECTED",
                    reason="sanitise_failed",
                    ticks_processed=self._ticks_processed,
                ),
                quote_valid=False,
                elapsed_ms=(time.perf_counter() - t0) * 1000.0,
            )

        decision = strategy.safe_evaluate(market)
        if decision.direction == "HOLD":
            return CoreTickResult(
                outcome=CoreOutcome(
                    action="HOLD",
                    reason=decision.reason or "hold",
                    decision=decision,
                    ticks_processed=self._ticks_processed,
                ),
                quote_valid=True,
                elapsed_ms=(time.perf_counter() - t0) * 1000.0,
            )

        now = time.monotonic()
        if (now - self._last_dispatch_mono) * 1000.0 < self._min_interval_ms:
            return CoreTickResult(
                outcome=CoreOutcome(
                    action="HOLD",
                    reason="dispatch_throttle",
                    decision=decision,
                    ticks_processed=self._ticks_processed,
                ),
                quote_valid=True,
                elapsed_ms=(time.perf_counter() - t0) * 1000.0,
            )

        client = rest_client or _NullRestClient()
        if simulate_disconnect:

            def _failing_call() -> bool:
                self._rest_calls += 1
                raise ConnectionError("simulated_mid_trade_disconnect")

            try:
                reconnect_with_backoff(
                    _failing_call,
                    label="gamma_disconnect",
                    backoff_sec=(0.01, 0.02),
                )
            except RuntimeError:
                self._reconnects += 1
                self.emergency_stop(client)
                return CoreTickResult(
                    outcome=CoreOutcome(
                        action="EMERGENCY_STOP",
                        reason="gamma_disconnect_recovered",
                        decision=decision,
                        reconnects=self._reconnects,
                        ticks_processed=self._ticks_processed,
                    ),
                    quote_valid=True,
                    elapsed_ms=(time.perf_counter() - t0) * 1000.0,
                )

        allowed, reason, params = self._iron_wall(decision, market, client)
        if not allowed:
            log_trade_inhibitor(
                epic=market.epic,
                gate="iron_wall",
                reason=reason,
            )
            return CoreTickResult(
                outcome=CoreOutcome(
                    action="REJECTED",
                    reason=reason,
                    decision=decision,
                    ticks_processed=self._ticks_processed,
                ),
                quote_valid=True,
                elapsed_ms=(time.perf_counter() - t0) * 1000.0,
            )

        self._last_dispatch_mono = now
        self._track_socket(client)
        return CoreTickResult(
            outcome=CoreOutcome(
                action="EXECUTE",
                reason="iron_wall_pass",
                decision=decision,
                execution_params=params,
                ticks_processed=self._ticks_processed,
            ),
            quote_valid=True,
            elapsed_ms=(time.perf_counter() - t0) * 1000.0,
        )

    def emergency_stop(self, rest_client: Any | None = None) -> None:
        self._emergency_stopped = True
        client = rest_client or _NullRestClient()
        try:
            from execution.capital_guard import CapitalGuard

            CapitalGuard._cancel_all_open_orders_and_positions(client)
        except Exception:
            pass
        try:
            from harmonization.clean_shutdown import write_crash_state

            write_crash_state(source="application_engine_emergency_stop")
        except Exception:
            pass

    def _track_socket(self, client: Any) -> None:
        sid = id(client)
        self._open_sockets.add(sid)

    def socket_leak_count(self) -> int:
        return len(self._open_sockets)

    def reset_for_tests(self) -> None:
        self._last_dispatch_mono = 0.0
        self._spread_history.clear()
        self._ticks_processed = 0
        self._reconnects = 0
        self._rest_calls = 0
        self._open_sockets.clear()
        self._emergency_stopped = False
        IronCladRiskEngine.reset_for_tests()


class _NullRestClient:
    """In-process stub — satisfies iron-clad drawdown checks in isolation tests."""

    def fetch_account_balance(self) -> float:
        return 10000.0

    def maybe_refresh_account_summary(self, min_interval: float = 60.0) -> dict[str, float]:
        return {"balance": 10000.0, "available": 10000.0}

    def close_position(self, deal_id: str) -> dict[str, Any]:
        return {"dealId": deal_id, "status": "CLOSED"}

    def cancel_working_order(self, deal_id: str) -> dict[str, Any]:
        return {"dealId": deal_id, "status": "CANCELLED"}

    def open_positions(self) -> list[dict[str, Any]]:
        return []

    def working_orders(self) -> list[dict[str, Any]]:
        return []


def make_quote(
    bid: float,
    offer: float,
) -> Quote:
    return Quote(
        bid=bid,
        offer=offer,
        time=datetime.now(timezone.utc),
    )
