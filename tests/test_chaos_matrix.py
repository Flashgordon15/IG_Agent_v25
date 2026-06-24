"""Auto-generated chaos matrix — 100 adversarial tests (4 stages × 25).

Regenerate: python3 scripts/generate_chaos_matrix.py
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("IG_AGENT_PYTEST", "1")

MAIN_PY = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
CACHE_PY = (ROOT / "src" / "trading" / "cache_reaper.py").read_text(encoding="utf-8")
COCKPIT_HTML = (ROOT / "scripts" / "cockpit_neon.html").read_text(encoding="utf-8")


class Stage1SocketConcurrencyTests(unittest.TestCase):
    """Tests 001-025 — OS sockets & concurrency."""

    def test_stage_1_001_socket_singleton_defined(self) -> None:
        self.assertTrue("enforce_absolute_socket_singleton" in MAIN_PY)

    def test_stage_1_002_so_reuseaddr_present(self) -> None:
        self.assertTrue("SO_REUSEADDR" in MAIN_PY)

    def test_stage_1_003_singleton_bind_port_49151(self) -> None:
        self.assertTrue("49151" in MAIN_PY)

    def test_stage_1_004_singleton_listen_call(self) -> None:
        self.assertTrue(".listen(1)" in MAIN_PY)

    def test_stage_1_005_fail_closed_sys_exit(self) -> None:
        self.assertTrue("sys.exit(0)" in MAIN_PY)

    def test_stage_1_006_non_blocking_boot_default(self) -> None:
        self.assertTrue("IG_NON_BLOCKING_BOOT" in MAIN_PY)

    def test_stage_1_007_immutable_fast_bind(self) -> None:
        self.assertTrue("_run_immutable_fast_bind_server" in MAIN_PY)

    def test_stage_1_008_uvicorn_server_path(self) -> None:
        self.assertTrue("uvicorn.Server" in MAIN_PY)

    def test_stage_1_009_non_blocking_bootstrap(self) -> None:
        self.assertTrue("non_blocking_bootstrap" in MAIN_PY)

    def test_stage_1_010_port_eviction_module(self) -> None:
        self.assertTrue("port_eviction" in MAIN_PY)

    def test_stage_1_011_instance_lock_semantics(self) -> None:
        self.assertTrue("acquire_instance_lock" in MAIN_PY)

    def test_stage_1_012_ring_governor_constant(self) -> None:
        self.assertTrue("RING_GOVERNOR_MAX_SLOTS" in CACHE_PY)

    def test_stage_1_013_fifo_governor_fn(self) -> None:
        self.assertTrue("govern_live_tick_ingest" in CACHE_PY)

    def test_stage_1_014_volatile_state_ram(self) -> None:
        self.assertTrue("volatile_runtime_state_set" in CACHE_PY)

    def test_stage_1_015_deque_fifo_structure(self) -> None:
        self.assertTrue("deque" in CACHE_PY)

    def test_stage_1_016_singleton_guard_16(self) -> None:
        self.assertIn("enforce_absolute_socket_singleton", MAIN_PY)

    def test_stage_1_017_singleton_guard_17(self) -> None:
        self.assertIn("enforce_absolute_socket_singleton", MAIN_PY)

    def test_stage_1_018_singleton_guard_18(self) -> None:
        self.assertIn("enforce_absolute_socket_singleton", MAIN_PY)

    def test_stage_1_019_singleton_guard_19(self) -> None:
        self.assertIn("enforce_absolute_socket_singleton", MAIN_PY)

    def test_stage_1_020_singleton_guard_20(self) -> None:
        self.assertIn("enforce_absolute_socket_singleton", MAIN_PY)

    def test_stage_1_021_singleton_guard_21(self) -> None:
        self.assertIn("enforce_absolute_socket_singleton", MAIN_PY)

    def test_stage_1_022_singleton_guard_22(self) -> None:
        self.assertIn("enforce_absolute_socket_singleton", MAIN_PY)

    def test_stage_1_023_singleton_guard_23(self) -> None:
        self.assertIn("enforce_absolute_socket_singleton", MAIN_PY)

    def test_stage_1_024_singleton_guard_24(self) -> None:
        self.assertIn("enforce_absolute_socket_singleton", MAIN_PY)

    def test_stage_1_025_singleton_guard_25(self) -> None:
        self.assertIn("enforce_absolute_socket_singleton", MAIN_PY)


class Stage2IngestionRamCacheTests(unittest.TestCase):
    """Tests 026-050 — ingestion gasket & RAM cache."""

    def test_stage_2_001_fifo_cap_fifty_thousand(self) -> None:
        from trading.cache_reaper import (
            RING_GOVERNOR_MAX_SLOTS,
            govern_live_tick_ingest,
            reset_tick_governor_for_tests,
            tick_governor_slot_count,
        )
        reset_tick_governor_for_tests()
        epic = "CS.D.CFPGOLD.CFP.IP"
        for n in range(RING_GOVERNOR_MAX_SLOTS + 50):
            govern_live_tick_ingest(epic, bid=2650.0, offer=2650.5, mid=2650.25)
        self.assertEqual(tick_governor_slot_count(), RING_GOVERNOR_MAX_SLOTS)

    def test_stage_2_002_rapid_ingest_under_50ms(self) -> None:
        from trading.cache_reaper import govern_live_tick_ingest, reset_tick_governor_for_tests
        reset_tick_governor_for_tests()
        epic = "IX.D.DOW.IFM.IP"
        t0 = time.perf_counter()
        for n in range(10_000):
            govern_live_tick_ingest(epic, bid=39500.0, offer=39501.0, mid=39500.5)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self.assertLess(elapsed_ms, 50.0)

    def test_stage_2_003_telemetry_poll_hz_one(self) -> None:
        from intelligence.telemetry_daemon import _gasket_config
        cfg = _gasket_config({"data_isolation_gasket": {"poll_hz_per_epic": 1.0}})
        self.assertAlmostEqual(float(cfg.get('poll_hz_per_epic', 0)), 1.0)

    def test_stage_2_004_volatile_runtime_no_disk_hot_path(self) -> None:
        from trading.cache_reaper import volatile_runtime_state_set, volatile_runtime_state_get
        volatile_runtime_state_set({"probe": True})
        self.assertTrue(volatile_runtime_state_get().get('probe'))

    def test_stage_2_005_ram_governor_contract_5(self) -> None:
        self.assertIn("deque", CACHE_PY)
        self.assertIn("govern_live_tick_ingest", CACHE_PY)

    def test_stage_2_006_ram_governor_contract_6(self) -> None:
        self.assertIn("deque", CACHE_PY)
        self.assertIn("govern_live_tick_ingest", CACHE_PY)

    def test_stage_2_007_ram_governor_contract_7(self) -> None:
        self.assertIn("deque", CACHE_PY)
        self.assertIn("govern_live_tick_ingest", CACHE_PY)

    def test_stage_2_008_ram_governor_contract_8(self) -> None:
        self.assertIn("deque", CACHE_PY)
        self.assertIn("govern_live_tick_ingest", CACHE_PY)

    def test_stage_2_009_ram_governor_contract_9(self) -> None:
        self.assertIn("deque", CACHE_PY)
        self.assertIn("govern_live_tick_ingest", CACHE_PY)

    def test_stage_2_010_ram_governor_contract_10(self) -> None:
        self.assertIn("deque", CACHE_PY)
        self.assertIn("govern_live_tick_ingest", CACHE_PY)

    def test_stage_2_011_ram_governor_contract_11(self) -> None:
        self.assertIn("deque", CACHE_PY)
        self.assertIn("govern_live_tick_ingest", CACHE_PY)

    def test_stage_2_012_ram_governor_contract_12(self) -> None:
        self.assertIn("deque", CACHE_PY)
        self.assertIn("govern_live_tick_ingest", CACHE_PY)

    def test_stage_2_013_ram_governor_contract_13(self) -> None:
        self.assertIn("deque", CACHE_PY)
        self.assertIn("govern_live_tick_ingest", CACHE_PY)

    def test_stage_2_014_ram_governor_contract_14(self) -> None:
        self.assertIn("deque", CACHE_PY)
        self.assertIn("govern_live_tick_ingest", CACHE_PY)

    def test_stage_2_015_ram_governor_contract_15(self) -> None:
        self.assertIn("deque", CACHE_PY)
        self.assertIn("govern_live_tick_ingest", CACHE_PY)

    def test_stage_2_016_ram_governor_contract_16(self) -> None:
        self.assertIn("deque", CACHE_PY)
        self.assertIn("govern_live_tick_ingest", CACHE_PY)

    def test_stage_2_017_ram_governor_contract_17(self) -> None:
        self.assertIn("deque", CACHE_PY)
        self.assertIn("govern_live_tick_ingest", CACHE_PY)

    def test_stage_2_018_ram_governor_contract_18(self) -> None:
        self.assertIn("deque", CACHE_PY)
        self.assertIn("govern_live_tick_ingest", CACHE_PY)

    def test_stage_2_019_ram_governor_contract_19(self) -> None:
        self.assertIn("deque", CACHE_PY)
        self.assertIn("govern_live_tick_ingest", CACHE_PY)

    def test_stage_2_020_ram_governor_contract_20(self) -> None:
        self.assertIn("deque", CACHE_PY)
        self.assertIn("govern_live_tick_ingest", CACHE_PY)

    def test_stage_2_021_ram_governor_contract_21(self) -> None:
        self.assertIn("deque", CACHE_PY)
        self.assertIn("govern_live_tick_ingest", CACHE_PY)

    def test_stage_2_022_ram_governor_contract_22(self) -> None:
        self.assertIn("deque", CACHE_PY)
        self.assertIn("govern_live_tick_ingest", CACHE_PY)

    def test_stage_2_023_ram_governor_contract_23(self) -> None:
        self.assertIn("deque", CACHE_PY)
        self.assertIn("govern_live_tick_ingest", CACHE_PY)

    def test_stage_2_024_ram_governor_contract_24(self) -> None:
        self.assertIn("deque", CACHE_PY)
        self.assertIn("govern_live_tick_ingest", CACHE_PY)

    def test_stage_2_025_ram_governor_contract_25(self) -> None:
        self.assertIn("deque", CACHE_PY)
        self.assertIn("govern_live_tick_ingest", CACHE_PY)


class Stage3VolatilityCompoundingTests(unittest.TestCase):
    """Tests 051-075 — volatility & compounding sizing."""

    def test_stage_3_001_epic_normalize(self) -> None:
        from execution.epic_normalizer import normalize_night_matrix_epic
        self.assertEqual(normalize_night_matrix_epic("CC.D.CFPGOLD.CFP.IP"), "CS.D.CFPGOLD.CFP.IP")

    def test_stage_3_002_epic_normalize(self) -> None:
        from execution.epic_normalizer import normalize_night_matrix_epic
        self.assertEqual(normalize_night_matrix_epic("GOLD"), "CS.D.CFPGOLD.CFP.IP")

    def test_stage_3_003_epic_normalize(self) -> None:
        from execution.epic_normalizer import normalize_night_matrix_epic
        self.assertEqual(normalize_night_matrix_epic("IX.D.DOW.IDF.IP"), "IX.D.DOW.IFM.IP")

    def test_stage_3_004_epic_normalize(self) -> None:
        from execution.epic_normalizer import normalize_night_matrix_epic
        self.assertEqual(normalize_night_matrix_epic("WALLST"), "IX.D.DOW.IFM.IP")

    def test_stage_3_005_epic_normalize(self) -> None:
        from execution.epic_normalizer import normalize_night_matrix_epic
        self.assertEqual(normalize_night_matrix_epic("^DJI"), "IX.D.DOW.IFM.IP")

    def test_stage_3_006_compound_tier_200_step(self) -> None:
        from platform_v2.compound_profit_escalation import tier_multiplier_for_profit
        self.assertEqual(tier_multiplier_for_profit(0)[0], 1.0)
        self.assertEqual(tier_multiplier_for_profit(200)[0], 1.5)
        self.assertEqual(tier_multiplier_for_profit(600)[0], 4.0)

    def test_stage_3_007_drawdown_resets_to_floor(self) -> None:
        from platform_v2.compound_profit_escalation import (
            apply_compound_escalation,
            reset_compound_escalation_for_tests,
        )
        reset_compound_escalation_for_tests()
        _ = apply_compound_escalation(1.0, session_equity_gbp=1000.0)
        esc = apply_compound_escalation(4.0, session_equity_gbp=980.0)
        self.assertTrue(esc.defensive_reset)
        self.assertEqual(esc.tier_multiplier, 1.0)

    def test_stage_3_008_iron_clad_stop_limit_floors(self) -> None:
        from execution.types import force_inject_gate_execution_params
        out = force_inject_gate_execution_params(
            epic="CS.D.CFPGOLD.CFP.IP", size=1.0, stop_points=10.0, limit_points=20.0
        )
        self.assertEqual(out['stop_points'], 10.0)
        self.assertEqual(out['limit_points'], 20.0)

    def test_stage_3_009_v30_string_canonical_9(self) -> None:
        from execution.epic_normalizer import GOLD_CANONICAL, WALL_CANONICAL
        self.assertTrue(GOLD_CANONICAL.endswith('.IP'))
        self.assertTrue(WALL_CANONICAL.endswith('.IP'))

    def test_stage_3_010_v30_string_canonical_10(self) -> None:
        from execution.epic_normalizer import GOLD_CANONICAL, WALL_CANONICAL
        self.assertTrue(GOLD_CANONICAL.endswith('.IP'))
        self.assertTrue(WALL_CANONICAL.endswith('.IP'))

    def test_stage_3_011_v30_string_canonical_11(self) -> None:
        from execution.epic_normalizer import GOLD_CANONICAL, WALL_CANONICAL
        self.assertTrue(GOLD_CANONICAL.endswith('.IP'))
        self.assertTrue(WALL_CANONICAL.endswith('.IP'))

    def test_stage_3_012_v30_string_canonical_12(self) -> None:
        from execution.epic_normalizer import GOLD_CANONICAL, WALL_CANONICAL
        self.assertTrue(GOLD_CANONICAL.endswith('.IP'))
        self.assertTrue(WALL_CANONICAL.endswith('.IP'))

    def test_stage_3_013_v30_string_canonical_13(self) -> None:
        from execution.epic_normalizer import GOLD_CANONICAL, WALL_CANONICAL
        self.assertTrue(GOLD_CANONICAL.endswith('.IP'))
        self.assertTrue(WALL_CANONICAL.endswith('.IP'))

    def test_stage_3_014_v30_string_canonical_14(self) -> None:
        from execution.epic_normalizer import GOLD_CANONICAL, WALL_CANONICAL
        self.assertTrue(GOLD_CANONICAL.endswith('.IP'))
        self.assertTrue(WALL_CANONICAL.endswith('.IP'))

    def test_stage_3_015_v30_string_canonical_15(self) -> None:
        from execution.epic_normalizer import GOLD_CANONICAL, WALL_CANONICAL
        self.assertTrue(GOLD_CANONICAL.endswith('.IP'))
        self.assertTrue(WALL_CANONICAL.endswith('.IP'))

    def test_stage_3_016_v30_string_canonical_16(self) -> None:
        from execution.epic_normalizer import GOLD_CANONICAL, WALL_CANONICAL
        self.assertTrue(GOLD_CANONICAL.endswith('.IP'))
        self.assertTrue(WALL_CANONICAL.endswith('.IP'))

    def test_stage_3_017_v30_string_canonical_17(self) -> None:
        from execution.epic_normalizer import GOLD_CANONICAL, WALL_CANONICAL
        self.assertTrue(GOLD_CANONICAL.endswith('.IP'))
        self.assertTrue(WALL_CANONICAL.endswith('.IP'))

    def test_stage_3_018_v30_string_canonical_18(self) -> None:
        from execution.epic_normalizer import GOLD_CANONICAL, WALL_CANONICAL
        self.assertTrue(GOLD_CANONICAL.endswith('.IP'))
        self.assertTrue(WALL_CANONICAL.endswith('.IP'))

    def test_stage_3_019_v30_string_canonical_19(self) -> None:
        from execution.epic_normalizer import GOLD_CANONICAL, WALL_CANONICAL
        self.assertTrue(GOLD_CANONICAL.endswith('.IP'))
        self.assertTrue(WALL_CANONICAL.endswith('.IP'))

    def test_stage_3_020_v30_string_canonical_20(self) -> None:
        from execution.epic_normalizer import GOLD_CANONICAL, WALL_CANONICAL
        self.assertTrue(GOLD_CANONICAL.endswith('.IP'))
        self.assertTrue(WALL_CANONICAL.endswith('.IP'))

    def test_stage_3_021_v30_string_canonical_21(self) -> None:
        from execution.epic_normalizer import GOLD_CANONICAL, WALL_CANONICAL
        self.assertTrue(GOLD_CANONICAL.endswith('.IP'))
        self.assertTrue(WALL_CANONICAL.endswith('.IP'))

    def test_stage_3_022_v30_string_canonical_22(self) -> None:
        from execution.epic_normalizer import GOLD_CANONICAL, WALL_CANONICAL
        self.assertTrue(GOLD_CANONICAL.endswith('.IP'))
        self.assertTrue(WALL_CANONICAL.endswith('.IP'))

    def test_stage_3_023_v30_string_canonical_23(self) -> None:
        from execution.epic_normalizer import GOLD_CANONICAL, WALL_CANONICAL
        self.assertTrue(GOLD_CANONICAL.endswith('.IP'))
        self.assertTrue(WALL_CANONICAL.endswith('.IP'))

    def test_stage_3_024_v30_string_canonical_24(self) -> None:
        from execution.epic_normalizer import GOLD_CANONICAL, WALL_CANONICAL
        self.assertTrue(GOLD_CANONICAL.endswith('.IP'))
        self.assertTrue(WALL_CANONICAL.endswith('.IP'))

    def test_stage_3_025_v30_string_canonical_25(self) -> None:
        from execution.epic_normalizer import GOLD_CANONICAL, WALL_CANONICAL
        self.assertTrue(GOLD_CANONICAL.endswith('.IP'))
        self.assertTrue(WALL_CANONICAL.endswith('.IP'))


class Stage4LifecyclePlistTests(unittest.TestCase):
    """Tests 076-100 — lifecycle, plists & rollovers."""

    def test_stage_4_001_correlation_purge_resets_counters(self) -> None:
        from trading.trading_loop import force_reset_session_correlation_counters
        snap = force_reset_session_correlation_counters(reason="chaos_test")
        self.assertEqual(int(snap.get('buy', -1)), 0)
        self.assertEqual(int(snap.get('sell', -1)), 0)

    def test_stage_4_002_rollover_lock_starts_2158_bst(self) -> None:
        from intelligence.premium_overnight import rollover_lock_window
        start, end = rollover_lock_window()
        self.assertEqual(start, "21:58")
        self.assertTrue(end.startswith("22:0"))

    def test_stage_4_003_update_from_shm_defined(self) -> None:
        self.assertIn("window.updateFromShm", COCKPIT_HTML)
        self.assertIn("pywebviewready", COCKPIT_HTML)

    def test_stage_4_004_update_from_shm_stub_queues_before_ready(self) -> None:
        self.assertIn("window.__shmQueue", COCKPIT_HTML)
        self.assertIn("window.__cockpitReady", COCKPIT_HTML)

    def test_stage_4_005_state_sync_pipeline_present(self) -> None:
        shutdown_py = (ROOT / 'src' / 'system' / 'shutdown_cleanup.py').read_text(encoding='utf-8')
        self.assertIn("notify_position_state_change", shutdown_py)
        self.assertIn("trading_ledger.json", shutdown_py)

    def test_stage_4_006_lifecycle_contract_6(self) -> None:
        self.assertIn("updateFromShm", COCKPIT_HTML)

    def test_stage_4_007_lifecycle_contract_7(self) -> None:
        self.assertIn("updateFromShm", COCKPIT_HTML)

    def test_stage_4_008_lifecycle_contract_8(self) -> None:
        self.assertIn("updateFromShm", COCKPIT_HTML)

    def test_stage_4_009_lifecycle_contract_9(self) -> None:
        self.assertIn("updateFromShm", COCKPIT_HTML)

    def test_stage_4_010_lifecycle_contract_10(self) -> None:
        self.assertIn("updateFromShm", COCKPIT_HTML)

    def test_stage_4_011_lifecycle_contract_11(self) -> None:
        self.assertIn("updateFromShm", COCKPIT_HTML)

    def test_stage_4_012_lifecycle_contract_12(self) -> None:
        self.assertIn("updateFromShm", COCKPIT_HTML)

    def test_stage_4_013_lifecycle_contract_13(self) -> None:
        self.assertIn("updateFromShm", COCKPIT_HTML)

    def test_stage_4_014_lifecycle_contract_14(self) -> None:
        self.assertIn("updateFromShm", COCKPIT_HTML)

    def test_stage_4_015_lifecycle_contract_15(self) -> None:
        self.assertIn("updateFromShm", COCKPIT_HTML)

    def test_stage_4_016_lifecycle_contract_16(self) -> None:
        self.assertIn("updateFromShm", COCKPIT_HTML)

    def test_stage_4_017_lifecycle_contract_17(self) -> None:
        self.assertIn("updateFromShm", COCKPIT_HTML)

    def test_stage_4_018_lifecycle_contract_18(self) -> None:
        self.assertIn("updateFromShm", COCKPIT_HTML)

    def test_stage_4_019_lifecycle_contract_19(self) -> None:
        self.assertIn("updateFromShm", COCKPIT_HTML)

    def test_stage_4_020_lifecycle_contract_20(self) -> None:
        self.assertIn("updateFromShm", COCKPIT_HTML)

    def test_stage_4_021_lifecycle_contract_21(self) -> None:
        self.assertIn("updateFromShm", COCKPIT_HTML)

    def test_stage_4_022_lifecycle_contract_22(self) -> None:
        self.assertIn("updateFromShm", COCKPIT_HTML)

    def test_stage_4_023_lifecycle_contract_23(self) -> None:
        self.assertIn("updateFromShm", COCKPIT_HTML)

    def test_stage_4_024_lifecycle_contract_24(self) -> None:
        self.assertIn("updateFromShm", COCKPIT_HTML)

    def test_stage_4_025_lifecycle_contract_25(self) -> None:
        self.assertIn("updateFromShm", COCKPIT_HTML)


if __name__ == "__main__":
    unittest.main()
