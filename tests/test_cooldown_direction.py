"""Cooldown keys are per epic:direction — BUY must not block SELL."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from execution.cooldown_tracker import CooldownTracker, cooldown_key


class CooldownDirectionTests(unittest.TestCase):
    def test_keys_are_direction_specific(self) -> None:
        self.assertEqual(cooldown_key("EPIC", "BUY"), "EPIC:BUY")
        self.assertEqual(cooldown_key("EPIC", "SELL"), "EPIC:SELL")
        self.assertNotEqual(cooldown_key("EPIC", "BUY"), cooldown_key("EPIC", "SELL"))

    def test_buy_cooldown_does_not_block_sell(self) -> None:
        cd = CooldownTracker(180)
        cd.record("IX.D.NIKKEI.IFM.IP", when=datetime.now(), direction="BUY")
        self.assertTrue(cd.is_active("IX.D.NIKKEI.IFM.IP", "BUY"))
        self.assertFalse(cd.is_active("IX.D.NIKKEI.IFM.IP", "SELL"))

    def test_sell_cooldown_does_not_block_buy(self) -> None:
        cd = CooldownTracker(180)
        cd.record("IX.D.NIKKEI.IFM.IP", when=datetime.now(), direction="SELL")
        self.assertTrue(cd.is_active("IX.D.NIKKEI.IFM.IP", "SELL"))
        self.assertFalse(cd.is_active("IX.D.NIKKEI.IFM.IP", "BUY"))

    def test_expired_cooldown_allows_both(self) -> None:
        cd = CooldownTracker(60)
        past = datetime.now() - timedelta(seconds=120)
        cd.record("EPIC", when=past, direction="BUY")
        self.assertFalse(cd.is_active("EPIC", "BUY"))
        self.assertFalse(cd.is_active("EPIC", "SELL"))


if __name__ == "__main__":
    unittest.main()
