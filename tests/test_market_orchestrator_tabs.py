"""Dashboard market tabs — all enabled instruments always present."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from runtime.market_orchestrator import MarketOrchestrator
from system.config_loader import ConfigLoader


class MarketOrchestratorTabTests(unittest.TestCase):
    @patch("runtime.market_orchestrator.publish_tick")
    def test_publish_includes_all_enabled_epics(self, publish_mock: MagicMock) -> None:
        cfg = ConfigLoader(ROOT / "config" / "config_v25.json").load_config()
        loop_j = MagicMock()
        loop_j._epic = "IX.D.NIKKEI.IFM.IP"
        loop_e = MagicMock()
        loop_e._epic = "CS.D.EURUSD.CFD.IP"
        loop_g = MagicMock()
        loop_g._epic = "CS.D.CFPGOLD.CFP.IP"

        enabled = [
            "IX.D.NIKKEI.IFM.IP",
            "CS.D.EURUSD.CFD.IP",
            "CS.D.CFPGOLD.CFP.IP",
        ]
        meta = {
            "IX.D.NIKKEI.IFM.IP": {"name": "Japan 225", "instrument_id": "japan_225"},
            "CS.D.EURUSD.CFD.IP": {"name": "EUR/USD", "instrument_id": "eur_usd"},
            "CS.D.CFPGOLD.CFP.IP": {"name": "Spot Gold", "instrument_id": "gold"},
        }
        orch = MarketOrchestrator(
            cfg,
            [loop_j, loop_e, loop_g],
            primary_epic=enabled[0],
            enabled_epics=enabled,
            instrument_meta=meta,
        )

        orch.on_market_snapshot(
            {
                "epic": "IX.D.NIKKEI.IFM.IP",
                "market": "Japan 225",
                "bid": 1.0,
                "offer": 2.0,
            }
        )
        orch.on_market_snapshot(
            {
                "epic": "CS.D.EURUSD.CFD.IP",
                "market": "EUR/USD",
                "bid": 1.1,
                "offer": 1.2,
            }
        )

        self.assertTrue(publish_mock.called)
        merged = publish_mock.call_args[0][0]
        self.assertEqual(merged["enabled_epics"], enabled)
        self.assertIn("CS.D.CFPGOLD.CFP.IP", merged["markets"])
        self.assertEqual(
            merged["markets"]["CS.D.CFPGOLD.CFP.IP"]["market"],
            "Spot Gold",
        )
        self.assertEqual(
            merged["instrument_labels"]["CS.D.CFPGOLD.CFP.IP"],
            "Spot Gold",
        )


if __name__ == "__main__":
    unittest.main()
