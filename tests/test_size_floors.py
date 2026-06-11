"""Operational epic size floors and gate execution param normalization."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from execution.size_floors import apply_operational_size_floor  # noqa: E402
from execution.types import normalize_gate_execution_params  # noqa: E402


class OperationalSizeFloorTests(unittest.TestCase):
    def test_index_floor(self) -> None:
        self.assertEqual(
            apply_operational_size_floor(0.05, "IX.D.NASDAQ.IFM.IP"),
            0.20,
        )

    def test_fx_floor(self) -> None:
        self.assertEqual(
            apply_operational_size_floor(0.5, "CS.D.GBPUSD.CFD.IP"),
            2.0,
        )

    def test_gold_floor(self) -> None:
        self.assertEqual(
            apply_operational_size_floor(0.3, "CS.D.CFPGOLD.CFP.IP"),
            1.0,
        )

    def test_unknown_epic_unchanged(self) -> None:
        self.assertEqual(apply_operational_size_floor(1.5, "IX.D.NIKKEI.IFM.IP"), 1.5)


class GateExecutionParamsTests(unittest.TestCase):
    def test_normalizes_floats(self) -> None:
        out = normalize_gate_execution_params(
            {
                "actual_size": "0.25",
                "stop_points": 80,
                "limit_points": 240.0,
                "risk_gbp": "150.5",
            }
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out["actual_size"], 0.25)
        self.assertEqual(out["stop_points"], 80.0)
        self.assertEqual(out["limit_points"], 240.0)
        self.assertEqual(out["risk_gbp"], 150.5)
        self.assertTrue(out["gate_sourced"])

    def test_rejects_invalid_payload(self) -> None:
        self.assertIsNone(normalize_gate_execution_params(None))
        self.assertIsNone(
            normalize_gate_execution_params({"actual_size": 0, "stop_points": 10})
        )
        self.assertIsNone(
            normalize_gate_execution_params({"actual_size": "bad", "stop_points": 10})
        )


if __name__ == "__main__":
    unittest.main()
