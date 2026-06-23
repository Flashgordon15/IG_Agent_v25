"""Order execution builder — force-injected gate_execution_params for every dispatch."""

from __future__ import annotations

from typing import Any

from data.models import Quote
from execution.types import TradeSignal, force_inject_gate_execution_params, normalize_gate_execution_params


def build_trade_signal_with_gate_params(
    *,
    market: str,
    epic: str,
    direction: str,
    raw_confidence: float,
    adjusted_confidence: float,
    setup_key: str,
    quote: Quote,
    snapshot: dict[str, Any] | None = None,
    notes: str = "",
    gate_execution_params: dict[str, Any] | None = None,
    trade_size: float = 1.0,
) -> TradeSignal:
    """Build TradeSignal with non-optional gate_execution_params schema."""
    injected = force_inject_gate_execution_params(
        epic=epic,
        size=trade_size,
        gate_execution_params=gate_execution_params,
    )
    normalized = normalize_gate_execution_params(injected) or injected
    return TradeSignal(
        market=market,
        epic=epic,
        direction=direction,
        raw_confidence=raw_confidence,
        adjusted_confidence=adjusted_confidence,
        setup_key=setup_key,
        quote=quote,
        snapshot=dict(snapshot or {}),
        notes=notes,
        gate_execution_params=normalized,
    )
