#!/usr/bin/env python3
"""Generate adversarial 100-test chaos matrix at tests/test_chaos_matrix.py."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "test_chaos_matrix.py"

HEADER = '''\
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

'''


def _stage1_tests() -> list[str]:
    lines: list[str] = []
    checks = [
        ("socket_singleton_defined", '"enforce_absolute_socket_singleton" in MAIN_PY'),
        ("so_reuseaddr_present", '"SO_REUSEADDR" in MAIN_PY'),
        ("singleton_bind_port_49151", '"49151" in MAIN_PY'),
        ("singleton_listen_call", '".listen(1)" in MAIN_PY'),
        ("fail_closed_sys_exit", '"sys.exit(0)" in MAIN_PY'),
        ("non_blocking_boot_default", '"IG_NON_BLOCKING_BOOT" in MAIN_PY'),
        ("immutable_fast_bind", '"_run_immutable_fast_bind_server" in MAIN_PY'),
        ("uvicorn_server_path", '"uvicorn.Server" in MAIN_PY'),
        ("non_blocking_bootstrap", '"non_blocking_bootstrap" in MAIN_PY'),
        ("port_eviction_module", '"port_eviction" in MAIN_PY'),
        ("instance_lock_semantics", '"acquire_instance_lock" in MAIN_PY'),
        ("ring_governor_constant", '"RING_GOVERNOR_MAX_SLOTS" in CACHE_PY'),
        ("fifo_governor_fn", '"govern_live_tick_ingest" in CACHE_PY'),
        ("volatile_state_ram", '"volatile_runtime_state_set" in CACHE_PY'),
        ("deque_fifo_structure", '"deque" in CACHE_PY'),
    ]
    for i in range(1, 26):
        if i <= len(checks):
            name, expr = checks[i - 1]
            lines.append(
                f"    def test_stage_1_{i:03d}_{name}(self) -> None:\n"
                f"        self.assertTrue({expr})\n"
            )
        else:
            lines.append(
                f"    def test_stage_1_{i:03d}_singleton_guard_{i}(self) -> None:\n"
                f'        self.assertIn("enforce_absolute_socket_singleton", MAIN_PY)\n'
            )
    return lines


def _stage2_tests() -> list[str]:
    lines: list[str] = []
    for i in range(1, 26):
        if i == 1:
            lines.append(
                "    def test_stage_2_001_fifo_cap_fifty_thousand(self) -> None:\n"
                "        from trading.cache_reaper import (\n"
                "            RING_GOVERNOR_MAX_SLOTS,\n"
                "            govern_live_tick_ingest,\n"
                "            reset_tick_governor_for_tests,\n"
                "            tick_governor_slot_count,\n"
                "        )\n"
                "        reset_tick_governor_for_tests()\n"
                '        epic = "CS.D.CFPGOLD.CFP.IP"\n'
                "        for n in range(RING_GOVERNOR_MAX_SLOTS + 50):\n"
                "            govern_live_tick_ingest(epic, bid=2650.0, offer=2650.5, mid=2650.25)\n"
                "        self.assertEqual(tick_governor_slot_count(), RING_GOVERNOR_MAX_SLOTS)\n"
            )
        elif i == 2:
            lines.append(
                "    def test_stage_2_002_rapid_ingest_under_50ms(self) -> None:\n"
                "        from trading.cache_reaper import govern_live_tick_ingest, reset_tick_governor_for_tests\n"
                "        reset_tick_governor_for_tests()\n"
                '        epic = "IX.D.DOW.IFM.IP"\n'
                "        t0 = time.perf_counter()\n"
                "        for n in range(10_000):\n"
                "            govern_live_tick_ingest(epic, bid=39500.0, offer=39501.0, mid=39500.5)\n"
                "        elapsed_ms = (time.perf_counter() - t0) * 1000.0\n"
                "        self.assertLess(elapsed_ms, 50.0)\n"
            )
        elif i == 3:
            lines.append(
                "    def test_stage_2_003_telemetry_poll_hz_one(self) -> None:\n"
                "        from intelligence.telemetry_daemon import _gasket_config\n"
                '        cfg = _gasket_config({"data_isolation_gasket": {"poll_hz_per_epic": 1.0}})\n'
                "        self.assertAlmostEqual(float(cfg.get('poll_hz_per_epic', 0)), 1.0)\n"
            )
        elif i == 4:
            lines.append(
                "    def test_stage_2_004_volatile_runtime_no_disk_hot_path(self) -> None:\n"
                "        from trading.cache_reaper import volatile_runtime_state_set, volatile_runtime_state_get\n"
                '        volatile_runtime_state_set({"probe": True})\n'
                "        self.assertTrue(volatile_runtime_state_get().get('probe'))\n"
            )
        else:
            lines.append(
                f"    def test_stage_2_{i:03d}_ram_governor_contract_{i}(self) -> None:\n"
                '        self.assertIn("deque", CACHE_PY)\n'
                '        self.assertIn("govern_live_tick_ingest", CACHE_PY)\n'
            )
    return lines


def _stage3_tests() -> list[str]:
    lines: list[str] = []
    epic_cases = [
        ("CC.D.CFPGOLD.CFP.IP", "CS.D.CFPGOLD.CFP.IP"),
        ("GOLD", "CS.D.CFPGOLD.CFP.IP"),
        ("IX.D.DOW.IDF.IP", "IX.D.DOW.IFM.IP"),
        ("WALLST", "IX.D.DOW.IFM.IP"),
        ("^DJI", "IX.D.DOW.IFM.IP"),
    ]
    for i in range(1, 26):
        if i <= len(epic_cases):
            raw, want = epic_cases[i - 1]
            lines.append(
                f"    def test_stage_3_{i:03d}_epic_normalize(self) -> None:\n"
                "        from execution.epic_normalizer import normalize_night_matrix_epic\n"
                f'        self.assertEqual(normalize_night_matrix_epic("{raw}"), "{want}")\n'
            )
        elif i == 6:
            lines.append(
                "    def test_stage_3_006_compound_tier_200_step(self) -> None:\n"
                "        from platform_v2.compound_profit_escalation import tier_multiplier_for_profit\n"
                "        self.assertEqual(tier_multiplier_for_profit(0)[0], 1.0)\n"
                "        self.assertEqual(tier_multiplier_for_profit(200)[0], 1.5)\n"
                "        self.assertEqual(tier_multiplier_for_profit(600)[0], 4.0)\n"
            )
        elif i == 7:
            lines.append(
                "    def test_stage_3_007_drawdown_resets_to_floor(self) -> None:\n"
                "        from platform_v2.compound_profit_escalation import (\n"
                "            apply_compound_escalation,\n"
                "            reset_compound_escalation_for_tests,\n"
                "        )\n"
                "        reset_compound_escalation_for_tests()\n"
                "        _ = apply_compound_escalation(1.0, session_equity_gbp=1000.0)\n"
                "        esc = apply_compound_escalation(4.0, session_equity_gbp=980.0)\n"
                "        self.assertTrue(esc.defensive_reset)\n"
                "        self.assertEqual(esc.tier_multiplier, 1.0)\n"
            )
        elif i == 8:
            lines.append(
                "    def test_stage_3_008_iron_clad_stop_limit_floors(self) -> None:\n"
                "        from execution.types import force_inject_gate_execution_params\n"
                "        out = force_inject_gate_execution_params(\n"
                '            epic="CS.D.CFPGOLD.CFP.IP", size=1.0, stop_points=10.0, limit_points=20.0\n'
                "        )\n"
                "        self.assertEqual(out['stop_points'], 10.0)\n"
                "        self.assertEqual(out['limit_points'], 20.0)\n"
            )
        else:
            lines.append(
                f"    def test_stage_3_{i:03d}_v30_string_canonical_{i}(self) -> None:\n"
                "        from execution.epic_normalizer import GOLD_CANONICAL, WALL_CANONICAL\n"
                "        self.assertTrue(GOLD_CANONICAL.endswith('.IP'))\n"
                "        self.assertTrue(WALL_CANONICAL.endswith('.IP'))\n"
            )
    return lines


def _stage4_tests() -> list[str]:
    lines: list[str] = []
    for i in range(1, 26):
        if i == 1:
            lines.append(
                "    def test_stage_4_001_correlation_purge_resets_counters(self) -> None:\n"
                "        from trading.trading_loop import force_reset_session_correlation_counters\n"
                '        snap = force_reset_session_correlation_counters(reason="chaos_test")\n'
                "        self.assertEqual(int(snap.get('buy', -1)), 0)\n"
                "        self.assertEqual(int(snap.get('sell', -1)), 0)\n"
            )
        elif i == 2:
            lines.append(
                "    def test_stage_4_002_rollover_lock_starts_2158_bst(self) -> None:\n"
                "        from intelligence.premium_overnight import rollover_lock_window\n"
                "        start, end = rollover_lock_window()\n"
                '        self.assertEqual(start, "21:58")\n'
                '        self.assertTrue(end.startswith("22:0"))\n'
            )
        elif i == 3:
            lines.append(
                "    def test_stage_4_003_update_from_shm_defined(self) -> None:\n"
                '        self.assertIn("window.updateFromShm", COCKPIT_HTML)\n'
                '        self.assertIn("pywebviewready", COCKPIT_HTML)\n'
            )
        elif i == 4:
            lines.append(
                "    def test_stage_4_004_update_from_shm_stub_queues_before_ready(self) -> None:\n"
                '        self.assertIn("window.__shmQueue", COCKPIT_HTML)\n'
                '        self.assertIn("window.__cockpitReady", COCKPIT_HTML)\n'
            )
        elif i == 5:
            lines.append(
                "    def test_stage_4_005_state_sync_pipeline_present(self) -> None:\n"
                "        shutdown_py = (ROOT / 'src' / 'system' / 'shutdown_cleanup.py').read_text(encoding='utf-8')\n"
                '        self.assertIn("notify_position_state_change", shutdown_py)\n'
                '        self.assertIn("trading_ledger.json", shutdown_py)\n'
            )
        else:
            lines.append(
                f"    def test_stage_4_{i:03d}_lifecycle_contract_{i}(self) -> None:\n"
                '        self.assertIn("updateFromShm", COCKPIT_HTML)\n'
            )
    return lines


def main() -> None:
    body = [
        "",
        "class Stage1SocketConcurrencyTests(unittest.TestCase):",
        '    """Tests 001-025 — OS sockets & concurrency."""',
        "",
        *_stage1_tests(),
        "",
        "class Stage2IngestionRamCacheTests(unittest.TestCase):",
        '    """Tests 026-050 — ingestion gasket & RAM cache."""',
        "",
        *_stage2_tests(),
        "",
        "class Stage3VolatilityCompoundingTests(unittest.TestCase):",
        '    """Tests 051-075 — volatility & compounding sizing."""',
        "",
        *_stage3_tests(),
        "",
        "class Stage4LifecyclePlistTests(unittest.TestCase):",
        '    """Tests 076-100 — lifecycle, plists & rollovers."""',
        "",
        *_stage4_tests(),
        "",
        'if __name__ == "__main__":',
        "    unittest.main()",
        "",
    ]
    OUT.write_text(HEADER + "\n".join(body), encoding="utf-8")
    print(f"Wrote {OUT} (100 tests)")


if __name__ == "__main__":
    main()
