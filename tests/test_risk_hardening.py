"""Risk hardening — shadow force-run, correlation caps, adaptive gate stops."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ai.strategy.performance_reviewer import force_shadow_learning_pipeline
from data.learning_store import LearningStore
from data.models import Quote
from execution.correlation_guard import (
    check_open_book_limits,
    reset_correlation_guard_for_tests,
)
from trading.trading_loop import TradingLoop


def _quote() -> Quote:
    return Quote(datetime(2026, 6, 9, 21, 0), 100.0, 100.5)


class CorrelationOpenBookTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_correlation_guard_for_tests()

    def test_blocks_second_us_index_short(self) -> None:
        positions = [
            {"epic": "IX.D.DOW.IFM.IP", "side": "SELL", "size": -0.5},
        ]
        ok, detail = check_open_book_limits(
            "IX.D.NASDAQ.IFM.IP",
            "SELL",
            positions,
        )
        self.assertFalse(ok)
        self.assertIn("US index shorts", detail)

    def test_blocks_third_global_open(self) -> None:
        positions = [
            {"epic": "CS.D.CFPGOLD.CFP.IP", "side": "SELL", "size": -1},
            {"epic": "IX.D.NIKKEI.IFM.IP", "side": "BUY", "size": 1},
        ]
        ok, detail = check_open_book_limits(
            "CS.D.EURUSD.CFD.IP",
            "BUY",
            positions,
        )
        self.assertFalse(ok)
        self.assertIn("global open book", detail)

    def test_allows_add_to_existing_epic(self) -> None:
        positions = [
            {"epic": "IX.D.DOW.IFM.IP", "side": "SELL", "size": -0.5},
            {"epic": "CS.D.CFPGOLD.CFP.IP", "side": "SELL", "size": -1},
        ]
        ok, _ = check_open_book_limits("IX.D.DOW.IFM.IP", "SELL", positions)
        self.assertTrue(ok)


class ForceShadowLearningTests(unittest.TestCase):
    def test_force_processes_fired_and_skipped_for_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            shadow = tmp_path / "shadow_log.jsonl"
            day = "2026-06-09"
            rows = [
                {
                    "timestamp": f"{day}T21:45:00",
                    "market": "Japan 225",
                    "direction": "SELL",
                    "would_have_fired": False,
                    "setup_key": "SELL|bear|us_afternoon|atr30-60|rsilow|volhigh",
                    "atr": 10.0,
                },
                {
                    "timestamp": f"{day}T21:50:00",
                    "market": "Japan 225",
                    "direction": "SELL",
                    "would_have_fired": True,
                    "setup_key": "SELL|bear|us_afternoon|atr60-90|rsilow|volhigh",
                    "atr": 12.0,
                },
            ]
            shadow.write_text(
                "\n".join(json.dumps(r) for r in rows) + "\n",
                encoding="utf-8",
            )
            ohlc_dir = tmp_path / "ohlc_cache"
            ohlc_dir.mkdir()
            base = datetime(2026, 6, 9, 18, 0, tzinfo=timezone.utc)
            bars = []
            price = 200.0
            for i in range(60):
                t = base + timedelta(minutes=5 * i)
                if i >= 45:
                    price -= 4.0
                bars.append(
                    {
                        "time": t.strftime("%Y-%m-%d %H:%M:%S"),
                        "high": price + 2,
                        "low": price - 12,
                        "close": price,
                    }
                )
            (ohlc_dir / "nikkei_5m.jsonl").write_text(
                "\n".join(json.dumps(b) for b in bars) + "\n",
                encoding="utf-8",
            )
            db_path = tmp_path / "learning.db"
            store = LearningStore(str(db_path))
            store.connect()
            with (
                patch(
                    "ai.strategy.performance_reviewer.shadow_log_paths",
                    return_value=[shadow],
                ),
                patch(
                    "ai.strategy.performance_reviewer.ohlc_cache_path",
                    lambda epic, market="": ohlc_dir / "nikkei_5m.jsonl",
                ),
                patch(
                    "ai.strategy.performance_reviewer._market_epic_map",
                    return_value={"Japan 225": "IX.D.NIKKEI.IFM.IP"},
                ),
            ):
                result = force_shadow_learning_pipeline(store, day=day)
            self.assertGreaterEqual(result.ingested, 1)
            store.close()


class AdaptiveGateStopTests(unittest.TestCase):
    def test_execution_stop_distance_prefers_adaptive_atr(self) -> None:
        from tests.test_trading_loop import _make_loop

        loop = _make_loop()
        loop._config.stop_distance_points = 80.0
        loop._config.adaptive_atr_risk_enabled = True
        loop._config.atr_multiplier = 2.5
        loop._execution_loop.execution_engine._adaptive.settings.side_effect = (
            lambda *a, **k: {"risk": 22.5, "atr": 9.0}
        )
        stop, source = loop._execution_stop_distance(
            setup_key="SELL|bear|us_afternoon|atr30-60|rsilow|volhigh",
            planning_conf=80.0,
            snapshot={"last": {"atr": 9.0, "spread": 1.0}},
        )
        self.assertEqual(stop, 22.5)
        self.assertEqual(source, "adaptive_atr")

    def test_gate_risk_uses_adaptive_stop_not_config_fixed(self) -> None:
        from tests.test_trading_loop import _make_loop

        loop = _make_loop()
        loop._config.stop_distance_points = 80.0
        loop._config.trade_size = 1.0
        loop._config.get = MagicMock(
            side_effect=lambda key, default=None: {
                "ig_point_value_gbp": 1.0,
                "risk_cap_gbp": 500,
            }.get(key, default)
        )
        loop._points.get_size_multiplier.return_value = 1.0
        loop._execution_loop.execution_engine._adaptive.settings.side_effect = (
            lambda *a, **k: {"risk": 25.0, "atr": 10.0}
        )
        loop._signal_engine.last_snapshot = {
            loop._market: {"last": {"atr": 10.0, "spread": 1.0}}
        }
        loop._signal_engine.evaluate.return_value = MagicMock(
            adjusted_confidence=80.0,
            setup_key="BUY|bull|london_morning|atr30-60|rsimid|volnormal",
            signal="BUY",
        )
        rest = MagicMock()
        rest.fetch_market_constraints.return_value = {"min_deal_size": 1.0}
        loop._execution_loop.execution_engine._rest_client = rest
        loop._execution_loop.execution_engine.trade_tracker.snapshot.return_value = {
            "positions": []
        }

        with (
            patch("system.market_data_hub.get_market_data_hub") as hub_mock,
            patch("system.risk_bands.bands_enabled", return_value=False),
        ):
            hub_mock.return_value.normal_spread.return_value = 5.0
            gate = loop._gate_risk_validation(_quote())

        self.assertTrue(gate.passed)
        self.assertEqual(gate.value["stop_points"], 25.0)
        self.assertEqual(gate.value["stop_source"], "adaptive_atr")
        self.assertEqual(gate.value["risk_gbp"], 25.0)


if __name__ == "__main__":
    unittest.main()
