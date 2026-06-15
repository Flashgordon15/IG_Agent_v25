"""Phase 2 system hardening regression tests."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api.auth import admin_password, reset_auth_for_tests
from api.close_handler import reset_close_handler_for_tests
from api.server import create_app
from api.snapshot_store import reset_snapshot_store_for_tests, set_snapshot_path_for_tests
from ml.interim_scorer import InterimConfidenceScorer
from system.config import Config
from trading.entry_protection import session_window_key
from trading.manual_intervention import shield_threshold_gbp
from trading.points_engine import PointsEngine, set_points_state_path_for_tests


def _login(client: TestClient) -> str:
    res = client.post("/api/auth/login", json={"password": admin_password()})
    assert res.status_code == 200, res.text
    return res.headers.get("X-Auth-Token") or res.cookies.get("ig_agent_auth")


class SystemHardeningTests(unittest.TestCase):
    def test_interim_scorer_min_recent_score_when_few_trades(self) -> None:
        cfg = Config(
            _data={
                "interim_scorer": {"interim_scorer_min_recent_score": 20},
                "interim_scorer_weights": {
                    "trend": 25,
                    "session": 25,
                    "volatility": 25,
                    "recent_performance": 25,
                },
            }
        )
        scorer = InterimConfidenceScorer()
        store = type("S", (), {})()
        store.recent_confirmed_closed_trades = lambda limit=10: [{"result": "WIN"}] * 3
        snap = {"last": {"fast_ema": 101, "slow_ema": 100, "atr": 5, "rsi": 60}}
        with patch("ml.interim_scorer.log_engine"):
            result = scorer.score(
                cfg=cfg,
                market="Gold",
                direction="BUY",
                snapshot=snap,
                store=store,
            )
        self.assertEqual(result.recent_performance, 20.0)

    def test_session_cap_resets_at_london_boundaries(self) -> None:
        london = ZoneInfo("Europe/London")
        late = datetime(2026, 6, 15, 6, 30, tzinfo=london)
        open_am = datetime(2026, 6, 15, 7, 5, tzinfo=london)
        us_open = datetime(2026, 6, 15, 13, 25, tzinfo=london)
        us_pm = datetime(2026, 6, 15, 13, 35, tzinfo=london)
        self.assertNotEqual(session_window_key(late), session_window_key(open_am))
        self.assertNotEqual(session_window_key(open_am), session_window_key(us_pm))
        self.assertNotEqual(session_window_key(us_open), session_window_key(us_pm))

    def test_drawdown_shield_threshold_demo_1000(self) -> None:
        cfg = Config(
            _data={
                "manual_intervention": {
                    "daily_drawdown_shield_gbp": 1000,
                    "daily_loss_limit_gbp": 1000,
                }
            }
        )
        self.assertEqual(shield_threshold_gbp(cfg), 1000.0)

    def test_agent_time_boundary_fields(self) -> None:
        from api.agent_time import get_agent_time_payload

        payload = get_agent_time_payload(
            at=datetime(2026, 6, 15, 12, 0, tzinfo=ZoneInfo("Europe/London"))
        )
        self.assertIn("next_boundary", payload)
        self.assertIn("minutes_to_boundary", payload)
        self.assertIn("boundary_type", payload)
        if payload["boundary_type"] is not None:
            self.assertIn(payload["boundary_type"], ("OPEN", "CLOSE"))


class PointsResetEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_auth_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        snap = Path(self.tmp.name) / "dashboard_snapshot.json"
        self.points_path = Path(self.tmp.name) / "points_state.json"
        self.points_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "cumulative": -12.0,
                    "cumulative_points": -12.0,
                    "state": "WARNING",
                    "session_score": 0.0,
                    "last_trade_score": 0.0,
                    "consecutive_losses": 2,
                    "signals_to_skip": 0,
                    "recovery_wins": 0,
                    "bootstrap_wins": 0,
                    "day_stopped": False,
                    "stop_latched": False,
                    "last_nominal": "WARNING",
                    "rapid_cooldown_until": 0.0,
                }
            ),
            encoding="utf-8",
        )
        reset_snapshot_store_for_tests()
        reset_close_handler_for_tests()
        set_snapshot_path_for_tests(snap)
        set_points_state_path_for_tests(self.points_path)
        self.client = TestClient(create_app(watch_snapshot=False))
        self.token = _login(self.client)

    def tearDown(self) -> None:
        self.client.close()
        reset_auth_for_tests()
        reset_snapshot_store_for_tests()
        reset_close_handler_for_tests()
        set_points_state_path_for_tests(None)
        self.tmp.cleanup()

    def test_points_reset_endpoint_requires_confirm(self) -> None:
        headers = {"X-Auth-Token": self.token}
        missing = self.client.post("/api/admin/reset-points", json={}, headers=headers)
        self.assertEqual(missing.status_code, 400)
        false_confirm = self.client.post(
            "/api/admin/reset-points", json={"confirm": False}, headers=headers
        )
        self.assertEqual(false_confirm.status_code, 400)
        ok = self.client.post(
            "/api/admin/reset-points", json={"confirm": True}, headers=headers
        )
        self.assertEqual(ok.status_code, 200, ok.text)
        body = ok.json()
        self.assertTrue(body.get("success"))
        self.assertAlmostEqual(float(body.get("previous_cumulative")), -12.0)
        self.assertEqual(body.get("new_state"), "HEALTHY")
        engine = PointsEngine(state_path=self.points_path)
        self.assertEqual(engine.get_state(), "HEALTHY")
        self.assertAlmostEqual(engine.snapshot().cumulative, 0.0)


if __name__ == "__main__":
    unittest.main()
