"""Tests for stream_ready gate and FX stale threshold behaviour."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.models import Quote
from system import stream_ready
from trading.trading_loop import GateResult, TickContext, TradingLoop


def _quote() -> Quote:
    return Quote(datetime(2026, 6, 2, 21, 0), 1.1634, 1.1635)


def _make_loop() -> TradingLoop:
    from tests.test_trading_loop import _make_loop as base_make

    return base_make()


class StreamReadyTests(unittest.TestCase):
    def setUp(self) -> None:
        stream_ready.reset_stream_ready()

    def tearDown(self) -> None:
        stream_ready.reset_stream_ready()

    def test_signal_and_wait(self) -> None:
        self.assertFalse(stream_ready.is_stream_ready())
        stream_ready.signal_stream_ready(source="test")
        self.assertTrue(stream_ready.is_stream_ready())
        self.assertTrue(stream_ready.wait_stream_ready(timeout=0.1))

    def test_reset_clears_ready(self) -> None:
        stream_ready.signal_stream_ready(source="test")
        stream_ready.reset_stream_ready()
        self.assertFalse(stream_ready.is_stream_ready())


class StreamStaleThresholdTests(unittest.TestCase):
    def test_fx_live_when_stream_ready_and_age_under_60s(self) -> None:
        loop = _make_loop()
        loop._epic = "CS.D.EURUSD.CFD.IP"
        loop._config.refresh_seconds = 5.0
        loop._config.get = MagicMock(
            side_effect=lambda key, default=None: {
                "ig_point_value_gbp": 1.0,
                "risk_cap_gbp": 10,
                "stale_threshold_seconds": 30,
            }.get(key, default)
        )

        snap = MagicMock()
        snap.bid = 1.1634
        snap.offer = 1.1635
        snap.age_seconds.return_value = 25.0

        session = MagicMock()
        session.is_session_open.return_value = True
        session.snapshot.return_value = MagicMock(phase="OPEN")
        loop._session = session

        ctx = TickContext(quote=_quote(), gates=[], all_passed=False)
        ctx.gates = [GateResult("session_open", True, True, "")]

        with patch("system.market_data_hub.get_market_data_hub") as hub_mock, patch(
            "system.stream_ready.is_stream_ready", return_value=True
        ), patch(
            "trading.trading_loop.compute_trade_readiness",
            return_value={"pct": 50, "remaining_pct": 50, "label": "x"},
        ), patch(
            "trading.trading_loop.format_health_badge_text", return_value="BLOCKED"
        ), patch(
            "trading.trading_loop.build_trade_eligibility", return_value={}
        ), patch.object(loop, "_positions_payload", return_value=[]), patch.object(
            loop, "_daily_pnl_signed_gbp", return_value=0.0
        ), patch.object(loop, "_balance_gbp", return_value=10000.0), patch.object(
            loop, "_win_rate_20_pct", return_value=50
        ):
            hub_mock.return_value.get_snapshot.return_value = snap
            hub_mock.return_value.normal_spread.return_value = 1.0
            hub_mock.return_value.is_in_maintenance.return_value = False
            payload = loop._build_snapshot_payload(ctx)

        self.assertEqual(payload["stream_status"], "LIVE")


if __name__ == "__main__":
    unittest.main()
