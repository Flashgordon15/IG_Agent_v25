"""REST API budget — preemptive throttle vs fresh stream (v24 failure register #6)."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.rest_api_budget import (
    RestApiBudget,
    RestBudgetPausedError,
    e2e_diagnostics_rest_window,
    hub_quote_stream_fresh,
    ohlc_bootstrap_rest_window,
)


class PreemptiveThrottleTests(unittest.TestCase):
    def test_preemptive_throttle_when_stream_stale_and_budget_high(self) -> None:
        budget = RestApiBudget(min_interval_seconds=0.001, warn_per_minute=6)
        with (
            patch.object(budget, "_maybe_warn_locked"),
            patch.object(budget, "_maybe_periodic_log_locked"),
            patch(
                "system.rest_api_budget.hub_quote_stream_genuinely_stale",
                return_value=True,
            ),
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
            patch(
                "system.rest_api_budget.hub_quote_stream_genuinely_stale",
                return_value=False,
            ),
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
            patch(
                "system.rest_api_budget.hub_quote_stream_genuinely_stale",
                return_value=True,
            ),
            patch("system.rate_limit_manager.get_rate_limit_manager") as mgr,
        ):
            mgr.return_value.check_rest_allowed.return_value = None
            mgr.return_value.is_rest_blocked.return_value = False
            for _ in range(4):
                budget.acquire(label="GET /positions")
        self.assertFalse(budget._preemptive_pause_active())

    def test_e2e_diagnostics_bypasses_preemptive_throttle(self) -> None:
        budget = RestApiBudget(min_interval_seconds=0.001, warn_per_minute=6)
        budget._preemptive_pause_until = time.time() + 30.0
        with (
            patch(
                "system.rest_api_budget.hub_quote_stream_genuinely_stale",
                return_value=True,
            ),
            patch("system.rate_limit_manager.get_rate_limit_manager") as mgr,
        ):
            mgr.return_value.check_rest_allowed.return_value = None
            mgr.return_value.is_rest_blocked.return_value = False
            with self.assertRaises(RestBudgetPausedError):
                budget.acquire(label="GET /markets/EPIC")
            with e2e_diagnostics_rest_window():
                budget.acquire(label="GET /markets/EPIC")

    def test_fresh_stream_clears_active_preemptive_pause(self) -> None:
        budget = RestApiBudget(min_interval_seconds=0.001, warn_per_minute=6)
        budget._preemptive_pause_until = time.time() + 30.0
        with patch(
            "system.rest_api_budget.hub_quote_stream_genuinely_stale",
            return_value=False,
        ):
            budget._track_preemptive_locked(time.time())
        self.assertFalse(budget._preemptive_pause_active())
        self.assertFalse(budget._preemptive_throttle_blocks_rest())

    def test_ohlc_bootstrap_rest_exempt_from_preemptive_budget(self) -> None:
        budget = RestApiBudget(min_interval_seconds=0.001, warn_per_minute=6)
        with (
            patch.object(budget, "_maybe_warn_locked"),
            patch.object(budget, "_maybe_periodic_log_locked"),
            patch(
                "system.rest_api_budget.hub_quote_stream_genuinely_stale",
                return_value=True,
            ),
            patch("system.rest_api_budget._hub_in_maintenance", return_value=False),
            patch("system.rate_limit_manager.get_rate_limit_manager") as mgr,
        ):
            mgr.return_value.check_rest_allowed.return_value = None
            mgr.return_value.is_rest_blocked.return_value = False
            with ohlc_bootstrap_rest_window():
                for _ in range(8):
                    budget.acquire(label="GET /prices/EPIC/MINUTE_5/100")
        self.assertFalse(budget._preemptive_pause_active())

    def test_maintenance_hub_not_treated_as_fresh(self) -> None:
        with (
            patch("system.market_data_hub.get_market_data_hub") as hub_mod,
            patch(
                "system.market_watch.japan225_session.is_quote_stream_fresh",
                return_value=False,
            ),
        ):
            hub = hub_mod.return_value
            hub.is_in_maintenance.return_value = True
            hub.get_snapshot.return_value = None
            self.assertFalse(hub_quote_stream_fresh())


class HardCapTests(unittest.TestCase):
    """Hard per-minute cap — blocks non-essential unconditionally regardless of stream state."""

    def _mgr_patch(self):
        """Return a patch for get_rate_limit_manager that is not blocked."""
        from unittest.mock import MagicMock

        mgr = MagicMock()
        mgr.check_rest_allowed.return_value = None
        mgr.is_rest_blocked.return_value = False
        return mgr

    def test_hard_cap_blocks_non_essential_when_reached(self) -> None:
        """Non-essential call at or beyond cap raises RestBudgetPausedError."""
        budget = RestApiBudget(
            min_interval_seconds=0.001, warn_per_minute=6, hard_cap_per_minute=3
        )
        with (
            patch.object(budget, "_maybe_warn_locked"),
            patch.object(budget, "_maybe_periodic_log_locked"),
            patch(
                "system.rest_api_budget.hub_quote_stream_genuinely_stale",
                return_value=False,
            ),
            patch("system.rest_api_budget._hub_in_maintenance", return_value=False),
            patch("system.rate_limit_manager.get_rate_limit_manager") as mgr_mock,
        ):
            mgr_mock.return_value = self._mgr_patch()
            # Make 3 non-essential calls (fills cap)
            for _ in range(3):
                budget.acquire(label="GET /accounts")
        # 4th non-essential must be blocked even with fresh stream
        with (
            patch(
                "system.rest_api_budget.hub_quote_stream_genuinely_stale",
                return_value=False,
            ),
            patch("system.rate_limit_manager.get_rate_limit_manager") as mgr_mock,
        ):
            mgr_mock.return_value = self._mgr_patch()
            with self.assertRaises(RestBudgetPausedError) as ctx:
                budget.acquire(label="GET /accounts")
        self.assertIn("hard_rate_cap", str(ctx.exception))

    def test_hard_cap_allows_essential_through_regardless(self) -> None:
        """Positions/orders calls always pass even when cap is exceeded."""
        budget = RestApiBudget(
            min_interval_seconds=0.001, warn_per_minute=6, hard_cap_per_minute=1
        )
        with (
            patch.object(budget, "_maybe_warn_locked"),
            patch.object(budget, "_maybe_periodic_log_locked"),
            patch(
                "system.rest_api_budget.hub_quote_stream_genuinely_stale",
                return_value=False,
            ),
            patch("system.rest_api_budget._hub_in_maintenance", return_value=False),
            patch("system.rate_limit_manager.get_rate_limit_manager") as mgr_mock,
        ):
            mgr_mock.return_value = self._mgr_patch()
            # One non-essential call fills the cap
            budget.acquire(label="GET /accounts")
            # Essential calls still go through
            budget.acquire(label="POST /positions/otc")
            budget.acquire(label="GET /confirms/DEAL123")

    def test_hard_cap_applies_with_fresh_stream(self) -> None:
        """Cap fires even when Lightstreamer is fully healthy — this is the key gap fixed."""
        budget = RestApiBudget(
            min_interval_seconds=0.001, warn_per_minute=6, hard_cap_per_minute=2
        )
        with (
            patch.object(budget, "_maybe_warn_locked"),
            patch.object(budget, "_maybe_periodic_log_locked"),
            patch(
                "system.rest_api_budget.hub_quote_stream_genuinely_stale",
                return_value=False,
            ),
            patch("system.rest_api_budget._hub_in_maintenance", return_value=False),
            patch("system.rate_limit_manager.get_rate_limit_manager") as mgr_mock,
        ):
            mgr_mock.return_value = self._mgr_patch()
            budget.acquire(label="GET /markets/EPIC")
            budget.acquire(label="GET /accounts")
            with self.assertRaises(RestBudgetPausedError):
                budget.acquire(label="GET /history/transactions")

    def test_ohlc_bootstrap_exempt_from_hard_cap(self) -> None:
        """ohlc_bootstrap calls don't count toward the hard cap."""
        budget = RestApiBudget(
            min_interval_seconds=0.001, warn_per_minute=6, hard_cap_per_minute=2
        )
        with (
            patch.object(budget, "_maybe_warn_locked"),
            patch.object(budget, "_maybe_periodic_log_locked"),
            patch(
                "system.rest_api_budget.hub_quote_stream_genuinely_stale",
                return_value=False,
            ),
            patch("system.rest_api_budget._hub_in_maintenance", return_value=False),
            patch("system.rate_limit_manager.get_rate_limit_manager") as mgr_mock,
        ):
            mgr_mock.return_value = self._mgr_patch()
            # 5 OHLC bootstrap calls — must not count toward cap
            with ohlc_bootstrap_rest_window():
                for _ in range(5):
                    budget.acquire(label="GET /prices/EPIC/MINUTE_5/100")
            # Non-essential call still allowed (cap not consumed by ohlc)
            budget.acquire(label="GET /accounts")

    def test_e2e_diagnostics_bypasses_hard_cap(self) -> None:
        """E2E diagnostic window bypasses the hard cap."""
        budget = RestApiBudget(
            min_interval_seconds=0.001, warn_per_minute=6, hard_cap_per_minute=1
        )
        with (
            patch.object(budget, "_maybe_warn_locked"),
            patch.object(budget, "_maybe_periodic_log_locked"),
            patch(
                "system.rest_api_budget.hub_quote_stream_genuinely_stale",
                return_value=False,
            ),
            patch("system.rest_api_budget._hub_in_maintenance", return_value=False),
            patch("system.rate_limit_manager.get_rate_limit_manager") as mgr_mock,
        ):
            mgr_mock.return_value = self._mgr_patch()
            budget.acquire(label="GET /accounts")  # fills cap
            with e2e_diagnostics_rest_window():
                budget.acquire(label="GET /markets/EPIC")  # must not raise

    def test_metrics_exposes_hard_cap_fields(self) -> None:
        """metrics() includes hard_cap_per_minute and utilization."""
        budget = RestApiBudget(
            min_interval_seconds=0.001, warn_per_minute=6, hard_cap_per_minute=4
        )
        m = budget.metrics()
        self.assertIn("hard_cap_per_minute", m)
        self.assertIn("hard_cap_calls_last_minute", m)
        self.assertIn("hard_cap_utilization_pct", m)
        self.assertEqual(m["hard_cap_per_minute"], 4)
        self.assertEqual(m["hard_cap_calls_last_minute"], 0)
        self.assertEqual(m["hard_cap_utilization_pct"], 0)


class PriorityBypassTests(unittest.TestCase):
    """priority=True skips min_interval wait and hard cap (confirm_deal critical path)."""

    def _mgr_patch(self):
        from unittest.mock import MagicMock

        mgr = MagicMock()
        mgr.check_rest_allowed.return_value = None
        mgr.is_rest_blocked.return_value = False
        return mgr

    def test_priority_bypasses_min_interval_wait(self) -> None:
        """priority=True must not block on min_interval even when slot was just taken."""
        budget = RestApiBudget(
            min_interval_seconds=60.0, warn_per_minute=6, hard_cap_per_minute=3
        )
        with (
            patch.object(budget, "_maybe_warn_locked"),
            patch.object(budget, "_maybe_periodic_log_locked"),
            patch(
                "system.rest_api_budget.hub_quote_stream_genuinely_stale",
                return_value=False,
            ),
            patch("system.rest_api_budget._hub_in_maintenance", return_value=False),
            patch("system.rate_limit_manager.get_rate_limit_manager") as mgr_mock,
        ):
            mgr_mock.return_value = self._mgr_patch()
            # Consume the slot — next non-priority call would wait 60 s
            budget.acquire(label="GET /accounts")
            # priority=True must proceed immediately without sleeping
            t0 = time.time()
            budget.acquire(label="GET /confirms/DEAL123", priority=True)
            elapsed = time.time() - t0
        self.assertLess(elapsed, 1.0, "priority bypass must not wait for min_interval")

    def test_priority_bypasses_hard_cap(self) -> None:
        """priority=True must pass even when the hard cap is fully consumed."""
        budget = RestApiBudget(
            min_interval_seconds=0.001, warn_per_minute=6, hard_cap_per_minute=2
        )
        with (
            patch.object(budget, "_maybe_warn_locked"),
            patch.object(budget, "_maybe_periodic_log_locked"),
            patch(
                "system.rest_api_budget.hub_quote_stream_genuinely_stale",
                return_value=False,
            ),
            patch("system.rest_api_budget._hub_in_maintenance", return_value=False),
            patch("system.rate_limit_manager.get_rate_limit_manager") as mgr_mock,
        ):
            mgr_mock.return_value = self._mgr_patch()
            # Fill cap with non-essential calls
            budget.acquire(label="GET /accounts")
            budget.acquire(label="GET /history/transactions")
            # priority call (confirm_deal) must not raise
            budget.acquire(label="GET /confirms/DEAL123", priority=True)

    def test_non_priority_still_blocked_by_hard_cap(self) -> None:
        """Sanity: non-priority calls are still blocked when cap is exhausted."""
        budget = RestApiBudget(
            min_interval_seconds=0.001, warn_per_minute=6, hard_cap_per_minute=2
        )
        with (
            patch.object(budget, "_maybe_warn_locked"),
            patch.object(budget, "_maybe_periodic_log_locked"),
            patch(
                "system.rest_api_budget.hub_quote_stream_genuinely_stale",
                return_value=False,
            ),
            patch("system.rest_api_budget._hub_in_maintenance", return_value=False),
            patch("system.rate_limit_manager.get_rate_limit_manager") as mgr_mock,
        ):
            mgr_mock.return_value = self._mgr_patch()
            budget.acquire(label="GET /accounts")
            budget.acquire(label="GET /history/transactions")
            with self.assertRaises(RestBudgetPausedError):
                budget.acquire(label="GET /accounts", priority=False)


if __name__ == "__main__":
    unittest.main()
