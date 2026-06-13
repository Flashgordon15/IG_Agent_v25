"""Accounting alignment tests — FX, funding, dealing rules, instrument class."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from execution.dealing_constraints import clamp_stop_to_broker_minimum
from system.fx_conversion import (
    IG_COMMERCIAL_FX_MARKUP,
    apply_ig_fx_markup,
    convert_to_account_gbp,
)
from system.instrument_class import filter_rotation_epics, is_weekend_epic
from system.overnight_funding import rollover_count_since
from system.pnl_accounting import normalize_shadow_net_pnl


class FxConversionTests(unittest.TestCase):
    def test_usd_to_gbp_applies_commercial_markup(self) -> None:
        gbp = convert_to_account_gbp(100.0, "USD")
        self.assertLess(gbp, 100.0 * 0.79)
        self.assertAlmostEqual(
            apply_ig_fx_markup(100.0 * 0.78), 100.0 * 0.78 * (1 - IG_COMMERCIAL_FX_MARKUP)
        )

    def test_gbp_passthrough(self) -> None:
        self.assertEqual(convert_to_account_gbp(42.0, "GBP"), 42.0)


class DealingConstraintsTests(unittest.TestCase):
    def test_buy_stop_too_close_rejected(self) -> None:
        out = clamp_stop_to_broker_minimum(
            "BUY", px=100.0, stop=99.5, min_distance_points=10.0, epic="IX.D.DOW.IFM.IP"
        )
        self.assertIsNone(out)

    def test_buy_stop_far_enough_accepted(self) -> None:
        out = clamp_stop_to_broker_minimum(
            "BUY", px=100.0, stop=80.0, min_distance_points=10.0, epic="IX.D.DOW.IFM.IP"
        )
        self.assertAlmostEqual(out, 80.0)


class InstrumentClassTests(unittest.TestCase):
    def test_weekend_epic_detected(self) -> None:
        self.assertTrue(is_weekend_epic("IX.D.DOW.WEEKEND.IP", "Weekend Wall St"))

    def test_weekday_epics_excluded_on_saturday(self) -> None:
        sat = datetime(2026, 6, 13, 12, 0, tzinfo=ZoneInfo("Europe/London"))
        out = filter_rotation_epics(
            ["IX.D.DOW.IFM.IP", "IX.D.DOW.WEEKEND.IP"],
            now=sat,
        )
        self.assertEqual(out, ["IX.D.DOW.WEEKEND.IP"])


class OvernightFundingTests(unittest.TestCase):
    def test_rollover_count(self) -> None:
        opened = datetime(2026, 6, 11, 10, 0, tzinfo=ZoneInfo("Europe/London"))
        now = datetime(2026, 6, 13, 12, 0, tzinfo=ZoneInfo("Europe/London"))
        self.assertGreaterEqual(rollover_count_since(opened, now=now), 2)


class ShadowNetPnlTests(unittest.TestCase):
    def test_ig_currency_authoritative(self) -> None:
        row = normalize_shadow_net_pnl(
            {"ig_pnl_currency": 10.0, "pnl_points": 999.0, "currency": "GBP"}
        )
        self.assertTrue(row["pnl_is_net"])
        self.assertEqual(row["ig_pnl_currency"], 10.0)


if __name__ == "__main__":
    unittest.main()
