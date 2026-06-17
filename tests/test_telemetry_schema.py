"""Tests — cockpit IG telemetry schema contract."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cockpit.telemetry_schema import (
    TelemetrySchemaMismatchError,
    validate_position_map,
    validate_position_payload,
)


class TelemetrySchemaTests(unittest.TestCase):
    def test_valid_eurusd_five_decimal_shape(self) -> None:
        raw = {
            "dealId": "DIAAAAXNM2VYUAN",
            "epic": "CS.D.EURUSD.CFD.IP",
            "side": "SELL",
            "level": 1.16125,
            "current": 1.16130,
            "profitAndLoss": -84.0,
            "currency": "USD",
            "size": 1.0,
        }
        out = validate_position_payload(raw)
        self.assertEqual(out["dealId"], "DIAAAAXNM2VYUAN")
        self.assertEqual(out["entry"], 1.16125)
        self.assertEqual(out["profitAndLoss"], -84.0)

    def test_placeholder_deal_id_rejected(self) -> None:
        with self.assertRaises(TelemetrySchemaMismatchError):
            validate_position_payload(
                {
                    "dealId": "mock",
                    "level": 1.16125,
                    "size": 1.0,
                }
            )

    def test_position_map_keys_are_deal_ids(self) -> None:
        rows = [
            {
                "dealId": "A",
                "epic": "CS.D.EURUSD.CFD.IP",
                "level": 1.08543,
                "profitAndLoss": -79.88,
                "currency": "USD",
                "size": 2.0,
            }
        ]
        pmap = validate_position_map(rows)
        self.assertIn("A", pmap)
        self.assertEqual(pmap["A"]["pnl_source"], "ig_broker")


if __name__ == "__main__":
    unittest.main()
