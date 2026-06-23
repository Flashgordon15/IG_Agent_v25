"""
Adversarial strategy payloads — stress the Application Layer in isolation tests.
"""

from __future__ import annotations

import math
import random
import time
from typing import Any

from strategy.base_strategy import BaseStrategy, StrategyDecision, StrategyInput


class StrategyAlpha(BaseStrategy):
    """Erratic high-frequency random signals — tests REST handle stability."""

    strategy_id = "adversarial_alpha"

    def __init__(self, *, seed: int = 42) -> None:
        self._rng = random.Random(seed)
        self._tick_count = 0

    def evaluate(self, market: StrategyInput) -> StrategyDecision:
        self._tick_count += 1
        roll = self._rng.random()
        if roll < 0.33:
            direction = "BUY"
        elif roll < 0.66:
            direction = "SELL"
        else:
            direction = "HOLD"
        return StrategyDecision(
            direction=direction,  # type: ignore[arg-type]
            confidence=self._rng.uniform(40.0, 99.0),
            reason="alpha_random_hf",
            metadata={"tick": self._tick_count, "ts": time.time()},
        )


class StrategyBeta(BaseStrategy):
    """Malformed vectors, NaN, empty payloads — tests data scrubbing."""

    strategy_id = "adversarial_beta"

    def evaluate(self, market: StrategyInput) -> StrategyDecision:
        payload: dict[str, Any] = {
            "empty_vector": [],
            "nan_confidence": float("nan"),
            "inf_bid": float("inf"),
            "garbage": None,
        }
        if market.feature_vector:
            payload["vector"] = [float("nan")] * len(market.feature_vector)
        direction = "BUY" if math.isnan(market.bid) else "SELL"
        return StrategyDecision(
            direction=direction,  # type: ignore[arg-type]
            confidence=float("nan"),
            reason="beta_malformed",
            metadata=payload,
        )


class StrategyGamma(BaseStrategy):
    """Simulates disconnect intent — core must emergency-stop safely."""

    strategy_id = "adversarial_gamma"

    def __init__(self) -> None:
        self._armed = False
        self._ticks = 0

    def evaluate(self, market: StrategyInput) -> StrategyDecision:
        self._ticks += 1
        if self._ticks >= 3:
            self._armed = True
        return StrategyDecision(
            direction="BUY",
            confidence=88.0,
            reason="gamma_disconnect_probe",
            metadata={"disconnect_armed": self._armed, "tick": self._ticks},
        )

    @property
    def disconnect_armed(self) -> bool:
        return self._armed
