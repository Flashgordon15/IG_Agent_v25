"""Economic calendar blackout tests."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from risk.economic_calendar import EconomicCalendar, reset_economic_calendar_for_tests
from system.config import Config

_LONDON = ZoneInfo("Europe/London")
GOLD = "CS.D.CFPGOLD.CFP.IP"
DOW = "IX.D.DOW.IFM.IP"

_ALL_EVENT = {
    "date": "2026-06-18",
    "time_bst": "13:30",
    "description": "US CPI",
    "impact": "HIGH",
    "instruments": ["all"],
}


def _cfg(*, enabled: bool = True, events: list | None = None) -> Config:
    return Config(
        _data={
            "economic_calendar": {
                "enabled": enabled,
                "pre_event_blackout_minutes": 15,
                "post_event_blackout_minutes": 30,
                "events": events if events is not None else [_ALL_EVENT],
            }
        }
    )


class EconomicCalendarTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_economic_calendar_for_tests()

    def test_calendar_blocks_pre_event(self) -> None:
        cal = EconomicCalendar(_cfg())
        at = datetime(2026, 6, 18, 13, 20, tzinfo=_LONDON)
        blocked, reason = cal.check_block(GOLD, now=at)
        self.assertTrue(blocked)
        self.assertIn("CPI", reason)

    def test_calendar_blocks_post_event(self) -> None:
        cal = EconomicCalendar(_cfg())
        at = datetime(2026, 6, 18, 13, 45, tzinfo=_LONDON)
        blocked, reason = cal.check_block(GOLD, now=at)
        self.assertTrue(blocked)
        self.assertIn("post", reason)

    def test_calendar_passes_outside_window(self) -> None:
        cal = EconomicCalendar(_cfg())
        at = datetime(2026, 6, 18, 10, 0, tzinfo=_LONDON)
        blocked, _ = cal.check_block(GOLD, now=at)
        self.assertFalse(blocked)

    def test_calendar_instruments_all_blocks_everything(self) -> None:
        cal = EconomicCalendar(_cfg())
        at = datetime(2026, 6, 18, 13, 25, tzinfo=_LONDON)
        blocked, _ = cal.check_block(DOW, now=at)
        self.assertTrue(blocked)

    def test_calendar_specific_instrument_only_blocks_that_epic(self) -> None:
        cal = EconomicCalendar(
            _cfg(
                events=[
                    {
                        "date": "2026-06-20",
                        "time_bst": "12:00",
                        "description": "Gold only",
                        "impact": "HIGH",
                        "instruments": [GOLD],
                    }
                ]
            )
        )
        at = datetime(2026, 6, 20, 12, 0, tzinfo=_LONDON)
        self.assertTrue(cal.check_block(GOLD, now=at)[0])
        self.assertFalse(cal.check_block(DOW, now=at)[0])

    def test_calendar_disabled_flag_bypasses_all_checks(self) -> None:
        cal = EconomicCalendar(_cfg(enabled=False, events=[]))
        at = datetime(2026, 6, 18, 13, 29, tzinfo=_LONDON)
        blocked, _ = cal.check_block(GOLD, now=at)
        self.assertFalse(blocked)


if __name__ == "__main__":
    unittest.main()
