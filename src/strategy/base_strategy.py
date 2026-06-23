"""
BaseStrategy — Decision Engine contract.

Strategies ingest sanitised numeric inputs only and emit BUY | SELL | HOLD.
The Application Layer (``core.application_engine``) owns all IG REST, SHM, and risk.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

Direction = Literal["BUY", "SELL", "HOLD"]


@dataclass(frozen=True)
class StrategyInput:
    """Clean numeric feed — no broker handles, no SHM pointers."""

    epic: str
    bid: float
    offer: float
    atr: float = 0.0
    rsi: float = 50.0
    momentum: float = 0.0
    volume: float = 0.0
    spread_pts: float = 0.0
    spread_percentile: float = 0.5
    feature_vector: tuple[float, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class StrategyDecision:
    """Binary-ish strategy output — Application Layer enforces iron-clad risk."""

    direction: Direction
    confidence: float
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def normalized(self) -> StrategyDecision:
        """Clamp confidence and coerce invalid directions to HOLD."""
        conf = float(self.confidence)
        if not math.isfinite(conf):
            conf = 0.0
        conf = max(0.0, min(100.0, conf))
        direction: Direction = self.direction
        if direction not in ("BUY", "SELL", "HOLD"):
            direction = "HOLD"
        return StrategyDecision(
            direction=direction,
            confidence=conf,
            reason=str(self.reason or ""),
            metadata=dict(self.metadata or {}),
        )


class BaseStrategy(ABC):
    """Strategy plugin — must never touch REST, SHM, or execution directly."""

    strategy_id: str = "base"

    @abstractmethod
    def evaluate(self, market: StrategyInput) -> StrategyDecision:
        """Return a trade intent from clean numbers only."""

    def safe_evaluate(self, market: StrategyInput) -> StrategyDecision:
        """Wrapper — never raises; malformed strategy code becomes HOLD."""
        try:
            return self.evaluate(market).normalized()
        except Exception as exc:
            return StrategyDecision(
                direction="HOLD",
                confidence=0.0,
                reason=f"strategy_error:{type(exc).__name__}",
            )
