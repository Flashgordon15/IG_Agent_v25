"""
Phase 3 — v30 data partition isolation and Phase 2 risk shield verification.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class V30Phase3DataPartitionTests(unittest.TestCase):
    def tearDown(self) -> None:
        from system.node_profile import reset_node_profile_for_tests

        reset_node_profile_for_tests()
        for key in (
            "NODE_ENV",
            "IG_NODE_PROFILE",
            "IG_AGENT_DATA_DIR",
            "IG_AGENT_LEGACY_DATA",
            "IG_TRIAGE_DB",
        ):
            os.environ.pop(key, None)

    def test_v30_paths_isolated_from_legacy_src_data(self) -> None:
        from system.paths import (
            analytics_dir,
            apex_isolated_root,
            data_dir,
            is_legacy_data_path,
            legacy_data_roots,
            triage_db_path,
        )

        legacy = legacy_data_roots()[0]
        self.assertTrue(is_legacy_data_path(legacy / "runtime_state.json"))
        isolated_data = apex_isolated_root() / "data"
        self.assertEqual(data_dir(), isolated_data)
        self.assertEqual(analytics_dir(), apex_isolated_root() / "analytics")
        self.assertEqual(
            triage_db_path(),
            apex_isolated_root() / "analytics" / "triage_v30.db",
        )
        self.assertNotEqual(data_dir(), legacy)

    def test_shadow_node_profile_uses_isolated_triage_db(self) -> None:
        os.environ["NODE_ENV"] = "shadow"
        from system.node_profile import get_node_profile, reset_node_profile_for_tests
        from system.paths import apex_isolated_root

        reset_node_profile_for_tests()
        profile = get_node_profile(reload=True)
        expected = apex_isolated_root() / "analytics" / "triage_v30.db"
        self.assertEqual(profile.triage_db, expected)
        self.assertEqual(profile.version_label, "30.0.0")
        self.assertNotIn("src/data", str(profile.triage_db))
        self.assertNotIn("src/analytics", str(profile.triage_db))

    def test_config_active_path_is_v30_not_v25(self) -> None:
        from system.config_loader import V30_FILE, primary_config_path

        active = primary_config_path()
        self.assertEqual(active.name, V30_FILE)
        self.assertNotEqual(active.name, "config_v25.json")
        self.assertTrue(active.exists())

    def test_v30_config_load_version_and_no_v22_scaffold(self) -> None:
        from system.config_loader import V30_FILE, ConfigLoader, config_dir

        path = config_dir() / V30_FILE
        self.assertTrue(path.exists())
        cfg = ConfigLoader(path).load_config(validate=False)
        self.assertEqual(cfg.get("version"), "30.0.0")
        self.assertEqual(cfg.get("app_version"), "v30.0")
        self.assertTrue(cfg.get("epic") or cfg.get("markets") or cfg.get("instruments"))

    def test_app_identity_version_30(self) -> None:
        from system.app_identity import APP_VERSION, APP_VERSION_LABEL

        self.assertEqual(APP_VERSION, "30.0.0")
        self.assertEqual(APP_VERSION_LABEL, "v30.0")

    def test_risk_registers_locked_at_10k_350_750(self) -> None:
        from apex.hardening import (
            BASELINE_EQUITY_GBP,
            PER_ASSET_RISK_CAP_GBP,
            PORTFOLIO_RISK_CEILING_GBP,
        )
        from execution.atomic_gateway import (
            locked_per_asset_cap_gbp,
            locked_portfolio_ceiling_gbp,
            locked_session_equity_gbp,
        )
        from trading.points_engine import (
            global_portfolio_risk_ceiling_gbp,
            per_asset_risk_cap_gbp,
            runtime_session_equity_gbp,
        )

        self.assertEqual(BASELINE_EQUITY_GBP, 10_000.0)
        self.assertEqual(PER_ASSET_RISK_CAP_GBP, 350.0)
        self.assertEqual(PORTFOLIO_RISK_CEILING_GBP, 750.0)
        self.assertEqual(locked_session_equity_gbp(), 10_000.0)
        self.assertEqual(locked_per_asset_cap_gbp(), 350.0)
        self.assertEqual(locked_portfolio_ceiling_gbp(), 750.0)
        self.assertEqual(runtime_session_equity_gbp(), 10_000.0)
        self.assertEqual(per_asset_risk_cap_gbp(), 350.0)
        self.assertEqual(global_portfolio_risk_ceiling_gbp(), 750.0)

    def test_integer_lot_truncation(self) -> None:
        from apex.hardening import floor_contract_size, under_min_lot_detail
        from trading.trading_loop import promote_high_confidence_signal
        from signals.signal_engine import SignalResult

        size_int, under = floor_contract_size(2.99)
        self.assertEqual(size_int, 2)
        self.assertFalse(under)
        zero_int, under_zero = floor_contract_size(0.4)
        self.assertEqual(zero_int, 0)
        self.assertTrue(under_zero)
        self.assertIn("HOLD: UNDER_MIN_LOT", under_min_lot_detail(0))

        sig = SignalResult(
            signal="WAIT",
            raw_confidence=88.0,
            adjusted_confidence=88.0,
            learning_delta=0.0,
            setup_key="BUY|lot",
            notes="",
            snapshot={"raw_signal": "BUY", "buy_score": 88.0},
        )
        promoted = promote_high_confidence_signal(sig, 45.0, raw_size=0.5)
        self.assertTrue(promoted.snapshot.get("under_min_lot"))


if __name__ == "__main__":
    unittest.main()
