"""
Production ML Strategy — Decision Engine with go-live boundaries.

- Trade only when spread is in the lowest 30% of rolling history.
- Dynamic confidence floor (min 75%) scaled by live volume.
- Outputs BUY/SELL/HOLD only; iron-clad risk enforced in Application Layer.
"""

from __future__ import annotations

import math

from harmonization.volatility_gate import NO_TRADE_PARADOX_MIN_PCT, no_trade_paradox_threshold
from strategy.base_strategy import BaseStrategy, StrategyDecision, StrategyInput

SPREAD_PERCENTILE_MAX = 0.30
VOLUME_FLOOR_REF = 1000.0
CONFIDENCE_FLOOR_MIN = NO_TRADE_PARADOX_MIN_PCT


class ProductionMLStrategy(BaseStrategy):
    """Optimal ML strategy operating inside production boundaries."""

    strategy_id = "production_ml_v29"

    def __init__(
        self,
        *,
        ml_probability: float | None = None,
        spread_percentile_cap: float = SPREAD_PERCENTILE_MAX,
    ) -> None:
        self._ml_override = ml_probability
        self._spread_cap = float(spread_percentile_cap)

    def _volume_scaled_floor(self, volume: float, atr: float) -> float:
        vol = max(float(volume or 0), 1.0)
        vol_ratio = min(vol / VOLUME_FLOOR_REF, 2.0)
        bonus = max(0.0, (vol_ratio - 0.5) * 4.0)
        floor = CONFIDENCE_FLOOR_MIN + min(bonus, 5.0)
        return no_trade_paradox_threshold(
            floor,
            atr=max(float(atr or 0), 1.0),
            atr_baseline=max(float(atr or 10), 10.0),
            rsi=50.0,
        )

    def _infer_ml_probability(self, market: StrategyInput) -> float:
        if self._ml_override is not None:
            return float(self._ml_override)
        vec = market.feature_vector
        if vec:
            finite = [v for v in vec if math.isfinite(v)]
            if finite:
                return max(0.0, min(1.0, sum(finite) / len(finite) / 100.0))
        rsi_edge = abs(market.rsi - 50.0) / 50.0
        mom = abs(market.momentum)
        raw = 0.55 + rsi_edge * 0.2 + min(mom, 1.0) * 0.15
        return max(0.0, min(1.0, raw))

    def evaluate(self, market: StrategyInput) -> StrategyDecision:
        spread_pct = float(market.spread_percentile)
        if spread_pct > self._spread_cap:
            return StrategyDecision(
                direction="HOLD",
                confidence=0.0,
                reason=f"spread_percentile {spread_pct:.2f} > cap {self._spread_cap:.2f}",
            )

        ml_prob = self._infer_ml_probability(market)
        confidence_pct = ml_prob * 100.0
        floor = self._volume_scaled_floor(market.volume, market.atr)

        if confidence_pct < floor:
            return StrategyDecision(
                direction="HOLD",
                confidence=confidence_pct,
                reason=f"confidence {confidence_pct:.1f}% < floor {floor:.1f}%",
            )

        if market.rsi >= 55 and market.momentum >= 0:
            direction = "BUY"
        elif market.rsi <= 45 and market.momentum <= 0:
            direction = "SELL"
        else:
            direction = "HOLD"
            return StrategyDecision(
                direction="HOLD",
                confidence=confidence_pct,
                reason="neutral_regime",
            )

        return StrategyDecision(
            direction=direction,
            confidence=confidence_pct,
            reason="production_ml_pass",
            metadata={
                "ml_prob": round(ml_prob, 4),
                "floor_pct": round(floor, 2),
                "spread_percentile": round(spread_pct, 4),
            },
        )
