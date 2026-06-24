"""Platform V2 — adaptive vol, compound escalation, feature drift."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from harmonization.iron_clad_risk import IronCladRiskEngine, MAX_ORDER_SIZE
from intelligence.matrix_prebaker import (
    COL_ATR_ANCHOR,
    COL_RSI_ANCHOR,
    COL_SAMPLES,
    MATRIX_COLS,
    TOTAL_CELLS,
    calibrate_live_tick_features,
    epic_slot,
    matrix_cell_index,
)
from platform_v2.adaptive_volatility_scalping import (
    dynamic_slip_tolerance,
    epic_matrix_atr_stats,
    reset_adaptive_volatility_for_tests,
)
from platform_v2.compound_profit_escalation import (
    apply_compound_escalation,
    read_session_net_profit_gbp,
    reset_compound_escalation_for_tests,
    tier_multiplier_for_profit,
)
from platform_v2.feature_drift_calibration import calibrate_live_features


def _seed_epic_matrix(matrix: np.ndarray, epic: str, *, rsi: float, atr: float) -> None:
    idx = matrix_cell_index(
        epic_id=epic_slot(epic),
        direction="BUY",
        rsi_q=8,
        atr_q=4,
        mom_q=4,
    )
    row = matrix[idx]
    row[COL_SAMPLES] = 5.0
    row[COL_RSI_ANCHOR] = rsi
    row[COL_ATR_ANCHOR] = atr


class AdaptiveVolatilityTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_adaptive_volatility_for_tests()

    def test_matrix_atr_stats_from_cells(self) -> None:
        matrix = np.zeros((TOTAL_CELLS, MATRIX_COLS), dtype=np.float32)
        _seed_epic_matrix(matrix, "CS.D.CRUDE.CFD.IP", rsi=55.0, atr=2.5)
        mean, std, n = epic_matrix_atr_stats("CS.D.CRUDE.CFD.IP", matrix=matrix)
        self.assertAlmostEqual(mean, 2.5, places=3)
        self.assertGreater(std, 0.0)
        self.assertEqual(n, 1)

    def test_breakout_expands_slip_tolerance(self) -> None:
        matrix = np.zeros((TOTAL_CELLS, MATRIX_COLS), dtype=np.float32)
        _seed_epic_matrix(matrix, "IX.D.FTSE.IFM.IP", rsi=50.0, atr=1.0)
        lull = dynamic_slip_tolerance(
            epic="IX.D.FTSE.IFM.IP",
            atr_live=0.8,
            spread=1.0,
        )
        surge = dynamic_slip_tolerance(
            epic="IX.D.FTSE.IFM.IP",
            atr_live=5.0,
            spread=3.5,
        )
        self.assertGreater(surge.slip_tolerance, lull.slip_tolerance)
        self.assertTrue(surge.breakout)


class CompoundEscalationTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_compound_escalation_for_tests()

    def test_tier_steps_every_200_gbp(self) -> None:
        self.assertEqual(tier_multiplier_for_profit(0)[0], 1.0)
        self.assertEqual(tier_multiplier_for_profit(199)[0], 1.0)
        self.assertEqual(tier_multiplier_for_profit(200)[0], 1.5)
        self.assertEqual(tier_multiplier_for_profit(600)[0], 4.0)

    def test_ledger_net_profit_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trading_ledger.json"
            path.write_text(
                json.dumps(
                    {
                        "metrics": {"net_pnl_gbp": 450.0},
                        "closed_trades": [],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(read_session_net_profit_gbp(ledger_path=path), 450.0)

    def test_defensive_reset_on_drawdown(self) -> None:
        with patch.dict(
            os.environ,
            {},
            clear=False,
        ):
            esc = apply_compound_escalation(1.0, session_equity_gbp=980.0)
            reset_compound_escalation_for_tests()
            _ = apply_compound_escalation(1.0, session_equity_gbp=1000.0)
            esc2 = apply_compound_escalation(2.0, session_equity_gbp=980.0)
            self.assertTrue(esc2.defensive_reset)
            self.assertEqual(esc2.tier_multiplier, 1.0)
            self.assertGreater(esc.size, 0)


class FeatureDriftTests(unittest.TestCase):
    def test_no_drift_when_within_band(self) -> None:
        matrix = np.zeros((TOTAL_CELLS, MATRIX_COLS), dtype=np.float32)
        _seed_epic_matrix(matrix, "IX.D.DAX.IFM.IP", rsi=50.0, atr=1.5)
        result = calibrate_live_features(
            epic="IX.D.DAX.IFM.IP",
            rsi=51.0,
            atr=1.55,
            momentum=0.5,
            matrix=matrix,
        )
        self.assertFalse(result.drifted)
        self.assertEqual(result.scale_multiplier, 1.0)

    def test_drift_pulls_toward_training_centroid(self) -> None:
        matrix = np.zeros((TOTAL_CELLS, MATRIX_COLS), dtype=np.float32)
        _seed_epic_matrix(matrix, "CS.D.EURUSD.CFD.IP", rsi=50.0, atr=1.5)
        result = calibrate_live_features(
            epic="CS.D.EURUSD.CFD.IP",
            rsi=95.0,
            atr=8.0,
            momentum=12.0,
            matrix=matrix,
        )
        self.assertTrue(result.drifted)
        self.assertLess(result.rsi, 95.0)
        self.assertLess(result.atr, 8.0)

    def test_matrix_prebaker_wrapper(self) -> None:
        matrix = np.zeros((TOTAL_CELLS, MATRIX_COLS), dtype=np.float32)
        _seed_epic_matrix(matrix, "CS.D.CFPGOLD.CFP.IP", rsi=48.0, atr=2.0)
        with patch("platform_v2.platform_v2_enabled", return_value=True):
            rsi, atr, mom, meta = calibrate_live_tick_features(
                "CS.D.CFPGOLD.CFP.IP",
                90.0,
                9.0,
                5.0,
                matrix=matrix,
            )
        self.assertLess(rsi, 90.0)
        self.assertTrue(meta.get("drifted") or meta.get("max_z", 0) > 0)


class IronCladV2IntegrationTests(unittest.TestCase):
    def test_v2_max_size_when_enabled(self) -> None:
        IronCladRiskEngine.reset_for_tests()
        with patch("platform_v2.platform_v2_enabled", return_value=True):
            self.assertGreater(IronCladRiskEngine.effective_max_order_size(), MAX_ORDER_SIZE)

    def test_v2_slip_tolerance_delegates(self) -> None:
        with patch("platform_v2.platform_v2_enabled", return_value=True):
            tol = IronCladRiskEngine.entry_spread_tolerance_points(
                "CS.D.CRUDE.CFD.IP",
                atr=4.0,
                bid=70.0,
                offer=70.5,
            )
        self.assertGreaterEqual(tol, 1.5)


if __name__ == "__main__":
    unittest.main()
