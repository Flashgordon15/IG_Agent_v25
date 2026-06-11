"""Tests for L4/L5 demo forward certification."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "v26"))

from research.l4_forward import (
    evaluate_l4_forward,
    evaluate_l5_prove,
    write_forward_cert,
)


class L4ForwardTests(unittest.TestCase):
    def test_l5_counts_days_above_target(self) -> None:
        with patch(
            "research.l4_forward.list_event_days", return_value=["d1", "d2", "d3"]
        ):
            with patch(
                "research.l4_forward.collect_forward_days",
                return_value=[
                    type(
                        "R",
                        (),
                        {
                            "day": "d1",
                            "fill_closes": 1,
                            "fill_pnl_gbp": 300.0,
                            "order_intents": 1,
                        },
                    )(),
                    type(
                        "R",
                        (),
                        {
                            "day": "d2",
                            "fill_closes": 1,
                            "fill_pnl_gbp": 50.0,
                            "order_intents": 1,
                        },
                    )(),
                    type(
                        "R",
                        (),
                        {
                            "day": "d3",
                            "fill_closes": 1,
                            "fill_pnl_gbp": 280.0,
                            "order_intents": 1,
                        },
                    )(),
                ],
            ):
                with patch(
                    "research.l4_forward._load_cert_config",
                    return_value={
                        "l5_window_days": 3,
                        "l5_days_required": 2,
                        "l5_daily_target_gbp": 250,
                    },
                ):
                    l5 = evaluate_l5_prove()
        self.assertEqual(l5["days_hit_target"], 2)
        self.assertEqual(l5["status"], "PASS")

    def test_l4_not_started_without_fills(self) -> None:
        with patch("research.l4_forward.list_event_days", return_value=["2026-06-01"]):
            with patch(
                "research.l4_forward.collect_forward_days",
                return_value=[
                    type(
                        "R",
                        (),
                        {
                            "day": "2026-06-01",
                            "fill_closes": 0,
                            "fill_pnl_gbp": 0.0,
                            "order_intents": 0,
                        },
                    )(),
                ],
            ):
                with patch(
                    "research.l4_forward._load_cert_config",
                    return_value={
                        "l4_window_days": 14,
                        "l4_median_daily_gbp": 100,
                        "l4_min_profit_factor": 1.2,
                        "l4_min_trading_days": 7,
                    },
                ):
                    l4 = evaluate_l4_forward()
        self.assertEqual(l4["status"], "NOT_STARTED")

    def test_write_forward_cert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            with patch("research.l4_forward._project_root", return_value=Path(tmp)):
                with patch(
                    "research.l4_forward.evaluate_l4_forward",
                    return_value={"status": "IN_PROGRESS", "pct": 10},
                ):
                    with patch(
                        "research.l4_forward.evaluate_l5_prove",
                        return_value={"status": "NOT_STARTED", "pct": 0},
                    ):
                        path = write_forward_cert()
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("l4", data)
            self.assertIn("l5", data)


if __name__ == "__main__":
    unittest.main()
