"""TEST mode — simulated fills using Config simulation parameters."""

from __future__ import annotations

import random
import string
import time
from typing import Any

from data.models import Quote
from execution.cooldown_tracker import CooldownTracker
from execution.trade_manager import TradeManager
from execution.types import ExecutionResult, TradeSignal
from system.config import Config
from system.trade_lifecycle_bus import (
    STAGE_IG_RESPONSE,
    STAGE_POSITION_OPENED,
    STATUS_OK,
    get_lifecycle_bus,
)


class TestSimulator:
    def __init__(
        self,
        config: Config,
        store: Any,
        trade_manager: TradeManager,
        cooldown: CooldownTracker,
    ) -> None:
        self._cfg = config
        self.store = store
        self.trade_manager = trade_manager
        self.cooldown = cooldown

    @property
    def config(self) -> Config:
        return self._cfg

    def execute(self, signal: TradeSignal, execution_params: dict[str, Any]) -> ExecutionResult:
        cfg = self._cfg
        if cfg.simulated_latency_ms > 0:
            time.sleep(cfg.simulated_latency_ms / 1000.0)

        slippage_pts = min(
            cfg.simulated_slippage * (2.0 - cfg.simulated_fill_quality),
            cfg.max_slippage_points,
        )

        # Build a fill quote, optionally adjusting the fill price by slippage.
        fill_quote = signal.quote
        if cfg.sim_apply_slippage and slippage_pts > 0:
            from dataclasses import replace as _dc_replace
            if signal.direction == "BUY":
                fill_quote = _dc_replace(signal.quote, offer=signal.quote.offer + slippage_pts)
            else:
                fill_quote = _dc_replace(signal.quote, bid=signal.quote.bid - slippage_pts)

        ref = "SIM-" + "".join(random.choices(string.ascii_uppercase, k=8))
        bus = get_lifecycle_bus()
        bus.emit(STAGE_IG_RESPONSE, STATUS_OK, f"simulated ref={ref}", deal_reference=ref)
        self.cooldown.record(signal.epic, direction=signal.direction)

        trade_id = self.trade_manager.open_trade_from_execution(
            market=signal.market, epic=signal.epic, side=signal.direction,
            quote=fill_quote, raw_confidence=signal.raw_confidence,
            adjusted_confidence=signal.adjusted_confidence, setup_key=signal.setup_key,
            deal_reference=ref,
            notes=f"{signal.notes} | slippage={slippage_pts:.1f}pts fill_q={cfg.simulated_fill_quality:.2f}",
            execution=execution_params, dry_run=True,
        )
        bus.emit(
            STAGE_POSITION_OPENED,
            STATUS_OK,
            f"simulated trade_id={trade_id}",
            trade_id=trade_id,
            epic=signal.epic,
        )

        return ExecutionResult(
            success=True,
            action="SIMULATED",
            deal_reference=ref,
            execution_params=execution_params,
            messages=[f"Simulated {signal.direction} id={trade_id} spread_mult={cfg.simulated_spread_multiplier}"],
        )

    def update_positions(self, market: str, epic: str, quote: Quote) -> list[str]:
        return self.trade_manager.update_from_quote(market, epic, quote)

    def has_open_position(self, epic: str) -> bool:
        return self.store.has_open_trade(epic)

    def reset(self) -> None:
        self.cooldown._last_trade.clear()
