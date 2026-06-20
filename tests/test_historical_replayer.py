"""Historical replayer + virtual clock integration tests."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from data.models import Quote
from simulation.replay_clock import clear_replay_clock, is_replay_active, set_replay_time


class HistoricalReplayerTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_replay_clock()

    def test_load_jsonl_and_virtual_clock_integrity(self) -> None:
        from simulation.historical_replayer import HistoricalReplayer, ReplayTick, load_ticks
        from system.market_data_hub import MarketDataHub
        from system.market_integrity import check_quote_integrity

        sample = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "simulation"
            / "data"
            / "sample_ticks.jsonl"
        )
        ticks = load_ticks(sample)
        self.assertGreaterEqual(len(ticks), 4)

        hub = MarketDataHub()
        ts = datetime(2026, 6, 10, 8, 0, 0, tzinfo=timezone.utc).timestamp()
        replayer = HistoricalReplayer(
            [ReplayTick("CS.D.EURUSD.CFD.IP", 1.085, 1.0852, ts)],
            speed=1000.0,
            hub=hub,
        )
        replayer.emit_tick(replayer._ticks[0])

        self.assertTrue(is_replay_active())
        snap = hub.get_snapshot("CS.D.EURUSD.CFD.IP")
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertLessEqual(snap.age_seconds(), 0.001)

        quote = snap.to_quote()
        with patch("system.market_integrity.epic_market_open", return_value=True):
            verdict = check_quote_integrity("CS.D.EURUSD.CFD.IP", quote)
        self.assertTrue(verdict.allowed, verdict.reason)

    def test_csv_loader(self) -> None:
        from simulation.historical_replayer import load_ticks

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ticks.csv"
            path.write_text(
                "epic,bid,offer,timestamp\n"
                "CS.D.EURUSD.CFD.IP,1.0800,1.0802,2026-06-10T09:00:00+00:00\n",
                encoding="utf-8",
            )
            ticks = load_ticks(path)
            self.assertEqual(len(ticks), 1)
            self.assertEqual(ticks[0].epic, "CS.D.EURUSD.CFD.IP")

    def test_replay_clock_advances(self) -> None:
        from simulation.replay_clock import now_datetime

        t0 = datetime(2026, 6, 10, 14, 30, tzinfo=timezone.utc)
        set_replay_time(t0)
        self.assertEqual(now_datetime(), t0)


if __name__ == "__main__":
    unittest.main()
