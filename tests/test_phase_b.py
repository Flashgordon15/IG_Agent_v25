"""Phase B — portfolio rehydration, correlation £ heat, shadow catch-up, ml_veto promote."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "v26"))

from execution.correlation_guard import (
    check_and_record,
    confirm_direction_risk,
    rehydrate_direction_risk,
    release_direction_risk,
    reset_correlation_guard_for_tests,
    snapshot,
)
from execution.portfolio_hooks import (
    rehydrate_portfolio_from_store,
    reset_portfolio_hooks_for_tests,
    risk_gbp_from_trade_row,
)
from system.portfolio_envelope import reset_portfolio_envelope_for_tests
from system.portfolio_envelope import snapshot as env_snap
from system.v26_shadow_offsets import (
    load_offset,
    reset_shadow_offsets_for_tests,
    save_offset,
)


class PortfolioRehydrationTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_portfolio_hooks_for_tests()

    def tearDown(self) -> None:
        reset_portfolio_hooks_for_tests()

    def test_risk_gbp_from_trade_row(self) -> None:
        row = {
            "entry": 100.0,
            "stop": 90.0,
            "size": 2.0,
            "epic": "CS.D.EURUSD.CFD.IP",
        }
        cfg = MagicMock()
        cfg.get = lambda k, d=None: (
            {
                "instruments": {
                    "eurusd": {
                        "epic": "CS.D.EURUSD.CFD.IP",
                        "ig_point_value_gbp": 1.0,
                    }
                }
            }
            if k == "instruments"
            else d
        )
        self.assertEqual(risk_gbp_from_trade_row(row, cfg=cfg), 20.0)

    def test_rehydrate_from_open_trades(self) -> None:
        store = MagicMock()
        store.active_trades.return_value = [
            {
                "dry_run": 0,
                "entry": 100.0,
                "stop": 90.0,
                "size": 1.0,
                "epic": "IX.D.DOW.IFM.IP",
                "ig_deal_id": "D1",
                "deal_reference": "",
                "id": 1,
            }
        ]
        store.conn.execute.return_value.fetchall.return_value = []
        store.sum_daily_pnl.return_value = -5.0

        cfg = MagicMock()
        cfg.get = lambda k, d=None: (
            {
                "instruments": {
                    "wall_street": {
                        "epic": "IX.D.DOW.IFM.IP",
                        "ig_point_value_gbp": 7.87,
                    }
                }
            }
            if k == "instruments"
            else d
        )

        with patch(
            "system.portfolio_envelope.portfolio_gate_enabled", return_value=True
        ):
            rehydrate_portfolio_from_store(store, cfg=cfg)
            snap = env_snap()
            self.assertGreater(snap["concurrent_risk_gbp"], 0)
            self.assertEqual(snap["daily_pnl_gbp"], -5.0)


class CorrelationHeatTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_correlation_guard_for_tests()

    def tearDown(self) -> None:
        reset_correlation_guard_for_tests()

    def test_blocks_when_direction_heat_exceeded(self) -> None:
        rehydrate_direction_risk(buy_risk_gbp=350.0, sell_risk_gbp=0.0)
        with patch(
            "execution.correlation_guard._max_same_direction_risk_gbp",
            return_value=400.0,
        ):
            ok, reason = check_and_record("BUY", risk_gbp=100.0)
            self.assertFalse(ok)
            self.assertIn("same-direction cap", reason)

    def test_confirm_and_release_direction_risk(self) -> None:
        rehydrate_direction_risk(buy_risk_gbp=100.0, sell_risk_gbp=0.0)
        confirm_direction_risk("BUY", 50.0)
        snap = snapshot()
        self.assertEqual(snap["buy_risk_gbp"], 150.0)
        release_direction_risk("BUY", 50.0)
        snap2 = snapshot()
        self.assertEqual(snap2["buy_risk_gbp"], 100.0)


class ShadowOffsetTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_shadow_offsets_for_tests()

    def test_save_and_load_offset(self) -> None:
        with patch(
            "system.v26_shadow_offsets._PATH",
            Path(tempfile.mkdtemp()) / "offsets.json",
        ):
            save_offset("2026-06-08", 12345)
            self.assertEqual(load_offset("2026-06-08"), 12345)


class ShadowSetupBanTests(unittest.TestCase):
    def test_setup_ban_blocks_shadow_intent(self) -> None:
        from shadow.runner import _apply_setup_ban_guard, reset_shadow_state
        from strategies.base import ShadowIntent

        reset_shadow_state()
        intent = ShadowIntent(
            strategy_id="S1",
            epic="CS.D.EURUSD.CFD.IP",
            market="EUR/USD",
            session="london_morning",
            direction="BUY",
            would_trade=True,
            confidence=80.0,
            setup_key="BUY|bull|test",
            source_ts="2026-06-08T12:00:00Z",
            reason="signal",
            payload={},
        )
        with patch("system.setup_registry.is_setup_banned", return_value=True):
            out = _apply_setup_ban_guard(intent)
        self.assertFalse(out.would_trade)
        self.assertTrue(out.payload.get("setup_banned"))


class MlVetoPromoteTests(unittest.TestCase):
    def test_build_per_epic_from_manifest(self) -> None:
        scripts = ROOT / "scripts"
        sys.path.insert(0, str(scripts))
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "v26_ml_veto_promote",
            scripts / "v26_ml_veto_promote.py",
        )
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)

        manifest = {
            "by_epic": {
                "CS.D.EURUSD.CFD.IP": {
                    "ok": True,
                    "val_wr": 0.54,
                    "veto_eligible": True,
                    "recommended_min_prob": 0.53,
                },
                "IX.D.DOW.IFM.IP": {
                    "ok": True,
                    "val_wr": 0.48,
                    "veto_eligible": False,
                    "recommended_min_prob": 0.52,
                },
            }
        }
        per = mod.build_per_epic(manifest, min_val_wr=0.52, dry_run=True)
        self.assertIn("CS.D.EURUSD.CFD.IP", per)
        self.assertNotIn("IX.D.DOW.IFM.IP", per)


if __name__ == "__main__":
    unittest.main()
