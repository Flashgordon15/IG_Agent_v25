"""V2 Cache Reaper — stale inflight / pending eviction."""

from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock

from execution.entry_inflight import (
    clear_entry,
    reset_entry_inflight_state_for_tests,
    try_begin_entry,
)
from execution.pending_order_reconcile import (
    mark_pending,
    ORDER_TYPE_ENTRY,
    reset_pending_state_for_tests,
    has_pending,
)
from trading.cache_reaper import (
    REAPER_TIMEOUT_SEC,
    RING_GOVERNOR_MAX_SLOTS,
    V2CacheReaper,
    govern_live_tick_ingest,
    reset_tick_governor_for_tests,
    reset_v2_cache_reaper_for_tests,
    tick_governor_slot_count,
)


class V2CacheReaperTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_pending_state_for_tests()
        reset_entry_inflight_state_for_tests()
        reset_v2_cache_reaper_for_tests()

    def tearDown(self) -> None:
        reset_pending_state_for_tests()
        reset_entry_inflight_state_for_tests()
        reset_v2_cache_reaper_for_tests()

    def test_evicts_stale_pending_without_deal_id(self) -> None:
        epic = "CS.D.CFPGOLD.CFP.IP"
        mark_pending(epic, side="BUY", order_type=ORDER_TYPE_ENTRY, deal_reference="")
        from execution import pending_order_reconcile as mod

        with mod._lock:
            rec = mod._pending[epic]
            mod._pending[epic] = type(rec)(
                epic=rec.epic,
                side=rec.side,
                order_type=rec.order_type,
                local_created_at=time.time() - REAPER_TIMEOUT_SEC - 5,
                broker_deal_reference="",
                pending_reconcile=False,
            )

        client = MagicMock()
        client.has_open_position.return_value = False
        client.open_positions.return_value = []

        reaper = V2CacheReaper(client, timeout_sec=REAPER_TIMEOUT_SEC)
        cleared = reaper.tick_once()
        self.assertEqual(cleared, 1)
        self.assertFalse(has_pending(epic))

    def test_skips_when_broker_shows_position(self) -> None:
        epic = "IX.D.FTSE.IFM.IP"
        mark_pending(epic, side="BUY", order_type=ORDER_TYPE_ENTRY)
        from execution import pending_order_reconcile as mod

        with mod._lock:
            rec = mod._pending[epic]
            mod._pending[epic] = type(rec)(
                epic=rec.epic,
                side=rec.side,
                order_type=rec.order_type,
                local_created_at=time.time() - REAPER_TIMEOUT_SEC - 5,
                broker_deal_reference="",
                pending_reconcile=True,
            )

        client = MagicMock()
        client.has_open_position.return_value = True

        reaper = V2CacheReaper(client, timeout_sec=REAPER_TIMEOUT_SEC)
        cleared = reaper.tick_once()
        self.assertEqual(cleared, 0)
        self.assertTrue(has_pending(epic))

    def test_evicts_stale_entry_inflight(self) -> None:
        epic = "CS.D.CRUDE.CFD.IP"
        try_begin_entry(epic, "BUY", 1.0)
        from execution import entry_inflight as mod

        with mod._lock:
            entry = mod._entries[epic]
            mod._entries[epic] = type(entry)(
                epic=entry.epic,
                direction=entry.direction,
                size=entry.size,
                local_created_at=time.time() - REAPER_TIMEOUT_SEC - 5,
                broker_deal_reference="",
            )

        client = MagicMock()
        client.has_open_position.return_value = False
        client.open_positions.return_value = []

        reaper = V2CacheReaper(client, timeout_sec=REAPER_TIMEOUT_SEC)
        cleared = reaper.tick_once()
        self.assertEqual(cleared, 1)
        clear_entry(epic)


class TickRingGovernorTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_tick_governor_for_tests()

    def tearDown(self) -> None:
        reset_tick_governor_for_tests()

    def test_fifo_cap_at_fifty_thousand(self) -> None:
        epic = "CS.D.CFPGOLD.CFP.IP"
        for i in range(RING_GOVERNOR_MAX_SLOTS + 25):
            govern_live_tick_ingest(
                epic,
                bid=2650.0 + i * 0.01,
                offer=2650.5 + i * 0.01,
                mid=2650.25 + i * 0.01,
            )
        self.assertEqual(tick_governor_slot_count(), RING_GOVERNOR_MAX_SLOTS)


if __name__ == "__main__":
    unittest.main()
