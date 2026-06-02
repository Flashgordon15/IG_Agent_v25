"""Adaptive IG position sync poll interval."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from runtime.ig_position_sync import IgPositionSync, SyncedPosition


class PositionSyncIntervalTests(unittest.TestCase):
    def _sync(self, *, open_n: int = 0) -> IgPositionSync:
        sync = IgPositionSync(MagicMock(), MagicMock(), interval_seconds=25.0)
        sync._snapshot.total_open = open_n
        if open_n:
            sync._snapshot.positions = [
                SyncedPosition(
                    deal_id="D1",
                    epic="IX.D.NIKKEI.IFM.IP",
                    direction="BUY",
                    size=1.0,
                    level=100.0,
                    upl=0.0,
                    stop_level=1.0,
                    limit_level=2.0,
                )
            ]
        return sync

    def test_flat_uses_relaxed_interval(self) -> None:
        sync = self._sync(open_n=0)
        self.assertEqual(sync._effective_interval(), 30.0)

    def test_open_high_signal_uses_fast_interval(self) -> None:
        sync = self._sync(open_n=1)
        with (
            patch.object(sync, "_snapshot_confidence_pct", return_value=85.0),
            patch.object(sync, "_needs_fast_position_sync", return_value=False),
        ):
            self.assertEqual(sync._effective_interval(), 15.0)

    def test_open_low_signal_uses_relaxed_interval(self) -> None:
        sync = self._sync(open_n=1)
        with (
            patch.object(sync, "_snapshot_confidence_pct", return_value=55.0),
            patch.object(sync, "_needs_fast_position_sync", return_value=False),
        ):
            self.assertEqual(sync._effective_interval(), 30.0)

    def test_missing_protection_forces_fast_interval(self) -> None:
        sync = self._sync(open_n=1)
        sync._snapshot.positions[0].stop_level = 0.0
        with patch.object(sync, "_snapshot_confidence_pct", return_value=40.0):
            self.assertEqual(sync._effective_interval(), 15.0)


if __name__ == "__main__":
    unittest.main()
