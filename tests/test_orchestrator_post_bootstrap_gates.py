"""
Post-OHLC bootstrap: real SignalEngine + EnvironmentScorer through orchestrator gates.

Proves gate 3 and gate 6 see bootstrapped candles (not empty / collecting).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.models import Quote
from data.learning_store import LearningStore
from execution.trading_loop import TickOutcome, TradingLoop as ExecutionTickLoop
from signals.signal_engine import SignalEngine
from system.config_loader import ConfigLoader
from trading.environment_scorer import GATE_PASS_MIN, EnvironmentScorer
from trading.ohlc_bootstrap import bootstrap_ohlc_for_session
from trading.points_engine import PointsEngine, set_points_state_path_for_tests
from trading.session_manager import SessionManager
from trading.trading_loop import TradingLoop


def _quote(at: datetime | None = None) -> Quote:
    t = at or datetime(2026, 5, 27, 12, 0)
    return Quote(t, 65000.0, 65007.0)


def _ohlc_bars(n: int = 100) -> list[dict]:
    base = datetime(2026, 5, 27, 10, 0)
    bars = []
    for i in range(n):
        t = base + timedelta(minutes=5 * i)
        bars.append(
            {
                "time": f"{t.year}/{t.month:02d}/{t.day:02d}:{t.hour:02d}:{t.minute:02d}:00",
                "high": 65100.0 + i,
                "low": 65090.0 + i,
                "bid_close": 65095.0 + i,
                "offer_close": 65102.0 + i,
                "close": 65098.0 + i,
            }
        )
    return bars


class OrchestratorPostBootstrapGatesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        set_points_state_path_for_tests(Path(self._tmp.name) / "points.json")

    def tearDown(self) -> None:
        set_points_state_path_for_tests(None)
        self._tmp.cleanup()

    @patch("system.market_data_hub.get_market_data_hub")
    @patch("trading.trading_loop.publish_tick")
    def test_gates_see_bootstrapped_candles(
        self, _publish: MagicMock, hub_mock: MagicMock
    ) -> None:
        hub = MagicMock()
        hub.is_in_maintenance.return_value = False
        hub.get_snapshot.return_value = MagicMock(age_seconds=lambda: 1.0)
        hub_mock.return_value = hub

        cfg = ConfigLoader(ROOT / "config" / "config_v25.json").load_config()
        market = "Japan 225"
        epic = "IX.D.NIKKEI.IFM.IP"

        store = LearningStore(str(Path(self._tmp.name) / "learning.db"))
        signal_engine = SignalEngine(cfg, store)
        env_scorer = EnvironmentScorer(signal_engine, config=cfg, epic=epic)
        points = PointsEngine(store, state_path=Path(self._tmp.name) / "points.json")
        session = SessionManager(
            epic,
            market=market,
            signal_engine=signal_engine,
            environment_scorer=env_scorer,
            points_engine=points,
            state_path=Path(self._tmp.name) / "session.json",
        )

        rest = MagicMock()
        rest.fetch_price_history.return_value = _ohlc_bars(100)
        injected = bootstrap_ohlc_for_session(
            rest, signal_engine, epic, market, environment_scorer=env_scorer, prefer_cache=False
        )
        self.assertEqual(injected, 100)

        _, c5, c15 = signal_engine.candle_frames(market)
        self.assertGreaterEqual(len(c5), 20)
        self.assertGreaterEqual(len(c15), 2)

        q = _quote()
        session.on_session_open(q)
        session._bars_at_open = 0  # type: ignore[attr-defined]

        exec_loop = MagicMock(spec=ExecutionTickLoop)
        exec_loop.execution_engine = MagicMock()
        exec_loop.execution_engine.trade_tracker.count_open_for_epic.return_value = 0
        exec_loop.execution_engine.trade_tracker.snapshot.return_value = {
            "positions": []
        }
        exec_loop.execution_engine.update_positions = MagicMock()

        loop = TradingLoop(
            cfg,
            market=market,
            epic=epic,
            session_manager=session,
            environment_scorer=env_scorer,
            points_engine=points,
            signal_engine=signal_engine,
            execution_loop=exec_loop,
            quote_source=lambda: q,
            learning_store=store,
            tick_interval_sec=0.05,
        )

        with patch.object(session, "is_session_open", return_value=True), patch.object(
            session, "is_cold_start", return_value=False
        ), patch.object(session, "check_gap_open", return_value=False), patch.object(
            session, "bars_since_open", return_value=10
        ), patch.object(
            session, "is_entry_blocked_near_session_end", return_value=(False, None)
        ), patch.object(
            session, "should_flatten", return_value=False
        ), patch.object(
            session, "should_run_flatten_attempt", return_value=False
        ), patch.object(
            session, "snapshot", return_value=MagicMock(phase="OPEN")
        ):
            gates = loop._evaluate_gates(q)

        by_name = {g.name: g for g in gates}

        fitness = by_name["environment_fitness"]
        self.assertTrue(
            fitness.passed,
            f"environment_fitness should pass: {fitness.detail}",
        )
        fit_score = (
            fitness.value.get("score")
            if isinstance(fitness.value, dict)
            else fitness.value
        )
        self.assertGreaterEqual(float(fit_score or 0), GATE_PASS_MIN)

        signal_gate = by_name["signal_confidence"]
        self.assertNotIn(
            "collecting candle history",
            (signal_gate.detail or "").lower(),
        )
        self.assertNotIn(
            "collecting live data",
            (signal_gate.detail or "").lower(),
        )

        sig_val = signal_gate.value
        if isinstance(sig_val, dict) and sig_val.get("signal"):
            notes = str(sig_val["signal"].notes or "").lower()
            self.assertNotIn("collecting live data", notes)

        df = signal_engine.quote_df(market)
        self.assertGreaterEqual(len(df), 50)


if __name__ == "__main__":
    unittest.main()
