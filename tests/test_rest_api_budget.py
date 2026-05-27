"""REST API budget — preemptive throttle vs fresh stream (v24 failure register #6)."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.rest_api_budget import RestApiBudget, RestBudgetPausedError


class PreemptiveThrottleTests(unittest.TestCase):
    def test_preemptive_throttle_when_stream_stale_and_budget_high(self) -> None:
        budget = RestApiBudget(min_interval_seconds=0.001, warn_per_minute=6)
        with (
            patch.object(budget, "_maybe_warn_locked"),
            patch.object(budget, "_maybe_periodic_log_locked"),
            patch("system.rest_api_budget.hub_quote_stream_fresh", return_value=False),
            patch("system.rate_limit_manager.get_rate_limit_manager") as mgr,
        ):
            mgr.return_value.check_rest_allowed.return_value = None
            mgr.return_value.is_rest_blocked.return_value = False
            for _ in range(7):
                budget.acquire(label="GET /positions")
        self.assertTrue(budget._preemptive_pause_active())
        with self.assertRaises(RestBudgetPausedError):
            budget.acquire(label="GET /accounts")

    def test_preemptive_throttle_skipped_when_stream_fresh(self) -> None:
        budget = RestApiBudget(min_interval_seconds=0.001, warn_per_minute=6)
        with (
            patch.object(budget, "_maybe_warn_locked"),
            patch.object(budget, "_maybe_periodic_log_locked"),
            patch("system.rest_api_budget.hub_quote_stream_fresh", return_value=True),
            patch("system.rate_limit_manager.get_rate_limit_manager") as mgr,
        ):
            mgr.return_value.check_rest_allowed.return_value = None
            mgr.return_value.is_rest_blocked.return_value = False
            for _ in range(10):
                budget.acquire(label="GET /positions")
        self.assertFalse(budget._preemptive_pause_active())
        budget.acquire(label="GET /accounts")

    def test_preemptive_not_armed_below_eighty_percent_utilization(self) -> None:
        budget = RestApiBudget(min_interval_seconds=0.001, warn_per_minute=6)
        with (
            patch.object(budget, "_maybe_warn_locked"),
            patch.object(budget, "_maybe_periodic_log_locked"),
            patch("system.rest_api_budget.hub_quote_stream_fresh", return_value=False),
            patch("system.rate_limit_manager.get_rate_limit_manager") as mgr,
        ):
            mgr.return_value.check_rest_allowed.return_value = None
            mgr.return_value.is_rest_blocked.return_value = False
            for _ in range(4):
                budget.acquire(label="GET /positions")
        self.assertFalse(budget._preemptive_pause_active())

    def test_fresh_stream_clears_active_preemptive_pause(self) -> None:
        budget = RestApiBudget(min_interval_seconds=0.001, warn_per_minute=6)
        budget._preemptive_pause_until = time.time() + 30.0
        with patch("system.rest_api_budget.hub_quote_stream_fresh", return_value=True):
            budget._track_preemptive_locked(time.time())
        self.assertFalse(budget._preemptive_pause_active())
        self.assertFalse(budget._preemptive_throttle_blocks_rest())


if __name__ == "__main__":
    unittest.main()
