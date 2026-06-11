"""IG position sync wiring in agent bootstrap."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class TestAgentBootstrapPositionSync(unittest.TestCase):
    @patch("runtime.ig_position_sync.IgPositionSync")
    def test_start_ig_position_sync_attaches_and_starts(self, mock_sync_cls) -> None:
        from runtime.agent_bootstrap import start_ig_position_sync

        rest = MagicMock()
        store = MagicMock()
        tracker = MagicMock()
        inst = mock_sync_cls.return_value

        out = start_ig_position_sync(
            rest, store, tracker, epic="IX.D.NIKKEI.IFM.IP", interval_seconds=25.0
        )

        self.assertIs(out, inst)
        mock_sync_cls.assert_called_once()
        tracker.attach_sync.assert_called_once_with(inst)
        inst.start.assert_called_once()

if __name__ == "__main__":
    unittest.main()
