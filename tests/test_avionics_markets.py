"""v29.1 Flight Deck avionics — localized asset keys and Decimal-safe quote parsing."""

from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cockpit.avionics_markets import (
    ASSET_EPIC_KEYS,
    ASSET_KEY_ALIASES,
    EPIC_ASSET_KEYS,
    enrich_avionics_markets,
)


def _gold_slice(*, rsi: object = 62.5, confidence: object = 71.0) -> dict:
    return {
        "epic": "CS.D.CFPGOLD.CFP.IP",
        "market": "Gold",
        "signal": {
            "direction": "WAIT",
            "confidence": confidence,
            "setup": "WAIT|mixed",
            "snapshot": {"rsi": rsi},
        },
        "health": {"gates": []},
    }


class AvionicsMarketsTests(unittest.TestCase):
    def setUp(self) -> None:
        import cockpit.web_server as ws
        import system.protective_learning as pl

        ws._flight_deck_boot_seeded = False
        pl._autonomous_engine_boot_armed = False

    def test_epic_asset_keys_canonical(self) -> None:
        self.assertEqual(
            set(EPIC_ASSET_KEYS.values()),
            {"GOLD", "WALL_STREET", "JAPAN_225", "EUR_USD"},
        )
        self.assertEqual(ASSET_EPIC_KEYS["GOLD"], "CS.D.CFPGOLD.CFP.IP")
        self.assertEqual(ASSET_EPIC_KEYS["WALL_STREET"], "IX.D.DOW.IFM.IP")
        self.assertEqual(ASSET_EPIC_KEYS["JAPAN_225"], "IX.D.NIKKEI.IFM.IP")
        self.assertEqual(ASSET_EPIC_KEYS["EUR_USD"], "CS.D.EURUSD.CFD.IP")

    def test_enrich_emits_localized_asset_slices(self) -> None:
        markets = {
            "CS.D.CFPGOLD.CFP.IP": _gold_slice(),
            "IX.D.DOW.IFM.IP": {
                "epic": "IX.D.DOW.IFM.IP",
                "signal": {"confidence": 68.0, "direction": "BUY"},
            },
            "IX.D.NIKKEI.IFM.IP": {
                "epic": "IX.D.NIKKEI.IFM.IP",
                "signal": {"confidence": 64.0, "direction": "WAIT"},
            },
            "CS.D.EURUSD.CFD.IP": {
                "epic": "CS.D.EURUSD.CFD.IP",
                "signal": {"confidence": 66.0, "direction": "SELL"},
            },
        }
        with patch(
            "cockpit.avionics_markets._snapshot_markets_by_epic",
            return_value=markets,
        ):
            out = enrich_avionics_markets({"markets": {}, "epics": {}})

        for key in ("GOLD", "WALL_STREET", "JAPAN_225", "EUR_USD"):
            self.assertIn(key, out.get("markets", {}))
            self.assertIn(key, out.get("hud_markets", {}))
            self.assertIn(key, out.get("avionics_assets", {}))
            asset = out["avionics_assets"][key]
            self.assertEqual(asset["asset_key"], key)
            self.assertEqual(asset["epic"], ASSET_EPIC_KEYS[key])

    def test_eurusd_alias_mirrors_canonical_row(self) -> None:
        markets = {
            "CS.D.EURUSD.CFD.IP": {
                "epic": "CS.D.EURUSD.CFD.IP",
                "bid": "1.08450",
                "offer": "1.08462",
                "signal": {"confidence": 70.0, "direction": "WAIT"},
            },
        }
        with patch(
            "cockpit.avionics_markets._snapshot_markets_by_epic",
            return_value=markets,
        ):
            out = enrich_avionics_markets({})

        self.assertIn("EUR_USD", out["hud_markets"])
        self.assertIn("EURUSD", out["hud_markets"])
        self.assertEqual(out["hud_markets"]["EURUSD"], out["hud_markets"]["EUR_USD"])
        self.assertEqual(ASSET_KEY_ALIASES["EURUSD"], "EUR_USD")

    def test_decimal_string_quotes_hydrate_hub_fallback(self) -> None:
        payload = {
            "epics": {
                "CS.D.CFPGOLD.CFP.IP": {
                    "bid": Decimal("2650.50"),
                    "offer": Decimal("2650.80"),
                    "spread": Decimal("0.30"),
                }
            }
        }
        with patch("cockpit.avionics_markets._snapshot_markets_by_epic", return_value={}):
            out = enrich_avionics_markets(payload)

        gold = out["markets"]["GOLD"]
        self.assertAlmostEqual(float(gold["bid"]), 2650.50, places=2)
        self.assertAlmostEqual(float(gold["offer"]), 2650.80, places=2)
        self.assertAlmostEqual(float(gold["spread"]), 0.30, places=2)

    def test_rsi_parsed_from_decimal_snapshot_string(self) -> None:
        markets = {
            "CS.D.CFPGOLD.CFP.IP": _gold_slice(rsi="72.50"),
        }
        with patch(
            "cockpit.avionics_markets._snapshot_markets_by_epic",
            return_value=markets,
        ):
            out = enrich_avionics_markets({})

        rsi = out["avionics_assets"]["GOLD"]["rsi"]
        self.assertIsNotNone(rsi)
        assert rsi is not None
        self.assertAlmostEqual(float(rsi), 72.50, places=2)

    def test_enrich_telemetry_ui_boot_controls_unlocked(self) -> None:
        from cockpit.web_server import _enrich_telemetry_for_ui

        out = _enrich_telemetry_for_ui({"ts": 1.0, "gates": {}, "epics": {}})
        controls = out.get("cockpit_controls") or {}
        self.assertFalse(controls.get("manual_stop"))
        self.assertFalse(controls.get("disabled"))
        self.assertTrue(controls.get("shadow_toggle_enabled"))
        shadow = out.get("shadow_trading") or {}
        self.assertEqual(str(shadow.get("mode", "")).upper(), "SHADOW")


if __name__ == "__main__":
    unittest.main()
