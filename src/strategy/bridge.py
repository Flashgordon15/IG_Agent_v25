"""
Strategy bridge — wires Decision Engine into TradingLoop without coupling layers.

TradingLoop calls ``evaluate_production_strategy`` for clean BUY/SELL/HOLD only;
Application Layer retains REST, SHM, and iron-clad risk.
"""

from __future__ import annotations

from typing import Any

from core.application_engine import ApplicationEngine
from data.models import Quote
from strategy.base_strategy import StrategyDecision
from strategy.production_strategy import ProductionMLStrategy

_ENGINE_CACHE: dict[str, ApplicationEngine] = {}


def _engine_for_epic(epic: str) -> ApplicationEngine:
    key = str(epic or "")
    eng = _ENGINE_CACHE.get(key)
    if eng is None:
        eng = ApplicationEngine(epic=key, min_dispatch_interval_ms=500.0)
        _ENGINE_CACHE[key] = eng
    return eng


def evaluate_production_strategy(
    *,
    epic: str,
    quote: Quote,
    atr: float = 0.0,
    rsi: float = 50.0,
    momentum: float = 0.0,
    volume: float = 0.0,
    ml_probability: float | None = None,
    hub_spread_percentile: float | None = None,
) -> StrategyDecision:
    """Run production ML strategy — returns sanitised decision only."""
    engine = _engine_for_epic(epic)
    spread = max(0.0, float(quote.offer) - float(quote.bid))
    engine.record_spread(spread)
    pct = (
        float(hub_spread_percentile)
        if hub_spread_percentile is not None
        else engine.spread_percentile(spread)
    )
    raw: dict[str, Any] = {
        "epic": epic,
        "bid": quote.bid,
        "offer": quote.offer,
        "atr": atr,
        "rsi": rsi,
        "momentum": momentum,
        "volume": volume,
        "spread_percentile": pct,
    }
    market = engine.sanitize_input(raw=raw)
    if market is None:
        from strategy.base_strategy import StrategyDecision as SD

        return SD(direction="HOLD", confidence=0.0, reason="bridge_sanitise_failed")
    strat = ProductionMLStrategy(ml_probability=ml_probability)
    return strat.safe_evaluate(market)


def reset_strategy_bridge_for_tests() -> None:
    _ENGINE_CACHE.clear()
