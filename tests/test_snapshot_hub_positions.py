"""Hub quote push should refresh open-position marks between loop snapshots."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api.snapshot_store import (
    publish_tick,
    push_hub_quote_to_dashboard,
    reset_snapshot_store_for_tests,
    set_snapshot_path_for_tests,
    get_tick,
)


class SnapshotHubPositionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        set_snapshot_path_for_tests(Path(self.tmp.name) / "dashboard_snapshot.json")
        reset_snapshot_store_for_tests()
        set_snapshot_path_for_tests(Path(self.tmp.name) / "dashboard_snapshot.json")

    def tearDown(self) -> None:
        reset_snapshot_store_for_tests()
        self.tmp.cleanup()

    def test_hub_push_refreshes_position_mark_and_pnl_pts(self) -> None:
        publish_tick(
            {
                "type": "tick",
                "epic": "CS.D.CFPGOLD.CFP.IP",
                "selected_epic": "CS.D.CFPGOLD.CFP.IP",
                "bid": 4454.79,
                "offer": 4455.29,
                "positions": [
                    {
                        "deal_id": "DIAAAAXNM2VYUAN",
                        "epic": "CS.D.CFPGOLD.CFP.IP",
                        "side": "SELL",
                        "entry": 4463.25,
                        "current": 4460.59,
                        "pnl_pts": 2.7,
                        "pnl_gbp": 0.0,
                        "size": 10.0,
                    }
                ],
            }
        )

        push_hub_quote_to_dashboard(
            "CS.D.CFPGOLD.CFP.IP",
            4454.79,
            4455.29,
            tick_age_s=0.0,
        )

        pos = (get_tick().get("positions") or [{}])[0]
        self.assertEqual(pos["current"], 4455.29)
        self.assertEqual(pos["pnl_pts"], 8.0)
        # pnl_gbp=0.0 from IG DEMO → now calculated from quote (not kept at 0)
        self.assertNotEqual(pos["pnl_gbp"], 0.0)


if __name__ == "__main__":
    unittest.main()
