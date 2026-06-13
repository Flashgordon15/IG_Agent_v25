"""Tests for risk telemetry formatters and correlation density scaler."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from execution.correlation_matrix import (
    correlation_density,
    correlation_density_risk_multiplier,
    epic_correlation_cluster,
    multiplier_for_density,
)
from execution.risk_telemetry import (
    format_spread_atr_entry_block,
    format_stale_decay_trail_tighten,
    notify_spread_atr_entry_blocked,
    reset_risk_telemetry_for_tests,
)


class CorrelationMatrixTests(unittest.TestCase):
    def test_indices_share_global_equity_cluster(self) -> None:
        self.assertEqual(
            epic_correlation_cluster("IX.D.NASDAQ.IFM.IP"),
            epic_correlation_cluster("IX.D.NIKKEI.IFM.IP"),
        )

    def test_forex_separate_from_indices(self) -> None:
        self.assertNotEqual(
            epic_correlation_cluster("CS.D.EURUSD.CFD.IP"),
            epic_correlation_cluster("IX.D.DOW.IFM.IP"),
        )

    def test_density_counts_cluster_peers(self) -> None:
        positions = [
            {"epic": "IX.D.NASDAQ.IFM.IP", "side": "BUY"},
            {"epic": "CS.D.EURUSD.CFD.IP", "side": "BUY"},
        ]
        self.assertEqual(
            correlation_density("IX.D.DOW.IFM.IP", positions),
            1,
        )
        self.assertEqual(
            correlation_density("CS.D.GBPUSD.CFD.IP", positions),
            1,
        )

    def test_multiplier_tiers(self) -> None:
        self.assertEqual(multiplier_for_density(0), 1.0)
        self.assertEqual(multiplier_for_density(2), 0.5)
        self.assertEqual(multiplier_for_density(4), 0.25)

    def test_risk_multiplier_detail(self) -> None:
        mult, density, detail = correlation_density_risk_multiplier(
            "IX.D.DOW.IFM.IP",
            [{"epic": "IX.D.NASDAQ.IFM.IP"}],
        )
        self.assertEqual(density, 1)
        self.assertEqual(mult, 0.75)
        self.assertIn("global_equity", detail)


class RiskTelemetryFormatTests(unittest.TestCase):
    def test_spread_block_format(self) -> None:
        text = format_spread_atr_entry_block(
            "IX.D.NIKKEI.IFM.IP",
            spread_pts=12.0,
            atr_pts=30.0,
            max_ratio=0.30,
            ratio=0.40,
        )
        self.assertIn("[RISK SHIELD]", text)
        self.assertIn("12.0 pts", text)
        self.assertIn("30%", text)
        self.assertIn("9.0 pts", text)

    def test_stale_decay_format(self) -> None:
        text = format_stale_decay_trail_tighten(
            "IX.D.NIKKEI.IFM.IP",
            market="Japan 225",
            side="BUY",
            stop=137.5,
            compression_pct=0.5,
            age_minutes=40,
        )
        self.assertIn("Stale Decay Trail", text)
        self.assertIn("50%", text)
        self.assertIn("40m", text)

    @patch("execution.risk_telemetry._dispatch_telegram")
    def test_notify_spread_uses_async_dispatch(self, mock_dispatch: MagicMock) -> None:
        reset_risk_telemetry_for_tests()
        from data.models import Quote
        from datetime import datetime

        q = Quote(datetime.now(), 100.0, 100.5)
        notify_spread_atr_entry_blocked(
            "IX.D.NIKKEI.IFM.IP",
            q,
            {"last": {"atr": 10.0}},
            max_ratio=0.30,
            ratio=0.45,
        )
        mock_dispatch.assert_called_once()
        self.assertIn("[RISK SHIELD]", mock_dispatch.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
