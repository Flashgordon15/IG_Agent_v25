"""Tests for frozen gate execution params and atomic portfolio envelope."""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from execution.types import freeze_gate_execution_params
from system.portfolio_envelope import (
    can_allocate,
    release_allocation,
    reset_portfolio_envelope_for_tests,
    snapshot,
)


class FreezeGateExecutionParamsTests(unittest.TestCase):
    def test_deep_copy_isolated_from_source_mutation(self) -> None:
        source = {
            "actual_size": 1.5,
            "stop_points": 10.0,
            "limit_points": 20.0,
            "risk_gbp": 15.0,
            "risk_band": "standard",
        }
        frozen = freeze_gate_execution_params(source)
        assert frozen is not None
        source["actual_size"] = 99.0
        self.assertAlmostEqual(frozen["actual_size"], 1.5)
        frozen["actual_size"] = 77.0
        again = freeze_gate_execution_params(source)
        assert again is not None
        self.assertAlmostEqual(again["actual_size"], 99.0)

    def test_invalid_payload_returns_none(self) -> None:
        self.assertIsNone(freeze_gate_execution_params({"actual_size": 0, "stop_points": 1}))


class PortfolioEnvelopeAtomicTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_portfolio_envelope_for_tests()

    def tearDown(self) -> None:
        reset_portfolio_envelope_for_tests()

    @patch("system.portfolio_envelope._envelope_config")
    def test_can_allocate_reserves_under_lock(self, mock_env) -> None:
        mock_env.return_value = {
            "max_concurrent_risk_gbp": 100.0,
            "max_daily_risk_deployed_gbp": 500.0,
            "min_available_gbp": 0.0,
            "account_balance_gbp": 10000.0,
            "reserve_pct": 0.0,
        }
        ok, _ = can_allocate(40.0, reserve=True)
        self.assertTrue(ok)
        snap = snapshot()
        self.assertAlmostEqual(snap["concurrent_risk_gbp"], 40.0)
        self.assertAlmostEqual(snap["daily_deployed_gbp"], 40.0)

    @patch("system.portfolio_envelope._envelope_config")
    def test_concurrent_reserve_prevents_double_allocation(self, mock_env) -> None:
        mock_env.return_value = {
            "max_concurrent_risk_gbp": 50.0,
            "max_daily_risk_deployed_gbp": 500.0,
            "min_available_gbp": 0.0,
            "account_balance_gbp": 10000.0,
            "reserve_pct": 0.0,
        }
        results: list[bool] = []

        def _worker() -> None:
            ok, _ = can_allocate(40.0, reserve=True)
            results.append(ok)

        t1 = threading.Thread(target=_worker)
        t2 = threading.Thread(target=_worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(sum(results), 1)
        snap = snapshot()
        self.assertAlmostEqual(snap["concurrent_risk_gbp"], 40.0)

    @patch("system.portfolio_envelope._envelope_config")
    def test_release_allocation_undoes_gate_reservation(self, mock_env) -> None:
        mock_env.return_value = {
            "max_concurrent_risk_gbp": 100.0,
            "max_daily_risk_deployed_gbp": 500.0,
            "min_available_gbp": 0.0,
            "account_balance_gbp": 10000.0,
            "reserve_pct": 0.0,
        }
        ok, _ = can_allocate(25.0, reserve=True)
        self.assertTrue(ok)
        release_allocation(25.0)
        snap = snapshot()
        self.assertAlmostEqual(snap["concurrent_risk_gbp"], 0.0)
        self.assertAlmostEqual(snap["daily_deployed_gbp"], 0.0)


if __name__ == "__main__":
    unittest.main()
