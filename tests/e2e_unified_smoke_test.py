"""
Definitive unified-engine E2E smoke harness.

Proves:
  1. Zombie / SHM cleanup
  2. Multi-feed ring ingest → Thread B bare-metal matrix lookup
  3. 12-gate stack bypass on alpha path
  4. Mock execution fill → in-memory WIN/LOSS performance row + Telegram close dispatch

Run:
  PYTHONPATH=src IG_AGENT_PYTEST=1 python3 -m pytest tests/e2e_unified_smoke_test.py -x -v

Live Telegram close probe:
  IG_E2E_TELEGRAM=1 TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... \\
    PYTHONPATH=src IG_AGENT_PYTEST=1 python3 -m pytest tests/e2e_unified_smoke_test.py -k telegram -v

Optional live boot (local only, not CI):
  IG_E2E_BOOT_MAIN=1 FINNHUB_KEY=... TWELVE_DATA_KEY=... \\
    python3 -m pytest tests/e2e_unified_smoke_test.py -k test_boot_unified_master -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from data.models import Quote
from execution.order_validator import ValidationResult
from execution.trading_loop import TickOutcome
from execution.types import ExecutionResult
from signals.signal_engine import SignalResult
from system.config import Config
from system.ipc.ring_buffer import (
    SOURCE_YAHOO,
    get_alpha_ring_buffer,
    reset_alpha_ring_buffer_for_tests,
)
from system.unified_fulfillment_cache import (
    get_fulfillment_payload,
    record_execution_performance_row,
    reset_fulfillment_cache_for_tests,
)
from system.unified_engine import (
    configure_unified_engine_env,
    reset_unified_engine_for_tests,
    start_unified_engine,
    stop_unified_engine,
)
from trading.trading_loop import TradingLoop as OrchestratorLoop

EPIC = "CS.D.CFPGOLD.CFP.IP"
MARKET = "Gold"


def _cleanup_zombies_and_shm() -> None:
    """Clear stale singletons, locks, POSIX segments, and supervision drift."""
    stop_unified_engine()
    reset_unified_engine_for_tests()
    reset_fulfillment_cache_for_tests()
    reset_alpha_ring_buffer_for_tests()
    try:
        from analytics.post_open_audit import reset_post_open_audit_for_tests

        reset_post_open_audit_for_tests()
    except Exception:
        pass
    try:
        from system.bootstrap_sanitizer import run_supervision_self_sanitize

        run_supervision_self_sanitize(repair=True)
    except Exception:
        pass
    try:
        from intelligence.matrix_prebaker import force_unmap_alpha_matrix, reset_alpha_matrix_for_tests

        force_unmap_alpha_matrix()
        reset_alpha_matrix_for_tests()
    except Exception:
        pass
    for name in ("ig_agent_v30_alpha_matrix", "ig_agent_v30_live_state"):
        try:
            from multiprocessing import shared_memory

            seg = shared_memory.SharedMemory(name=name, create=False)
            seg.close()
            seg.unlink()
        except Exception:
            pass
    lock = ROOT / "src" / "data" / ".ig_agent_v29.lock"
    if lock.is_file():
        try:
            lock.unlink()
        except Exception:
            pass


def _seed_alpha_cell(
    ring,
    *,
    epic: str,
    direction: str = "BUY",
    signal_floor: float = 50.0,
    win_prob: float = 0.56,
) -> int:
    from intelligence.matrix_prebaker import (
        COL_APPROVED,
        COL_FITNESS_FLOOR,
        COL_ML_FLOOR,
        COL_SAMPLES,
        COL_SIGNAL_FLOOR,
        COL_WIN_PROB,
        epic_slot,
        matrix_cell_index,
        quantize_atr,
        quantize_momentum,
        quantize_rsi,
    )

    cell = matrix_cell_index(
        epic_id=epic_slot(epic),
        direction=direction,
        rsi_q=quantize_rsi(55.0),
        atr_q=quantize_atr(12.0, epic=epic),
        mom_q=quantize_momentum(0.001),
    )
    row = ring.matrix[cell]
    row[COL_SAMPLES] = np.float32(24.0)
    row[COL_APPROVED] = np.float32(1.0)
    row[COL_SIGNAL_FLOOR] = np.float32(signal_floor)
    row[COL_FITNESS_FLOOR] = np.float32(55.0)
    row[COL_ML_FLOOR] = np.float32(0.45)
    row[COL_WIN_PROB] = np.float32(win_prob)
    ring.write_matrix_generation(ring.matrix, vector_density=1)
    return int(cell)


def _make_cfg() -> Config:
    return Config(
        {
            "operating_mode": "DEMO",
            "dry_run": False,
            "allow_live_trading": False,
            "auto_trade_enabled": True,
            "signal_threshold": 50,
            "trade_size": 1.0,
            "currency_code": "USD",
            "cooldown_seconds": 0,
            "max_spread_points": 100,
            "max_spread": 100,
            "stop_distance_points": 30,
            "limit_distance_points": 90,
            "reward_multiple": 3.0,
            "risk_points": 30,
            "max_open_positions": 5,
            "max_positions_per_epic": 2,
            "refresh_seconds": 5,
            "trading_hours_enabled": False,
            "market_watch_enabled": False,
            "adaptive_execution_enabled": False,
            "learning_demo_mode": {"enabled": False},
        }
    )


def _build_orchestrator_loop() -> OrchestratorLoop:
    cfg = _make_cfg()
    exec_loop = MagicMock()
    exec_loop.execution_engine = MagicMock()
    exec_loop.execution_engine.update_positions = MagicMock()

    loop = OrchestratorLoop(
        config=cfg,
        market=MARKET,
        epic=EPIC,
        session_manager=MagicMock(is_session_open=MagicMock(return_value=True)),
        environment_scorer=MagicMock(),
        points_engine=MagicMock(),
        signal_engine=MagicMock(add_quote=MagicMock()),
        execution_loop=exec_loop,
        quote_source=lambda: None,
        publish_snapshots=False,
    )
    loop._tick_indicator_snapshot = lambda _q: {"rsi": 55.0, "atr": 12.0}
    return loop


class UnifiedE2ESmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        _cleanup_zombies_and_shm()
        configure_unified_engine_env(cycle_sec=900, api_port=8080)
        os.environ["IG_E2E_SHADOW_FORCE_FILL"] = "1"
        os.environ["FINNHUB_KEY"] = os.environ.get(
            "FINNHUB_KEY", "d84asthr01qutij8i4a0d84asthr01qutij8i4ag"
        )
        os.environ["TWELVE_DATA_KEY"] = os.environ.get(
            "TWELVE_DATA_KEY", "c33d709357dd4ef8823d4e3eefdac056"
        )

    def tearDown(self) -> None:
        _cleanup_zombies_and_shm()
        os.environ.pop("IG_E2E_SHADOW_FORCE_FILL", None)

    def test_synthetic_tick_bypasses_gates_and_records_performance(self) -> None:
        ring = get_alpha_ring_buffer()
        cell = _seed_alpha_cell(ring, epic=EPIC, win_prob=0.562)
        ring.write_quote_race_win(
            EPIC,
            bid=2650.0,
            offer=2650.5,
            mid=2650.25,
            source_id=SOURCE_YAHOO,
        )

        loop = _build_orchestrator_loop()
        quote = Quote(datetime.now(timezone.utc), 2650.0, 2650.5)

        signal = SignalResult(
            signal="BUY",
            raw_confidence=56.2,
            adjusted_confidence=56.2,
            learning_delta=0.0,
            setup_key="e2e",
            notes="e2e",
            snapshot={"atr": 12.0},
        )
        fill = ExecutionResult(
            success=True,
            action="OPEN",
            deal_id="E2E-MOCK-001",
            deal_reference="E2E-MOCK-001",
        )
        mock_outcome = TickOutcome(
            quote=quote,
            signal=signal,
            trade_signal=MagicMock(),
            validation=ValidationResult(allowed=True, reasons=[], checks={}),
            execution=fill,
        )
        with patch.object(
            loop._execution_loop,
            "process_tick",
            return_value=mock_outcome,
        ) as mock_proc:
            ctx = loop.run_bare_metal_unified_tick(quote)

        self.assertIsNotNone(ctx)
        self.assertTrue(ctx.all_passed, ctx.wait_reason)
        self.assertEqual(len(ctx.gates), 0, "zero-gate frontier path has no gate stack")
        self.assertTrue(mock_proc.called, "mock IG execution bridge must fire")
        mock_proc.assert_called_once()
        self.assertTrue(
            mock_proc.call_args.kwargs.get("shadow_force_fill"),
            "bare-metal path must force shadow fill in E2E",
        )

        payload = get_fulfillment_payload()
        row = payload.get("last_performance_row")
        self.assertIsNotNone(row, "performance row must land in RAM cache")
        self.assertIn(str(row.get("result")), ("WIN", "LOSS"))
        self.assertGreater(float(row.get("confidence") or 0), 54.5)
        self.assertEqual(int(row.get("cell_index")), cell)
        self.assertIn(str(row.get("status")), ("OPEN", "CLOSED"))

    def test_supervision_self_sanitize_embedded(self) -> None:
        from system.bootstrap_sanitizer import run_supervision_self_sanitize

        payload = run_supervision_self_sanitize(repair=True)
        self.assertIn("supervision_drift", payload)
        self.assertIn("overnight_supervision", payload)

    def test_instant_close_telegram_dispatch_mocked(self) -> None:
        from analytics.post_open_audit import (
            format_instant_trade_close,
            record_closed_trade,
            start_post_open_audit_hub,
        )

        start_post_open_audit_hub(hourly=False)
        row = {
            "epic": EPIC,
            "direction": "BUY",
            "result": "WIN",
            "size": 1.0,
            "entry": 2650.0,
            "exit": 2655.0,
            "pnl_gbp": 4.25,
            "status": "CLOSED",
        }
        text = format_instant_trade_close(row)
        self.assertIn(EPIC, text)
        self.assertIn("WIN", text)
        self.assertIn("£+4.25", text)

        sent: list[str] = []

        async def _fake_send(msg: str) -> bool:
            sent.append(msg)
            return True

        with patch(
            "analytics.post_open_audit._telegram_send_async",
            side_effect=_fake_send,
        ):
            record_closed_trade(row)
            deadline = time.time() + 3.0
            while time.time() < deadline and not sent:
                time.sleep(0.05)
        self.assertTrue(sent, "async Telegram task must dispatch close alert")
        self.assertIn("TRADE CLOSED", sent[0])

    def test_dual_horizon_summary_format(self) -> None:
        from analytics.post_open_audit import (
            format_dual_horizon_summary,
            record_closed_trade,
        )

        record_closed_trade(
            {
                "epic": EPIC,
                "direction": "BUY",
                "result": "WIN",
                "pnl_gbp": 2.5,
                "size": 1.0,
                "entry": 100.0,
                "exit": 101.0,
            }
        )
        summary = format_dual_horizon_summary()
        self.assertIn("Dual-Horizon Audit", summary)
        self.assertIn("Last 1 Hour", summary)
        self.assertIn("Rolling 24 Hours", summary)

    @unittest.skipUnless(
        os.environ.get("IG_E2E_TELEGRAM") == "1",
        "set IG_E2E_TELEGRAM=1 to push a live Telegram close alert",
    )
    def test_live_telegram_close_notification(self) -> None:
        from analytics.post_open_audit import record_closed_trade, start_post_open_audit_hub

        start_post_open_audit_hub(hourly=False)
        os.environ["IG_E2E_TELEGRAM"] = "1"
        record_closed_trade(
            {
                "epic": EPIC,
                "direction": "BUY",
                "result": "WIN",
                "size": 1.0,
                "entry": 2650.0,
                "exit": 2655.0,
                "pnl_gbp": 0.01,
                "deal_id": "E2E-TG-LIVE",
            }
        )
        time.sleep(2.0)

    def test_thread_b_ring_seq_dedupes_stale_ticks(self) -> None:
        ring = get_alpha_ring_buffer()
        _seed_alpha_cell(ring, epic=EPIC)
        ring.write_quote_race_win(
            EPIC,
            bid=100.0,
            offer=100.5,
            mid=100.25,
            source_id=SOURCE_YAHOO,
        )
        first = ring.read_quote_for_epic(EPIC)
        self.assertIsNotNone(first)
        bid1, offer1, seq1 = first  # type: ignore[misc]
        second = ring.read_quote_for_epic(EPIC)
        self.assertEqual(second, (bid1, offer1, seq1))
        ring.write_quote_race_win(
            EPIC,
            bid=101.0,
            offer=101.5,
            mid=101.25,
            source_id=SOURCE_YAHOO,
        )
        third = ring.read_quote_for_epic(EPIC)
        self.assertIsNotNone(third)
        self.assertGreater(int(third[2]), int(seq1))  # type: ignore[index]

    def test_fulfillment_cache_four_stages(self) -> None:
        ring = get_alpha_ring_buffer()
        _seed_alpha_cell(ring, epic=EPIC)
        ring.write_recency_calibration(rsi_bias=0.0, atr_bias=0.0, mom_bias=0.0)
        record_execution_performance_row(
            epic=EPIC,
            direction="BUY",
            result="WIN",
            confidence=56.2,
            cell_index=0,
            latency_us=0.42,
            deal_id="E2E-STAGE",
        )
        snap = get_fulfillment_payload()
        stages = snap.get("stages") or []
        self.assertEqual(len(stages), 4)
        names = [s.get("name") for s in stages]
        self.assertEqual(
            names,
            [
                "Ingestion Health",
                "Look-Ahead Matrix",
                "Auto-Tuning Core",
                "Live Execution Bridge",
            ],
        )

    @unittest.skipUnless(
        os.environ.get("IG_E2E_BOOT_MAIN") == "1",
        "set IG_E2E_BOOT_MAIN=1 to boot live master (local pre-flight only)",
    )
    def test_boot_unified_master_subprocess(self) -> None:
        """Boot single master with credentials — operator pre-flight only."""
        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "IG_UNIFIED_ENGINE": "1",
            "IG_PARALLEL_DUAL": "0",
            "IG_AGENT_FROM_LAUNCHER": "1",
            "FINNHUB_KEY": os.environ.get("FINNHUB_KEY", ""),
            "TWELVE_DATA_KEY": os.environ.get("TWELVE_DATA_KEY", ""),
        }
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "src" / "main.py"), "--daemon-cycle=900"],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            deadline = time.time() + 90.0
            healthy = False
            while time.time() < deadline:
                try:
                    import urllib.request

                    with urllib.request.urlopen("http://127.0.0.1:8080/api/health", timeout=2) as res:
                        if res.status == 200:
                            healthy = True
                            break
                except Exception:
                    pass
                if proc.poll() is not None:
                    break
                time.sleep(2.0)
            self.assertTrue(healthy, "unified master failed to reach /api/health")
            with urllib.request.urlopen(
                "http://127.0.0.1:8080/api/unified/fulfillment", timeout=3
            ) as res:
                body = res.read().decode("utf-8")
            self.assertIn("stages", body)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    unittest.main()
