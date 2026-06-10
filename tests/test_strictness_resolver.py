"""Velocity-driven strictness resolver."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading.strictness_resolver import (  # noqa: E402
    StrictnessLimits,
    VelocityMetrics,
    _profile_from_velocity,
    is_low_volatility_regime,
    resolve_strictness,
)


class VelocityStrictnessTests(unittest.TestCase):
    def test_loose_regime_on_high_vol_and_clean_trend(self) -> None:
        profile = _profile_from_velocity(
            VelocityMetrics(normalized_volatility=1.5, trend_cleanliness=25.0)
        )
        self.assertEqual(profile, "loose")
        limits = resolve_strictness(
            velocity_metrics=VelocityMetrics(
                normalized_volatility=1.5, trend_cleanliness=30.0
            )
        )
        self.assertEqual(limits.profile, "loose")
        self.assertEqual(limits.fitness_floor, 30.0)
        self.assertEqual(limits.rsi_buy_max, 100.0)
        self.assertEqual(limits.rsi_sell_min, 0.0)

    def test_strict_regime_on_low_volatility(self) -> None:
        profile = _profile_from_velocity(
            VelocityMetrics(normalized_volatility=0.6, trend_cleanliness=10.0)
        )
        self.assertEqual(profile, "strict")
        limits = resolve_strictness(
            velocity_metrics=VelocityMetrics(
                normalized_volatility=0.65, trend_cleanliness=5.0
            )
        )
        self.assertEqual(limits.profile, "strict")
        self.assertEqual(limits.fitness_floor, 55.0)

    def test_firm_regime_default_and_mid_vol(self) -> None:
        self.assertEqual(_profile_from_velocity(None), "firm")
        profile = _profile_from_velocity(
            VelocityMetrics(normalized_volatility=1.0, trend_cleanliness=20.0)
        )
        self.assertEqual(profile, "firm")
        # High vol without clean trend stays firm (not loose).
        profile = _profile_from_velocity(
            VelocityMetrics(normalized_volatility=1.5, trend_cleanliness=10.0)
        )
        self.assertEqual(profile, "firm")

    def test_resolve_from_signal_engine_market(self) -> None:
        engine = MagicMock()
        engine.quote_df.return_value = object()
        engine.candles.side_effect = lambda df, n: [1] * 300
        engine.add_indicators.side_effect = lambda df: df

        import pandas as pd

        c5 = pd.DataFrame({"atr": [1.0] * 300})
        c5.iloc[-2, c5.columns.get_loc("atr")] = 1.4
        c15 = pd.DataFrame(
            {
                "fast_ema": [100.0],
                "slow_ema": [99.0],
                "rsi": [55.0],
                "atr": [1.0],
            }
        )

        def candles_side_effect(df, n):
            return c5 if n == 5 else c15

        engine.candles.side_effect = candles_side_effect
        engine.add_indicators.side_effect = lambda df: df

        limits = resolve_strictness(signal_engine=engine, market="Gold")
        self.assertIsInstance(limits, StrictnessLimits)
        self.assertIn(limits.profile, ("loose", "firm", "strict"))

    def test_is_low_volatility_regime_below_strict_threshold(self) -> None:
        from unittest.mock import patch

        engine = MagicMock()
        with patch(
            "trading.strictness_resolver.compute_velocity_metrics",
            return_value=VelocityMetrics(
                normalized_volatility=0.5, trend_cleanliness=10.0
            ),
        ):
            self.assertTrue(
                is_low_volatility_regime(signal_engine=engine, market="Gold")
            )

    def test_is_low_volatility_regime_above_strict_threshold(self) -> None:
        from unittest.mock import patch

        engine = MagicMock()
        with patch(
            "trading.strictness_resolver.compute_velocity_metrics",
            return_value=VelocityMetrics(
                normalized_volatility=1.0, trend_cleanliness=10.0
            ),
        ):
            self.assertFalse(
                is_low_volatility_regime(signal_engine=engine, market="Gold")
            )


if __name__ == "__main__":
    unittest.main()
