"""Shared execution types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from data.models import Quote


class ExecutionMode(str, Enum):
    """TEST = internal simulator; DEMO/LIVE = real IG REST + LiveExecutor."""

    TEST = "TEST"
    DEMO = "DEMO"
    LIVE = "LIVE"

    def uses_simulator(self) -> bool:
        return self == ExecutionMode.TEST

    def uses_broker(self) -> bool:
        return self in (ExecutionMode.DEMO, ExecutionMode.LIVE)


@dataclass
class TradeSignal:
    market: str
    epic: str
    direction: str
    raw_confidence: float
    adjusted_confidence: float
    setup_key: str
    quote: Quote
    snapshot: dict[str, Any] = field(default_factory=dict)
    notes: str = ""


@dataclass
class ExecutionResult:
    success: bool
    action: str
    deal_reference: str | None = None
    deal_id: str | None = None
    rejection_reason: str | None = None
    execution_params: dict[str, Any] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)
