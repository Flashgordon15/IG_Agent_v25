"""Live vol-regime soft gate for indices."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.live_regime_gate import (
    atr_percentile_rank,
    momentum_vol_penalty,
    reset_live_regime_cache_for_tests,
)


class LiveRegimeGateTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_live_regime_cache_for_tests()

    def test_atr_percentile_rank(self) -> None:
        series = pd.Series([10.0] * 90 + [20.0])
        pct = atr_percentile_rank(series)
        self.assertIsNotNone(pct)
        assert pct is not None
        self.assertGreaterEqual(pct, 90.0)

    def test_no_penalty_for_fx(self) -> None:
        mult, detail = momentum_vol_penalty(
            "CS.D.EURUSD.CFD.IP", {"vol_regime": "high"}
        )
        self.assertEqual(mult, 1.0)
        self.assertEqual(detail, "")

    @patch("system.live_regime_gate.live_vol_soft_gate_enabled", return_value=True)
    @patch(
        "system.live_regime_gate.index_epics",
        return_value=frozenset({"IX.D.DOW.IFM.IP"}),
    )
    @patch("system.live_regime_gate._atr_percentile_from_engine", return_value=96.0)
    @patch("system.live_regime_gate.atr_percentile_block_above", return_value=95.0)
    @patch("system.live_regime_gate.extreme_vol_penalty_pct", return_value=15.0)
    def test_index_extreme_vol_penalty(
        self,
        _a: MagicMock,
        _b: MagicMock,
        _c: MagicMock,
        _d: MagicMock,
        _e: MagicMock,
    ) -> None:
        engine = MagicMock()
        mult, detail = momentum_vol_penalty(
            "IX.D.DOW.IFM.IP",
            {},
            signal_engine=engine,
            market="Wall Street",
        )
        self.assertAlmostEqual(mult, 0.85)
        self.assertIn("momentum", detail.lower())


if __name__ == "__main__":
    unittest.main()
