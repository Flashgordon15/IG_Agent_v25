"""IgPositionSync — managed epic filtering."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from runtime.ig_position_sync import IgPositionSync, SyncedPosition


class ManagedEpicFilterTests(unittest.TestCase):
    def test_positions_for_sync_keeps_only_managed_epics(self) -> None:
        sync = IgPositionSync(
            MagicMock(),
            MagicMock(),
            managed_epics=frozenset({"IX.D.NIKKEI.IFM.IP", "CS.D.EURUSD.CFD.IP"}),
        )
        positions = [
            SyncedPosition(
                deal_id="D1",
                epic="IX.D.NIKKEI.IFM.IP",
                direction="BUY",
                size=1.0,
                level=100.0,
                upl=0.0,
            ),
            SyncedPosition(
                deal_id="D2",
                epic="KC.D.CKILN.CASH.IP",
                direction="BUY",
                size=30.0,
                level=586.0,
                upl=-1.0,
            ),
        ]
        kept = sync._positions_for_sync(positions)
        self.assertEqual([p.deal_id for p in kept], ["D1"])


if __name__ == "__main__":
    unittest.main()
