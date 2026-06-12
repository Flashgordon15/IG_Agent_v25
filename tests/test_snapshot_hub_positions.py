"""Hub quote push should refresh open-position marks between loop snapshots."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api.snapshot_store import (
    force_position_view_refresh,
    get_tick,
    publish_tick,
    push_hub_quote_to_dashboard,
    reset_snapshot_store_for_tests,
    set_snapshot_path_for_tests,
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

    def test_force_refresh_bypasses_hub_push_throttle(self) -> None:
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

        self.assertTrue(
            force_position_view_refresh("CS.D.CFPGOLD.CFP.IP", 4450.0, 4450.5)
        )
        pos_first = (get_tick().get("positions") or [{}])[0]
        self.assertEqual(pos_first["current"], 4450.5)
        self.assertEqual(pos_first["pnl_pts"], 12.8)

        self.assertTrue(
            force_position_view_refresh("CS.D.CFPGOLD.CFP.IP", 4448.0, 4448.5)
        )
        pos_second = (get_tick().get("positions") or [{}])[0]
        self.assertEqual(pos_second["current"], 4448.5)
        self.assertEqual(pos_second["pnl_pts"], 14.8)

    def test_hub_refresh_updates_display_daily_pnl(self) -> None:
        publish_tick(
            {
                "type": "tick",
                "epic": "CS.D.CFPGOLD.CFP.IP",
                "selected_epic": "CS.D.CFPGOLD.CFP.IP",
                "realized_daily_pnl_gbp": 12.0,
                "daily_pnl_gbp": 12.0,
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

        force_position_view_refresh("CS.D.CFPGOLD.CFP.IP", 4450.0, 4450.5)
        tick = get_tick()
        self.assertGreater(float(tick.get("open_unrealized_gbp") or 0), 0)
        self.assertGreater(float(tick.get("daily_pnl_gbp") or 0), 12.0)


if __name__ == "__main__":
    unittest.main()
