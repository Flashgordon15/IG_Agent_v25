"""Harden factory matrix — gate params, ffill, IG ledger, SHM pid restart."""

from __future__ import annotations

import ctypes
import os
import sys
import unittest
from multiprocessing import shared_memory
from unittest.mock import MagicMock, patch

import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from execution.types import normalize_gate_execution_params
from intelligence.matrix_prebaker import (
    COL_SAMPLES,
    FFILL_STREAMING_EPICS,
    MATRIX_COLS,
    TOTAL_CELLS,
    apply_streaming_ffill_to_matrix,
    epic_slot,
    matrix_cell_index,
    matrix_row_with_streaming_ffill,
)
from system.ipc.cockpit_shm_passive import COCKPIT_SHM_MAGIC, CockpitShmHeader
from system.ipc.ring_buffer import (
    COCKPIT_SHM_ALLOC_BYTES,
    _attach_cockpit_shm,
    _evict_zombie_cockpit_shm,
    reset_cockpit_shm_for_tests,
)
from system.unified_fulfillment_cache import (
    _PERF_ROWS,
    record_execution_performance_row,
    reset_fulfillment_cache_for_tests,
    sync_performance_rows_from_ig_rest,
)
from execution.order_payload_builder import build_trade_signal_with_gate_params
from execution.types import force_inject_gate_execution_params
from harmonization.iron_clad_risk import (
    MANDATORY_LIMIT_POINTS,
    MANDATORY_STOP_POINTS,
    mandatory_limit_points_for_epic,
    mandatory_stop_points_for_epic,
)


class GateExecutionParamsTests(unittest.TestCase):
    def test_alpha_matrix_payload_requires_stop_and_limit(self) -> None:
        gate_exec = {
            "alpha_matrix": True,
            "gate_sourced": True,
            "actual_size": 1.0,
            "size": 1.0,
            "stop_points": 10.0,
            "limit_points": 20.0,
            "stop_source": "alpha_matrix_epic_default",
        }
        normalized = normalize_gate_execution_params(gate_exec)
        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertEqual(normalized["stop_points"], 10.0)
        self.assertEqual(normalized["limit_points"], 20.0)
        self.assertTrue(normalized.get("gate_sourced"))

    def test_missing_stop_points_gets_iron_clad_floors(self) -> None:
        partial = {
            "gate_sourced": True,
            "actual_size": 1.0,
            "size": 1.0,
        }
        normalized = normalize_gate_execution_params(partial)
        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertEqual(normalized["stop_points"], 8.0)
        self.assertEqual(normalized["limit_points"], 12.0)


class StreamingMatrixFfillTests(unittest.TestCase):
    def test_nikkei_empty_cell_forward_filled(self) -> None:
        epic = "IX.D.NIKKEI.IFM.IP"
        self.assertIn(epic, FFILL_STREAMING_EPICS)
        matrix = np.zeros((TOTAL_CELLS, MATRIX_COLS), dtype=np.float32)
        anchor_idx = matrix_cell_index(
            epic_id=epic_slot(epic),
            direction="BUY",
            rsi_q=8,
            atr_q=4,
            mom_q=2,
        )
        matrix[anchor_idx][COL_SAMPLES] = 12.0
        matrix[anchor_idx][0] = 52.5

        empty_idx = matrix_cell_index(
            epic_id=epic_slot(epic),
            direction="BUY",
            rsi_q=8,
            atr_q=4,
            mom_q=5,
        )
        self.assertEqual(float(matrix[empty_idx][COL_SAMPLES]), 0.0)

        filled = apply_streaming_ffill_to_matrix(matrix)
        self.assertGreater(filled, 0)
        self.assertGreater(float(matrix[empty_idx][COL_SAMPLES]), 0.0)

        row = matrix_row_with_streaming_ffill(matrix, empty_idx, epic=epic)
        self.assertGreater(float(row[COL_SAMPLES]), 0.0)

    def test_eurusd_lookup_bridge_ffill(self) -> None:
        epic = "CS.D.EURUSD.CFD.IP"
        matrix = np.zeros((TOTAL_CELLS, MATRIX_COLS), dtype=np.float32)
        idx = matrix_cell_index(
            epic_id=epic_slot(epic),
            direction="SELL",
            rsi_q=10,
            atr_q=2,
            mom_q=0,
        )
        matrix[idx][COL_SAMPLES] = 3.0
        target = matrix_cell_index(
            epic_id=epic_slot(epic),
            direction="SELL",
            rsi_q=10,
            atr_q=2,
            mom_q=7,
        )
        row = matrix_row_with_streaming_ffill(matrix, target, epic=epic)
        self.assertGreater(float(row[COL_SAMPLES]), 0.0)


class IgRestLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_fulfillment_cache_for_tests()

    def tearDown(self) -> None:
        reset_fulfillment_cache_for_tests()

    def test_phantom_row_without_deal_id_rejected_in_demo(self) -> None:
        with patch(
            "system.agent_execution_mode.authentic_demo_broker_required",
            return_value=True,
        ):
            record_execution_performance_row(
                epic="CS.D.CFPGOLD.CFP.IP",
                direction="BUY",
                result="WIN",
                confidence=80.0,
                cell_index=1,
                latency_us=10.0,
                deal_id="",
                pnl_gbp=1.0,
            )
        self.assertEqual(len(_PERF_ROWS), 0)

    def test_sync_replaces_rows_from_positions_otc(self) -> None:
        mock_positions = [
            {
                "market": {"epic": "CS.D.CFPGOLD.CFP.IP", "bid": 2400.0},
                "position": {
                    "dealId": "DIAAA123",
                    "direction": "BUY",
                    "size": 1.0,
                    "level": 2400.5,
                    "upl": 12.34,
                },
            }
        ]
        mock_rest = MagicMock()
        mock_rest.open_positions.return_value = mock_positions

        with patch(
            "system.agent_execution_mode.authentic_demo_broker_required",
            return_value=True,
        ), patch(
            "system.credentials_loader.load_credentials",
            return_value={},
        ), patch(
            "system.ig_rest_session.ensure_shared_authenticated",
            return_value=mock_rest,
        ):
            count = sync_performance_rows_from_ig_rest(force=True)

        self.assertEqual(count, 1)
        self.assertEqual(len(_PERF_ROWS), 1)
        row = _PERF_ROWS[0]
        self.assertEqual(row["deal_id"], "DIAAA123")
        self.assertEqual(row["source"], "ig_rest_positions_otc")
        self.assertEqual(row["status"], "OPEN")
        mock_rest.open_positions.assert_called_once()


class CockpitShmPidRestartTests(unittest.TestCase):
    _TEST_SHM = "ig_agent_v30_shm_harden_test"

    def setUp(self) -> None:
        reset_cockpit_shm_for_tests()
        self._unlink_test_segment()

    def tearDown(self) -> None:
        self._unlink_test_segment()
        reset_cockpit_shm_for_tests()

    @classmethod
    def _unlink_test_segment(cls) -> None:
        try:
            seg = shared_memory.SharedMemory(name=cls._TEST_SHM, create=False)
        except FileNotFoundError:
            return
        try:
            seg.close()
            seg.unlink()
        except Exception:
            pass

    def test_stale_pid_segment_evicted_on_restart(self) -> None:
        name = self._TEST_SHM
        seg = shared_memory.SharedMemory(name=name, create=True, size=COCKPIT_SHM_ALLOC_BYTES)
        try:
            hdr = CockpitShmHeader()
            hdr.magic = COCKPIT_SHM_MAGIC
            hdr.agent_pid = 999_999_999
            seg.buf[: ctypes.sizeof(CockpitShmHeader)] = bytes(hdr)
        finally:
            seg.close()

        with patch("system.ipc.ring_buffer.COCKPIT_SHM_NAME", name):
            _evict_zombie_cockpit_shm(name)

        with self.assertRaises(FileNotFoundError):
            shared_memory.SharedMemory(name=name, create=False)

    def test_attach_create_replaces_foreign_pid_layout(self) -> None:
        name = self._TEST_SHM
        self._unlink_test_segment()
        foreign = shared_memory.SharedMemory(name=name, create=True, size=COCKPIT_SHM_ALLOC_BYTES)
        try:
            hdr = CockpitShmHeader()
            hdr.magic = COCKPIT_SHM_MAGIC
            hdr.agent_pid = 42
            foreign.buf[: ctypes.sizeof(CockpitShmHeader)] = bytes(hdr)
        finally:
            foreign.close()

        with patch("system.ipc.ring_buffer.COCKPIT_SHM_NAME", name), patch(
            "system.ipc.ring_buffer._COCKPIT_SHM", None
        ):
            seg = _attach_cockpit_shm(create=True)
            live = CockpitShmHeader.from_buffer_copy(
                bytes(seg.buf[: ctypes.sizeof(CockpitShmHeader)])
            )
            self.assertEqual(int(live.magic), COCKPIT_SHM_MAGIC)
            self.assertNotEqual(int(live.agent_pid), 42)
            seg.close()


class IntegrityAbortGuardTests(unittest.TestCase):
    """Gold / Wall St — gate_execution_params must never be absent at dispatch."""

    def test_force_inject_gold_and_wall_st(self) -> None:
        for epic in ("CS.D.CFPGOLD.CFP.IP", "IX.D.DOW.IFM.IP"):
            payload = force_inject_gate_execution_params(epic=epic, size=1.0, gate_execution_params=None)
            norm = normalize_gate_execution_params(payload)
            self.assertIsNotNone(norm, epic)
            assert norm is not None
            stop_floor = mandatory_stop_points_for_epic(epic)
            limit_floor = mandatory_limit_points_for_epic(epic)
            self.assertGreaterEqual(norm["stop_points"], stop_floor)
            self.assertGreaterEqual(norm["limit_points"], limit_floor)

    def test_order_builder_never_emits_empty_gate(self) -> None:
        from data.models import Quote
        from datetime import datetime, timezone

        q = Quote(time=datetime.now(timezone.utc), bid=1.0, offer=1.01)
        sig = build_trade_signal_with_gate_params(
            market="GOLD",
            epic="CS.D.CFPGOLD.CFP.IP",
            direction="BUY",
            raw_confidence=80.0,
            adjusted_confidence=80.0,
            setup_key="test",
            quote=q,
            gate_execution_params=None,
        )
        self.assertIsNotNone(sig.gate_execution_params)
        assert sig.gate_execution_params is not None
        self.assertGreater(float(sig.gate_execution_params["stop_points"]), 0)


class LatencyPacketFillTests(unittest.TestCase):
    def test_nikkei_bfill_populates_leading_gap(self) -> None:
        from intelligence.matrix_prebaker import LATENCY_PACKET_FFILL_EPICS, apply_streaming_ffill_to_matrix

        epic = "IX.D.NIKKEI.IFM.IP"
        self.assertIn(epic, LATENCY_PACKET_FFILL_EPICS)
        matrix = np.full((TOTAL_CELLS, MATRIX_COLS), np.nan, dtype=np.float32)
        anchor_idx = matrix_cell_index(
            epic_id=epic_slot(epic), direction="BUY", rsi_q=5, atr_q=3, mom_q=8
        )
        matrix[anchor_idx][COL_SAMPLES] = 7.0
        empty_leading = matrix_cell_index(
            epic_id=epic_slot(epic), direction="BUY", rsi_q=5, atr_q=3, mom_q=2
        )
        apply_streaming_ffill_to_matrix(matrix)
        self.assertGreater(float(matrix[empty_leading][COL_SAMPLES]), 0.0)

    def test_eurusd_nan_sanitized_before_lookup(self) -> None:
        from intelligence.matrix_prebaker import sanitize_matrix_nan_inf

        epic = "CS.D.EURUSD.CFD.IP"
        matrix = np.full((TOTAL_CELLS, MATRIX_COLS), np.nan, dtype=np.float32)
        idx = matrix_cell_index(
            epic_id=epic_slot(epic), direction="SELL", rsi_q=4, atr_q=2, mom_q=4
        )
        matrix[idx][COL_SAMPLES] = 5.0
        sanitize_matrix_nan_inf(matrix)
        target = matrix_cell_index(
            epic_id=epic_slot(epic), direction="SELL", rsi_q=4, atr_q=2, mom_q=9
        )
        row = matrix_row_with_streaming_ffill(matrix, target, epic=epic)
        self.assertFalse(np.isnan(float(row[COL_SAMPLES])))


class PhantomLedgerGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_fulfillment_cache_for_tests()

    def tearDown(self) -> None:
        reset_fulfillment_cache_for_tests()

    def test_phantom_simulation_rows_never_append(self) -> None:
        with patch(
            "system.agent_execution_mode.authentic_demo_broker_required",
            return_value=True,
        ):
            for _ in range(130):
                record_execution_performance_row(
                    epic="CS.D.CFPGOLD.CFP.IP",
                    direction="BUY",
                    result="WIN",
                    confidence=80.0,
                    cell_index=1,
                    latency_us=1.0,
                    deal_id="",
                    pnl_gbp=1.0,
                )
        self.assertEqual(len(_PERF_ROWS), 0)

    def test_rest_route_tag_is_positions_otc(self) -> None:
        from ig_api.endpoints import position_otc, position_otc_list

        self.assertEqual(position_otc(), "/v1/positions/otc")
        self.assertEqual(position_otc_list(), "/v1/positions/otc")


class AlphaMatrixShmLifecycleTests(unittest.TestCase):
    def tearDown(self) -> None:
        from intelligence.matrix_prebaker import force_unmap_alpha_matrix

        force_unmap_alpha_matrix()

    def test_flush_on_pid_change_unmaps_segment(self) -> None:
        from intelligence.matrix_prebaker import flush_stale_alpha_matrix_shm, force_unmap_alpha_matrix

        force_unmap_alpha_matrix()
        flush_stale_alpha_matrix_shm(current_pid=1000)
        with patch(
            "intelligence.matrix_prebaker.force_unmap_alpha_matrix",
            wraps=force_unmap_alpha_matrix,
        ) as mock_unmap:
            flushed = flush_stale_alpha_matrix_shm(current_pid=2000)
        self.assertTrue(flushed)
        mock_unmap.assert_called()


if __name__ == "__main__":
    unittest.main()
