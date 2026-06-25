"""Active lifecycle trade registry tests."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.learning_store import LearningStore
from runtime.active_lifecycle_trades import (
    STATE_ADOPTED,
    STATE_AGENT_MANAGED,
    STATE_CLOSED,
    adopt_broker_position,
    ensure_lifecycle_table,
    list_active_lifecycle_trades,
    reconcile_active_lifecycle_trades,
)


class ActiveLifecycleTradesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = LearningStore(str(Path(self._tmp.name) / "learning.db"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_adopt_imports_unknown_broker_deal(self) -> None:
        result = adopt_broker_position(
            self.store,
            deal_id="DEAL123",
            epic="CS.D.EURUSD.CFD.IP",
            direction="BUY",
            level=1.16,
            size=1.0,
            stop_level=1.15,
            limit_level=1.17,
            source="test",
        )
        self.assertTrue(result.adopted)
        self.assertEqual(result.state, STATE_ADOPTED)
        row = self.store.find_open_by_deal_id("DEAL123")
        self.assertIsNotNone(row)
        active = list_active_lifecycle_trades(self.store)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["deal_id"], "DEAL123")

    def test_reconcile_syncs_existing_and_closes_gone(self) -> None:
        adopt_broker_position(
            self.store,
            deal_id="KEEP",
            epic="IX.D.DOW.IFM.IP",
            direction="SELL",
            level=42000.0,
            size=0.5,
            source="test",
        )
        adopt_broker_position(
            self.store,
            deal_id="GONE",
            epic="IX.D.DOW.IFM.IP",
            direction="BUY",
            level=42100.0,
            size=0.5,
            source="test",
        )

        broker = [
            MagicMock(
                deal_id="KEEP",
                epic="IX.D.DOW.IFM.IP",
                direction="SELL",
                size=0.5,
                level=42010.0,
                stop_level=42100.0,
                limit_level=41900.0,
                upl=-5.0,
                market_name="Wall Street",
                deal_reference="",
            )
        ]
        counts = reconcile_active_lifecycle_trades(self.store, broker, source="test")
        self.assertEqual(counts["synced"], 1)
        self.assertEqual(counts["closed_registry"], 1)

        active = list_active_lifecycle_trades(self.store)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["deal_id"], "KEEP")
        self.assertEqual(active[0]["lifecycle_state"], STATE_AGENT_MANAGED)

        conn = self.store.conn
        ensure_lifecycle_table(conn)
        gone = conn.execute(
            "SELECT lifecycle_state FROM active_lifecycle_trades WHERE deal_id='GONE'"
        ).fetchone()
        self.assertEqual(gone[0], STATE_CLOSED)


if __name__ == "__main__":
    unittest.main()
