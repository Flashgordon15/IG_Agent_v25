"""Tests for v26 PROFIT API and snapshot reader."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api.close_handler import reset_close_handler_for_tests
from api.server import create_app
from api.snapshot_store import (
    reset_snapshot_store_for_tests,
    set_snapshot_path_for_tests,
)
from api.v26_profit import build_profit_payload


class V26ProfitApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        reset_snapshot_store_for_tests()
        reset_close_handler_for_tests()
        set_snapshot_path_for_tests(Path(self.tmp.name) / "dashboard_snapshot.json")
        self.client = TestClient(create_app(watch_snapshot=False))
        self.state_dir = Path(self.tmp.name) / "state"
        self.state_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.client.close()
        reset_snapshot_store_for_tests()
        reset_close_handler_for_tests()
        self.tmp.cleanup()

    def _write_snapshots(self) -> None:
        (self.state_dir / "expectancy_snapshot.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-06-08T12:00:00Z",
                    "rolling_days": 14,
                    "portfolio": {
                        "n": 5,
                        "wr": 0.2,
                        "e_gbp": -7.34,
                        "total_pnl_gbp": -36.7,
                    },
                    "setups": [
                        {
                            "setup_key": "SELL|bear|asia_early|atr180-210|rsilow|volnormal",
                            "n": 3,
                            "wr": 0.0,
                            "e_gbp": -17.53,
                            "total_pnl_gbp": -52.59,
                            "status": "INSUFFICIENT",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (self.state_dir / "shadow_strategy_pnl.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-06-08T12:00:00Z",
                    "attributed_fills": 5,
                    "total_fills": 7,
                    "by_strategy": {
                        "S1_rules_v25": {
                            "n": 5,
                            "wr": 0.2,
                            "e_gbp": -7.34,
                            "total_pnl_gbp": -36.7,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        (self.state_dir / "v26_ohlc_replay.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-06-08T12:00:00Z",
                    "bar_lab": {
                        "ok": True,
                        "total_bars": 1200,
                        "markets": 3,
                        "by_strategy": {
                            "S2_momentum": {"intents": 40, "would_trade": 12},
                            "S3_session_fx": {"intents": 8, "would_trade": 3},
                        },
                    },
                    "walk_forward": {
                        "ok": True,
                        "total_rows": 100,
                        "by_epic": {
                            "IX.D.NIKKEI.IFM.IP": {
                                "recommended_threshold": 80,
                                "best_wr": 0.55,
                                "total_rows": 50,
                            }
                        },
                    },
                    "ml_veto_hints": [
                        "IX.D.NIKKEI.IFM.IP: replay WR 55% best at ≥80% (n=50)"
                    ],
                }
            ),
            encoding="utf-8",
        )
        (self.state_dir / "v26_trade_learning.json").write_text(
            json.dumps(
                {
                    "replay_historical": {"total_rows": 5000, "fired_rows": 800},
                    "ml_training_store": {"total_records": 16},
                    "ml_readiness": {
                        "combined_proxy": 900,
                        "min_labelled_rows": 500,
                        "ready_for_ml_veto": True,
                    },
                }
            ),
            encoding="utf-8",
        )
        (self.state_dir / "v26_learning_snapshot.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-06-08T12:00:00Z",
                    "portfolio_envelope": {
                        "account_balance_gbp": 10000,
                        "available_gbp": 8800,
                        "concurrent_risk_gbp": 200,
                        "max_concurrent_risk_gbp": 1200,
                        "utilization_pct": 16.7,
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_build_profit_payload_reads_snapshots(self) -> None:
        self._write_snapshots()
        with patch("api.v26_profit._state_dir", return_value=self.state_dir):
            payload = build_profit_payload()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["portfolio"]["n"], 5)
        self.assertIn("S1_rules_v25", payload["shadow_strategies"])
        self.assertEqual(payload["milestones"]["current"], "M0")
        self.assertEqual(payload["bar_lab_historical"]["total_bars"], 1200)
        self.assertEqual(
            payload["bar_lab_historical"]["by_strategy"]["S2_momentum"]["would_trade"],
            12,
        )
        self.assertIn("IX.D.NIKKEI.IFM.IP", payload["walk_forward"]["by_epic"])
        self.assertEqual(payload["portfolio_envelope"]["available_gbp"], 8800)
        self.assertEqual(len(payload["ohlc_replay"]["ml_veto_hints"]), 1)
        self.assertEqual(
            payload["trade_learning"]["replay_historical"]["total_rows"], 5000
        )
        self.assertTrue(payload["ml_readiness"]["ready_for_ml_veto"])

    def test_api_v26_profit_endpoint(self) -> None:
        self._write_snapshots()
        with patch("api.v26_profit._state_dir", return_value=self.state_dir):
            r = self.client.get("/api/v26/profit")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body.get("ok"))
        self.assertIn("setups", body)
        self.assertIn("milestones", body)


if __name__ == "__main__":
    unittest.main()
