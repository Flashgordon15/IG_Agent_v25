from __future__ import annotations

import unittest

from execution.market_suspension import (
    clear_for_tests,
    gate_detail,
    is_blocked,
    note_ig_order_error,
)


class MarketSuspensionTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_for_tests()

    def tearDown(self) -> None:
        clear_for_tests()

    def test_suspension_blocks_five_minutes(self) -> None:
        exc = Exception("error.trading.market-closed on epic")
        self.assertTrue(note_ig_order_error(exc))
        self.assertTrue(is_blocked())
        self.assertIn("market-closed", gate_detail())


if __name__ == "__main__":
    unittest.main()
