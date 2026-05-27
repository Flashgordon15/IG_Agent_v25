"""Japan 225 hub maintenance mode — blank ticks must not trigger REST."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.market_data_hub import MarketDataHub


class MarketDataHubMaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = MarketDataHub()
        self.epic = "IX.D.NIKKEI.IFM.IP"
        self.rest = MagicMock()
        self.rest.fetch_live_prices = MagicMock(return_value=(65000.0, 65030.0))
        self.hub.attach_rest(self.rest)

    def test_blank_tick_enters_maintenance_once(self) -> None:
        with patch("system.market_data_hub.log_engine") as log_mock:
            self.hub.enter_maintenance(self.epic)
            self.hub.enter_maintenance(self.epic)
            self.assertTrue(self.hub.is_in_maintenance(self.epic))
            maint_logs = [
                c
                for c in log_mock.call_args_list
                if "maintenance window" in str(c.args[0]).lower()
            ]
            self.assertEqual(len(maint_logs), 1)

    def test_fetch_if_stale_skips_rest_during_maintenance(self) -> None:
        self.hub.publish(self.epic, 65000.0, 65030.0, source="lightstreamer")
        self.hub.enter_maintenance(self.epic)
        snap = self.hub.fetch_if_stale(self.epic, min_interval=0.0)
        self.rest.fetch_live_prices.assert_not_called()
        self.assertIsNotNone(snap)
        self.assertEqual(self.hub.metrics()["total_fetches"], 0)

    def test_publish_valid_prices_exits_maintenance(self) -> None:
        self.hub.enter_maintenance(self.epic)
        self.assertTrue(self.hub.is_in_maintenance(self.epic))
        self.hub.publish(self.epic, 65100.0, 65130.0, source="lightstreamer")
        self.assertFalse(self.hub.is_in_maintenance(self.epic))


if __name__ == "__main__":
    unittest.main()
