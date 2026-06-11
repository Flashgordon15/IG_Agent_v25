"""v29.1 daily loss baseline reset + soft pause."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.config import Config
from system.daily_loss_policy import (
    RUNTIME_BASELINE_KEY,
    RUNTIME_DAY_KEY,
    RUNTIME_VERSION_KEY,
    daily_loss_gate_status,
    effective_daily_loss_gbp,
    effective_daily_pnl,
)
from system.v291_upgrade import apply_v291_upgrade


class DailyLossPolicyTests(unittest.TestCase):
    def test_effective_pnl_zero_after_baseline_same_day(self) -> None:
        store = MagicMock()
        store.sum_daily_pnl.return_value = -1338.17
        store.get_runtime_state.side_effect = lambda k: {
            RUNTIME_DAY_KEY: __import__("datetime").date.today().isoformat(),
            RUNTIME_BASELINE_KEY: "-1338.1700",
        }.get(k)
        self.assertAlmostEqual(effective_daily_pnl(store), 0.0, places=2)
        self.assertAlmostEqual(effective_daily_loss_gbp(store), 0.0, places=2)

    def test_soft_pause_blocks_before_hard(self) -> None:
        store = MagicMock()
        store.sum_daily_pnl.return_value = -450.0
        store.get_runtime_state.return_value = None
        cfg = Config(_data={"max_daily_loss_gbp": 500, "learning_demo_mode": {}})
        ok, detail, meta = daily_loss_gate_status(store, cfg)
        self.assertFalse(ok)
        self.assertIn("soft pause", detail.lower())
        self.assertEqual(meta.get("tier"), "soft")

    def test_hard_stop_at_limit(self) -> None:
        store = MagicMock()
        store.sum_daily_pnl.return_value = -520.0
        store.get_runtime_state.return_value = None
        cfg = Config(_data={"max_daily_loss_gbp": 500})
        ok, detail, meta = daily_loss_gate_status(store, cfg)
        self.assertFalse(ok)
        self.assertEqual(meta.get("tier"), "hard")


class GateCoherenceEffectivePnlTests(unittest.TestCase):
    def test_coherence_source_uses_effective_daily_pnl(self) -> None:
        import inspect

        from system import gate_coherence

        src = inspect.getsource(gate_coherence.audit_trading_readiness)
        self.assertIn("daily_pnl_gbp=float(effective_daily_pnl(store", src)


class V291UpgradeTests(unittest.TestCase):
    def test_apply_refreshes_baseline_on_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from data.learning_store import LearningStore

            db = Path(tmp) / "test.db"
            store = LearningStore(str(db))
            cfg = Config(
                _data={
                    "learning_demo_mode": {
                        "daily_loss_reset": {
                            "enabled": True,
                            "upgrade_version": "v29.1",
                            "refresh_on_startup": True,
                        }
                    }
                }
            )
            with patch.object(store, "sum_daily_pnl", return_value=-1000.0):
                first = apply_v291_upgrade(store, cfg=cfg)
            self.assertTrue(first.get("applied"))
            self.assertEqual(store.get_runtime_state(RUNTIME_VERSION_KEY), "v29.1")
            with patch.object(store, "sum_daily_pnl", return_value=-1200.0):
                second = apply_v291_upgrade(store, cfg=cfg)
            self.assertTrue(second.get("applied"))
            self.assertAlmostEqual(effective_daily_loss_gbp(store), 0.0, places=2)
