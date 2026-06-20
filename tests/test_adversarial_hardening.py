"""Adversarial v30 monolith hardening — chaos, fuzz, and cross-profile firewall."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np


class PillarParameterMatrixTests(unittest.TestCase):
    def test_frozen_risk_constants(self) -> None:
        from apex.hardening import (
            BASELINE_EQUITY_GBP,
            ML_VETO_FLOOR,
            NETWORK_HEARTBEAT_INTERVAL_SEC,
            PER_ASSET_RISK_CAP_GBP,
            PORTFOLIO_RISK_CEILING_GBP,
        )
        from signals.indicators import ML_VETO_FLOOR as IND_FLOOR

        self.assertEqual(BASELINE_EQUITY_GBP, 10_000.0)
        self.assertEqual(PER_ASSET_RISK_CAP_GBP, 350.0)
        self.assertEqual(PORTFOLIO_RISK_CEILING_GBP, 750.0)
        self.assertAlmostEqual(ML_VETO_FLOOR, 0.450)
        self.assertAlmostEqual(IND_FLOOR, 0.450)
        self.assertEqual(NETWORK_HEARTBEAT_INTERVAL_SEC, 3.0)

    def test_network_heartbeat_cadence_in_streaming_client(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "src" / "ig_api" / "streaming_client.py"
        ).read_text(encoding="utf-8")
        self.assertIn("3s Cloudflare/IG alternation", source)


class NetworkDegradationChaosTests(unittest.TestCase):
    def tearDown(self) -> None:
        from apex.hardening import reset_hardening_for_tests
        from apex.microkernel import reset_microkernel_for_tests

        reset_hardening_for_tests()
        reset_microkernel_for_tests()

    def test_execution_freeze_blocks_worker_c_and_rest(self) -> None:
        from apex.hardening import is_execution_frozen, reset_hardening_for_tests, set_network_degraded
        from apex.microkernel import get_microkernel, reset_microkernel_for_tests

        reset_hardening_for_tests()
        reset_microkernel_for_tests()
        set_network_degraded(True, source="chaos_broadband_crash")

        kernel = get_microkernel()
        verdict = kernel.publish_risk_context(
            epic="CS.D.EURUSD.CFD.IP",
            size=1.0,
            stop_pts=10.0,
            spread_pts=1.0,
            point_value_gbp=1.0,
            concurrent_risk_gbp=0.0,
            ml_pass=True,
        )
        self.assertTrue(is_execution_frozen())
        self.assertFalse(verdict.allowed)

        os.environ["NODE_ENV"] = "shadow"
        from system.node_profile import reset_node_profile_for_tests

        reset_node_profile_for_tests()
        client = __import__("ig_api.rest_client", fromlist=["IGRestClient"]).IGRestClient(
            MagicMock()
        )
        with patch.object(client, "ensure_session", MagicMock()):
            result = client.place_market_order(
                epic="CS.D.EURUSD.CFD.IP",
                direction="BUY",
                size=1.0,
                stop_distance=10.0,
            )
        self.assertEqual(result.get("status"), "EXECUTION_FROZEN")


class ProfileCrossTalkFirewallTests(unittest.TestCase):
    def tearDown(self) -> None:
        from system.node_profile import reset_node_profile_for_tests

        reset_node_profile_for_tests()
        os.environ.pop("NODE_ENV", None)
        os.environ.pop("IG_TRIAGE_DB", None)

    def test_production_order_payload_trapped_as_mock_shadow(self) -> None:
        """Simulated production-position ticket must not reach IG REST on shadow node."""
        os.environ["NODE_ENV"] = "shadow"
        from system.node_profile import reset_node_profile_for_tests

        reset_node_profile_for_tests()

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "triage_v30.db"
            os.environ["IG_TRIAGE_DB"] = str(db_path)
            client = __import__("ig_api.rest_client", fromlist=["IGRestClient"]).IGRestClient(
                MagicMock()
            )
            client.ensure_session = MagicMock(side_effect=AssertionError("live REST forbidden"))
            result = client.place_market_order(
                epic="CS.D.EURUSD.CFD.IP",
                direction="BUY",
                size=2.0,
                stop_distance=15.0,
            )
            self.assertEqual(result["status"], "MOCK_SHADOW_ENTRY")
            self.assertTrue(result.get("shadow"))
            client.ensure_session.assert_not_called()


class FloatFuzzMutationTests(unittest.TestCase):
    def test_contract_size_fuzz_vectors(self) -> None:
        from apex.hardening import floor_contract_size, under_min_lot_detail

        cases = [
            (0.00047, 0, True),
            (-1.5, 0, True),
            (99999.9, 99999, False),
            (1.9, 1, False),
            (0.999, 0, True),
            (float("nan"), 0, True),
        ]
        for raw, expect_int, expect_under in cases:
            size_int, under = floor_contract_size(raw)
            self.assertEqual(size_int, expect_int, msg=f"raw={raw}")
            self.assertEqual(under, expect_under, msg=f"raw={raw}")
            if under:
                self.assertIn("UNDER_MIN_LOT", under_min_lot_detail(size_int))


class IngestionRaceAssaultTests(unittest.TestCase):
    def tearDown(self) -> None:
        from apex.microkernel import reset_microkernel_for_tests

        reset_microkernel_for_tests()

    def test_flood_5000_ticks_without_deadlock(self) -> None:
        from apex.microkernel import get_microkernel, reset_microkernel_for_tests
        from analytics.triage_logger import reset_triage_logger_for_tests

        triage_tmp = tempfile.mkdtemp(prefix="adv_flood_triage_")
        self.addCleanup(lambda: __import__("shutil").rmtree(triage_tmp, ignore_errors=True))
        os.environ["IG_TRIAGE_DB"] = str(Path(triage_tmp) / "triage_v30.db")
        os.environ["IG_MULTI_API_BROKER"] = "0"
        reset_triage_logger_for_tests()
        reset_microkernel_for_tests()
        from apex.warmup_progress import mark_warmup_ready

        mark_warmup_ready()
        kernel = get_microkernel()
        kernel.start()

        quote = MagicMock(bid=1.1000, offer=1.1002)
        epic = "CS.D.EURUSD.CFD.IP"
        errors: list[BaseException] = []

        def _flood() -> None:
            try:
                for _ in range(5000):
                    kernel.on_tick_ingest(epic, quote)
            except BaseException as exc:
                errors.append(exc)

        t0 = time.perf_counter()
        threads = [threading.Thread(target=_flood) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)
            self.assertFalse(t.is_alive(), "worker thread deadlock detected")
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self.assertLess(elapsed_ms, 30_000.0)
        self.assertEqual(errors, [])

        ring = kernel._ring_for(epic)
        close, _, _ = ring.ordered_views()
        self.assertLessEqual(len(close), 256)
        self.assertEqual(close.dtype, np.float64)
        stats = kernel.stats()
        self.assertGreaterEqual(stats["ingested"], 1000)
        kernel.stop()


class AgentBootstrapOsShadowTests(unittest.TestCase):
    def test_transaction_sync_module_imports_os_at_top_level(self) -> None:
        """Regression: txn sync must not hit UnboundLocalError when boot_mode=True."""
        source = (
            Path(__file__).resolve().parents[1] / "src" / "runtime" / "agent_bootstrap.py"
        ).read_text(encoding="utf-8")
        header = source.split("def build_market_orchestrator", 1)[0]
        self.assertIn("import os", header)


class MicroTrendAlphaTests(unittest.TestCase):
    def test_ml_veto_floor_hard_locked(self) -> None:
        from signals.indicators import resolve_ml_veto_floor

        self.assertAlmostEqual(resolve_ml_veto_floor(), 0.450)
        self.assertAlmostEqual(resolve_ml_veto_floor(epic="CS.D.EURUSD.CFD.IP"), 0.450)

    def test_micro_trend_promotes_on_momentum_shift(self) -> None:
        from signals.indicators import (
            STRATEGY_THRESHOLD_LOW_PCT,
            evaluate_micro_trend_alpha,
        )

        n = 64
        close = np.full(n, 100.0, dtype=np.float64)
        close[-8:] = np.linspace(100.0, 101.6, 8, dtype=np.float64)
        result = evaluate_micro_trend_alpha(close)
        self.assertGreaterEqual(float(result["score_pct"]), STRATEGY_THRESHOLD_LOW_PCT)
        self.assertTrue(result["promote"])
        self.assertIn(result["direction"], ("BUY", "SELL"))


class ElectronBootConstantTests(unittest.TestCase):
    def test_sidecar_ready_timeout_ninety_seconds(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "main.js").read_text(encoding="utf-8")
        self.assertIn("SIDECAR_READY_TIMEOUT_MS = 90000", source)
        self.assertIn("const IPC_RETRY_MS = 2000", source)
        self.assertIn("apex_ipc.sock", source)


if __name__ == "__main__":
    unittest.main()
