"""Premium overnight session rules — rollover lock + momentum gate relief."""

from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from system.config import Config
from trading.entry_protection import check_session_blackout

GOLD = "CS.D.CFPGOLD.CFP.IP"
DOW = "IX.D.DOW.IFM.IP"
_LONDON = ZoneInfo("Europe/London")


def _cfg(**overrides) -> Config:
    data = {
        "entry_protection": {
            "enabled": True,
            "session_blackout_enabled": True,
            "gold_epic": GOLD,
            "gold_weekday_blackout_start": "20:00",
            "gold_weekday_blackout_end": "06:00",
            "gold_weekend_blackout_start": "Fri 20:00",
            "gold_weekend_blackout_end": "Mon 06:00",
            "premium_overnight": {
                "enabled": True,
                "epics": [GOLD, DOW, "CS.D.EURUSD.CFD.IP", "IX.D.NIKKEI.IFM.IP"],
                "rollover_lock_start": "21:58",
                "rollover_lock_end": "22:05",
                "overnight_session_start": "22:00",
                "overnight_session_end": "06:00",
                "momentum_confidence_floor": 0.72,
            },
        },
    }
    data["entry_protection"].update(overrides.get("entry_protection") or {})
    return Config(_data=data)


def _london(y, m, d, h, mi) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=_LONDON)


class PremiumOvernightTests(unittest.TestCase):
    def test_weekday_2205_allowed_for_gold(self) -> None:
        cfg = _cfg()
        blocked, reason = check_session_blackout(GOLD, cfg, now=_london(2026, 6, 16, 22, 5))
        self.assertFalse(blocked, reason)

    def test_rollover_lock_2200_blocks(self) -> None:
        cfg = _cfg()
        blocked, reason = check_session_blackout(GOLD, cfg, now=_london(2026, 6, 16, 22, 0))
        self.assertTrue(blocked)
        self.assertIn("rollover lock", reason.lower())

    def test_legacy_blackout_when_lockdown_overridden_off(self) -> None:
        cfg = Config(
            _data={
                "entry_protection": {
                    "enabled": True,
                    "session_blackout_enabled": True,
                    "gold_epic": GOLD,
                    "gold_weekday_blackout_start": "20:00",
                    "gold_weekday_blackout_end": "06:00",
                    "gold_weekend_blackout_start": "Fri 20:00",
                    "gold_weekend_blackout_end": "Mon 06:00",
                    "premium_overnight": {
                        "enabled": False,
                        "lockdown_override_disable": True,
                    },
                }
            }
        )
        blocked, reason = check_session_blackout(GOLD, cfg, now=_london(2026, 6, 16, 22, 5))
        self.assertTrue(blocked)
        self.assertIn("blackout", reason.lower())

    def test_overnight_momentum_pass(self) -> None:
        from intelligence.premium_overnight import premium_overnight_momentum_pass

        cfg = _cfg()
        self.assertTrue(
            premium_overnight_momentum_pass(
                GOLD,
                "MOMENTUM_UP",
                0.80,
                config=cfg,
                now=_london(2026, 6, 16, 23, 0),
            )
        )
        self.assertFalse(
            premium_overnight_momentum_pass(
                GOLD,
                "NEUTRAL",
                0.80,
                config=cfg,
                now=_london(2026, 6, 16, 23, 0),
            )
        )

    def test_momentum_pass_during_day_session(self) -> None:
        from intelligence.premium_overnight import premium_overnight_momentum_pass

        cfg = _cfg()
        self.assertTrue(
            premium_overnight_momentum_pass(
                GOLD,
                "SWEEP_BUY",
                0.85,
                config=cfg,
                now=_london(2026, 6, 16, 14, 30),
            )
        )

    def test_lockdown_enabled_without_config_block(self) -> None:
        from intelligence.premium_overnight import premium_overnight_enabled

        self.assertTrue(premium_overnight_enabled(None))


if __name__ == "__main__":
    unittest.main()
