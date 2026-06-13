"""Execution-layer spread-to-ATR entry shield."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.learning_store import LearningStore
from data.models import Quote
from execution.execution_engine import ExecutionEngine
from execution.spread_atr_circuit import (
    BLOCKED_SPREAD_TO_ATR_CIRCUIT_BREAKER,
    entry_spread_atr_blocked,
    execution_spread_atr_block_message,
    spread_to_atr_circuit_max,
    spread_to_atr_ratio,
)
from execution.types import ExecutionMode, TradeSignal
from system.config import Config


def _minimal_cfg() -> Config:
    return Config(
        _data={
            "dry_run": True,
            "allow_live_trading": False,
            "trade_size": 1.0,
            "stop_distance_points": 30.0,
            "limit_distance_points": 90.0,
            "spread_to_atr_circuit_breaker_max": 0.30,
            "cooldown_seconds": 0,
            "max_open_positions": 5,
            "max_positions_per_epic": 2,
        }
    )


class SpreadAtrCircuitTests(unittest.TestCase):
    def test_soak_override_applies(self) -> None:
        with patch(
            "system.gate_relaxation.soak_spread_to_atr_max",
            return_value=0.45,
        ):
            self.assertEqual(spread_to_atr_circuit_max(_minimal_cfg(), "IX.D.TEST"), 0.45)

    def test_entry_blocked_when_ratio_exceeds_max(self) -> None:
        quote = Quote(datetime(2026, 6, 13, 12, 0), 100.0, 140.0)
        snapshot = {"last": {"atr": 100.0}}
        with patch(
            "system.gate_relaxation.soak_spread_to_atr_max",
            side_effect=lambda d: d,
        ):
            blocked, ratio, max_ratio = entry_spread_atr_blocked(
                quote, snapshot, _minimal_cfg(), "IX.D.TEST"
            )
        self.assertTrue(blocked)
        self.assertAlmostEqual(ratio, 0.40)
        self.assertEqual(max_ratio, 0.30)

    def test_execution_message_format(self) -> None:
        msg = execution_spread_atr_block_message("SIMULATOR", 0.35, max_ratio=0.30)
        self.assertIn("[RISK ENGINE] (SIMULATOR)", msg)
        self.assertIn("0.35", msg)
        self.assertIn("30% ATR limit", msg)

    def test_forex_fractional_spread_ratio(self) -> None:
        quote = Quote(datetime(2026, 6, 13, 12, 0), 1.08510, 1.08522)
        snapshot = {"last": {"atr": 0.00060}}
        with patch(
            "system.gate_relaxation.soak_spread_to_atr_max",
            side_effect=lambda d: d,
        ):
            ratio, atr = spread_to_atr_ratio(quote, snapshot)
            blocked, _, max_ratio = entry_spread_atr_blocked(
                quote,
                snapshot,
                _minimal_cfg(),
                "CS.D.EURUSD.CFD.IP",
            )
        self.assertAlmostEqual(quote.spread, 0.00012)
        self.assertAlmostEqual(atr, 0.00060)
        self.assertAlmostEqual(ratio, 0.20)
        self.assertFalse(blocked)
        self.assertEqual(max_ratio, 0.30)

    def test_zero_atr_fail_open_no_crash(self) -> None:
        quote = Quote(datetime(2026, 6, 13, 12, 0), 18000.0, 18001.5)
        with patch(
            "system.gate_relaxation.soak_spread_to_atr_max",
            side_effect=lambda d: d,
        ):
            blocked, ratio, _ = entry_spread_atr_blocked(
                quote,
                {"last": {"atr": 0}},
                _minimal_cfg(),
                "IX.D.DAX.IG.IP",
            )
        self.assertFalse(blocked)
        self.assertEqual(ratio, 0.0)

        blocked_missing, ratio_missing, _ = entry_spread_atr_blocked(
            quote,
            None,
            _minimal_cfg(),
            "IX.D.DAX.IG.IP",
        )
        self.assertFalse(blocked_missing)
        self.assertEqual(ratio_missing, 0.0)


class ExecutionEngineSpreadShieldTests(unittest.TestCase):
    def test_simulator_path_blocked_at_dispatch(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        store = LearningStore(str(Path(tmp.name) / "t.db"))
        store.connect()
        cfg = _minimal_cfg()
        engine = ExecutionEngine(
            mode=ExecutionMode.TEST,
            config=cfg,
            store=store,
        )
        signal = TradeSignal(
            market="Test",
            epic="IX.D.TEST",
            direction="BUY",
            raw_confidence=90.0,
            adjusted_confidence=90.0,
            setup_key="BUY|bull|asia",
            quote=Quote(datetime(2026, 6, 13, 12, 0), 100.0, 140.0),
            snapshot={"last": {"atr": 100.0}},
            notes="",
        )
        with patch(
            "execution.economic_check.integrity_gate_sourced_required",
            return_value=False,
        ), patch.object(
            engine._risk,
            "assess",
            return_value=MagicMock(
                approved=True,
                size=1.0,
                stop_distance=30.0,
                limit_distance=90.0,
                reason="",
            ),
        ), patch(
            "system.gate_relaxation.soak_spread_to_atr_max",
            side_effect=lambda d: d,
        ):
            result = engine.execute_trade(signal, prevalidated=True)
        self.assertEqual(result.action, "REJECTED")
        self.assertEqual(result.rejection_reason, BLOCKED_SPREAD_TO_ATR_CIRCUIT_BREAKER)
        store.close()
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
