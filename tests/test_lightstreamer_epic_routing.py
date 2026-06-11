"""Lightstreamer multi-epic subscription routing."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ig_api.lightstreamer_streaming import IGLightstreamerStreamingClient


class LightstreamerEpicRoutingTests(unittest.TestCase):
    def test_epic_from_item_name(self) -> None:
        self.assertEqual(
            IGLightstreamerStreamingClient._epic_from_item_name(
                "MARKET:CS.D.EURUSD.CFD.IP", "fallback"
            ),
            "CS.D.EURUSD.CFD.IP",
        )
        self.assertEqual(
            IGLightstreamerStreamingClient._epic_from_item_name("", "IX.D.NIKKEI.IFM.IP"),
            "IX.D.NIKKEI.IFM.IP",
        )


if __name__ == "__main__":
    unittest.main()
