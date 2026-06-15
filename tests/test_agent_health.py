"""Health snapshot rules — rest_poll staleness and CLOSED market bypass."""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from api.agent_health import (
    _apply_supervision_init_timeout,
    _health_quote_max_age_sec,
    _quotes_fresh_by_epic,
    evaluate_trading_health,
    reset_init_timeout_state_for_tests,
)


class AgentHealthTests(unittest.TestCase):
    def test_health_quote_max_age_default_for_lightstreamer(self) -> None:
        with patch("api.agent_health._is_rest_poll_transport", return_value=False):
            self.assertEqual(_health_quote_max_age_sec(epic_count=6), 45.0)

    def test_health_quote_max_age_wider_for_rest_poll(self) -> None:
        with (
            patch("api.agent_health._is_rest_poll_transport", return_value=True),
            patch("system.config_loader.get_config") as mock_cfg,
        ):
            cfg = mock_cfg.return_value
            cfg._data = {}
            cfg.refresh_seconds = 5.0
            self.assertEqual(_health_quote_max_age_sec(epic_count=6), 120.0)

    def test_health_quote_max_age_rest_poll_config_override(self) -> None:
        with (
            patch("api.agent_health._is_rest_poll_transport", return_value=True),
            patch("system.config_loader.get_config") as mock_cfg,
        ):
            cfg = mock_cfg.return_value
            cfg._data = {"health_quote_max_age_rest_poll_sec": 180}
            cfg.refresh_seconds = 5.0
            self.assertEqual(_health_quote_max_age_sec(epic_count=6), 180.0)

    def test_evaluate_trading_health_snapshot_closed_exempts_stale_quotes(self) -> None:
        epic = "IX.D.NIKKEI.IFM.IP"
        with (
            patch("api.agent_health._markets_open_count", return_value=1),
            patch("api.agent_health._snapshot_market_state", return_value="CLOSED"),
        ):
            health = evaluate_trading_health(
                loops_running=True,
                paused=False,
                gate_age=8.0,
                epics=[epic],
                quote_fresh={epic: False},
            )
        self.assertTrue(health["trading_healthy"])
        self.assertFalse(health["quotes_required_for_health"])
        self.assertFalse(any(i.startswith("quotes_stale:") for i in health["issues"]))

    def test_evaluate_trading_health_all_snapshot_closed_skips_quotes(self) -> None:
        epics = ["IX.D.NIKKEI.IFM.IP", "CS.D.EURUSD.CFD.IP"]
        with patch(
            "api.agent_health._snapshot_market_state",
            side_effect=lambda e: "CLOSED",
        ):
            health = evaluate_trading_health(
                loops_running=True,
                paused=False,
                gate_age=8.0,
                epics=epics,
                quote_fresh={e: False for e in epics},
            )
        self.assertEqual(health["markets_open_count"], 0)
        self.assertTrue(health["trading_healthy"])
        self.assertFalse(any("quotes_stale" in i for i in health["issues"]))

    def test_supervision_init_timeout_clears_null_fields_after_live_quotes(self) -> None:
        import api.agent_health as ah

        reset_init_timeout_state_for_tests()
        ah._INIT_QUOTES_LIVE_SINCE = time.time() - 95.0

        out = _apply_supervision_init_timeout(
            {
                "quotes_fresh": True,
                "markets_open_count": 2,
                "supervision_drift_ok": None,
                "watchdog_active": None,
            }
        )
        self.assertTrue(out["supervision_drift_ok"])
        self.assertTrue(out["watchdog_active"])
        self.assertTrue(out["init_force_cleared"])
        self.assertGreaterEqual(out["init_live_sec"], 90.0)

    def test_supervision_init_hard_timeout_logs_once(self) -> None:
        import api.agent_health as ah

        reset_init_timeout_state_for_tests()
        ah._INIT_QUOTES_LIVE_SINCE = time.time() - 125.0

        with patch("api.agent_health.log_engine") as mock_log:
            out = _apply_supervision_init_timeout(
                {
                    "quotes_fresh": True,
                    "markets_open_count": 1,
                    "supervision_drift_ok": None,
                    "watchdog_active": None,
                }
            )
            self.assertTrue(out["init_force_cleared"])
            mock_log.assert_called_once()
            self.assertIn("[INIT] Forced clear after timeout", mock_log.call_args[0][0])

            _apply_supervision_init_timeout(out)
            mock_log.assert_called_once()

    def test_quotes_fresh_by_epic_uses_hub_tick_age(self) -> None:
        with patch("system.rest_api_budget.hub_quote_stream_tick_age") as mock_age:
            mock_age.side_effect = lambda epic: 30.0 if epic == "EPIC_A" else None
            result = _quotes_fresh_by_epic(["EPIC_A", "EPIC_B"], max_age=60.0)
        self.assertEqual(result, {"EPIC_A": True, "EPIC_B": False})


if __name__ == "__main__":
    unittest.main()
