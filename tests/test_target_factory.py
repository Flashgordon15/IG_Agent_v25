"""Target factory — live-fire ledger reconciliation gates."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from harmonization.iron_clad_risk import (
    MANDATORY_LIMIT_POINTS,
    MANDATORY_STOP_POINTS,
    MAX_ORDER_SIZE,
    IronCladRiskEngine,
)
from harmonization.volatility_gate import dynamic_confidence_floor
from intelligence.matrix_prebaker import FFILL_STREAMING_EPICS
from target_reconciliation.live_fire_ledger import (
    TARGET_NET_PNL_GBP,
    TARGET_WIN_RATE,
    _detect_phantom_rows,
    _ledger_metrics,
    reconcile_trading_ledger,
    write_trading_ledger,
)


class IronCladEnvelopeTests(unittest.TestCase):
    def test_mandatory_stop_limit_floors(self) -> None:
        self.assertEqual(MANDATORY_STOP_POINTS, 10.0)
        self.assertEqual(MANDATORY_LIMIT_POINTS, 20.0)
        self.assertEqual(MAX_ORDER_SIZE, 1.0)

    def test_validate_order_enforces_floors(self) -> None:
        IronCladRiskEngine.reset_for_tests()
        ok, reason, norm = IronCladRiskEngine.validate_order(
            epic="CS.D.EURUSD.CFD.IP",
            direction="BUY",
            size=0.5,
            stop_distance=3.0,
            limit_distance=5.0,
            bid=1.1000,
            offer=1.1002,
            rest_client=MagicMock(),
        )
        self.assertTrue(ok, reason)
        self.assertGreaterEqual(norm["stop_distance"], MANDATORY_STOP_POINTS)
        self.assertGreaterEqual(norm["limit_distance"], MANDATORY_LIMIT_POINTS)


class VolatilityGateTests(unittest.TestCase):
    def test_low_vol_lowers_threshold(self) -> None:
        base = 52.5
        low = dynamic_confidence_floor(
            base_threshold=base, atr=0.8, atr_baseline=2.0, rsi=50.0
        )
        high = dynamic_confidence_floor(
            base_threshold=base, atr=2.5, atr_baseline=2.0, rsi=50.0
        )
        self.assertLess(low["adjusted_threshold"], base)
        self.assertGreaterEqual(high["adjusted_threshold"], low["adjusted_threshold"])


class FfillEpicCoverageTests(unittest.TestCase):
    def test_night_matrix_epics_covered(self) -> None:
        required = {
            "CS.D.CFPGOLD.CFP.IP",
            "IX.D.DOW.IFM.IP",
            "IX.D.NIKKEI.IFM.IP",
            "CS.D.EURUSD.CFD.IP",
        }
        self.assertTrue(required.issubset(FFILL_STREAMING_EPICS))


class LedgerReconciliationTests(unittest.TestCase):
    def test_phantom_rows_detected(self) -> None:
        rows = [
            {"deal_id": "", "pnl_gbp": 1.0, "result": "CLOSED", "source": "shadow_simulator"},
            {"deal_id": "DI123", "pnl_gbp": 1.0, "result": "CLOSED", "source": "ig_rest"},
        ]
        phantoms = _detect_phantom_rows(rows)
        self.assertGreaterEqual(len(phantoms), 1)

    def test_metrics_win_rate(self) -> None:
        rows = [
            {"pnl_gbp": 50.0, "status": "CLOSED"},
            {"pnl_gbp": -10.0, "status": "CLOSED"},
            {"pnl_gbp": 30.0, "status": "CLOSED"},
        ]
        metrics = _ledger_metrics(rows)
        self.assertEqual(metrics["wins"], 2)
        self.assertEqual(metrics["losses"], 1)
        self.assertAlmostEqual(metrics["win_rate"], 2 / 3, places=3)

    def test_reconcile_writes_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "trading_ledger.json"
            with patch(
                "target_reconciliation.live_fire_ledger.LEDGER_PATH", ledger_path
            ), patch(
                "target_reconciliation.live_fire_ledger.audit_architecture",
                return_value={"ok": True, "issues": []},
            ), patch(
                "target_reconciliation.live_fire_ledger._fetch_gate_blockers",
                return_value=[],
            ), patch(
                "target_reconciliation.live_fire_ledger._broker_closed_rows",
                return_value=[
                    {
                        "deal_id": "DI999",
                        "pnl_gbp": 1200.0,
                        "status": "CLOSED",
                        "source": "ig_rest",
                    },
                    {
                        "deal_id": "DI998",
                        "pnl_gbp": 50.0,
                        "status": "CLOSED",
                        "source": "ig_rest",
                    },
                ],
            ), patch(
                "target_reconciliation.live_fire_ledger._broker_open_rows",
                return_value=[],
            ):
                payload = reconcile_trading_ledger(hours=24)
            self.assertTrue(ledger_path.is_file())
            self.assertTrue(payload["targets_met"])
            self.assertGreaterEqual(
                payload["metrics"]["net_pnl_gbp"], TARGET_NET_PNL_GBP
            )
            self.assertGreaterEqual(payload["metrics"]["win_rate"], TARGET_WIN_RATE)


class TargetConstantsTests(unittest.TestCase):
    def test_profit_and_win_rate_targets(self) -> None:
        self.assertEqual(TARGET_NET_PNL_GBP, 1000.0)
        self.assertEqual(TARGET_WIN_RATE, 0.60)


if __name__ == "__main__":
    unittest.main()
