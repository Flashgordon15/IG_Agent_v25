"""
Lock-less circular alpha ring buffer — in-process Thread A → Thread B handoff.

Single-writer (Shadow Coprocessor) / single-reader (Live Execution) pattern.
No mutex on the hot path; alignment tracked via monotonic sequence counters.
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np

from intelligence.matrix_prebaker import (
    COL_APPROVED,
    COL_ATR_ANCHOR,
    COL_FITNESS_FLOOR,
    COL_ML_FLOOR,
    COL_RSI_ANCHOR,
    COL_SAMPLES,
    COL_SIGNAL_FLOOR,
    COL_WIN_PROB,
    MATRIX_COLS,
    TOTAL_CELLS,
)
from system.market_data_hub import NIGHT_MATRIX_EPICS
from system.engine_log import log_engine

RING_CALIB_SLOTS = 256
QUOTE_EPIC_SLOTS = len(NIGHT_MATRIX_EPICS)
QUOTE_COL_BID = 0
QUOTE_COL_OFFER = 1
QUOTE_COL_MID = 2
QUOTE_COL_SOURCE = 3
QUOTE_COL_WIN_SEQ = 4
QUOTE_COL_UPDATED_NS = 5
QUOTE_SLOT_COLS = 6
SOURCE_YAHOO = 1.0
SOURCE_FINNHUB = 2.0
SOURCE_TWELVE_DATA = 3.0
_META_WRITE_SEQ = 0
_META_READ_SEQ = 1
_META_ALIGNED = 2
_META_VECTOR_DENSITY = 3
_META_LAST_WRITE_NS = 4
_META_LAST_READ_NS = 5
_META_E2E_LAST_NS = 6
_META_E2E_P50_NS = 7
_META_E2E_P99_NS = 8
_META_COMPILE_GEN = 9
_META_SIZE = 16

_RING_SINGLETON: "UnifiedAlphaRingBuffer | None" = None


class UnifiedAlphaRingBuffer:
    """
    Fixed pre-allocated matrix + calibration ring in process RAM.

    Thread A writes via naked index assignment; Thread B reads ``matrix_view``
    without locks. Misalignment surfaces as empty cells / death-switch — not stalls.
    """

    def __init__(self) -> None:
        self._matrix = np.zeros((TOTAL_CELLS, MATRIX_COLS), dtype=np.float32, order="C")
        self._calibrations = np.zeros((RING_CALIB_SLOTS, 4), dtype=np.float64, order="C")
        self._quote_ring = np.zeros((QUOTE_EPIC_SLOTS, QUOTE_SLOT_COLS), dtype=np.float32, order="C")
        self._meta = np.zeros(_META_SIZE, dtype=np.uint64)
        self._e2e_samples_ns: list[int] = []

    def epic_quote_slot(self, epic: str) -> int | None:
        try:
            return NIGHT_MATRIX_EPICS.index(str(epic or "").strip())
        except ValueError:
            return None

    def write_quote_race_win(
        self,
        epic: str,
        *,
        bid: float,
        offer: float,
        mid: float,
        source_id: float,
    ) -> bool:
        """Thread A racing feeds — naked pointer write into quote ring slot."""
        slot = self.epic_quote_slot(epic)
        if slot is None:
            return False
        row = self._quote_ring[slot]
        row[QUOTE_COL_BID] = np.float32(bid)
        row[QUOTE_COL_OFFER] = np.float32(offer)
        row[QUOTE_COL_MID] = np.float32(mid)
        row[QUOTE_COL_SOURCE] = np.float32(source_id)
        row[QUOTE_COL_WIN_SEQ] = np.float32(float(row[QUOTE_COL_WIN_SEQ]) + 1.0)
        row[QUOTE_COL_UPDATED_NS] = np.float32(time.perf_counter_ns() % (2**32))
        self._meta[_META_LAST_WRITE_NS] = time.perf_counter_ns()
        return True

    def read_quote_for_epic(self, epic: str) -> tuple[float, float, int] | None:
        """Thread B — naked quote ring read (bid, offer, win_seq)."""
        slot = self.epic_quote_slot(epic)
        if slot is None:
            return None
        row = self._quote_ring[slot]
        bid = float(row[QUOTE_COL_BID])
        offer = float(row[QUOTE_COL_OFFER])
        if bid <= 0.0 or offer <= 0.0:
            return None
        seq = int(float(row[QUOTE_COL_WIN_SEQ]))
        self._meta[_META_LAST_READ_NS] = time.perf_counter_ns()
        return bid, offer, seq

    def quote_ring_telemetry(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for idx, epic in enumerate(NIGHT_MATRIX_EPICS):
            row = self._quote_ring[idx]
            source_val = float(row[QUOTE_COL_SOURCE])
            source = "yahoo"
            if source_val >= SOURCE_TWELVE_DATA:
                source = "twelvedata"
            elif source_val >= SOURCE_FINNHUB:
                source = "finnhub"
            rows.append(
                {
                    "epic": epic,
                    "bid": float(row[QUOTE_COL_BID]),
                    "offer": float(row[QUOTE_COL_OFFER]),
                    "mid": float(row[QUOTE_COL_MID]),
                    "source": source,
                    "win_seq": int(float(row[QUOTE_COL_WIN_SEQ])),
                }
            )
        return rows

    @property
    def matrix(self) -> np.ndarray:
        return self._matrix

    def write_matrix_generation(
        self,
        source: np.ndarray,
        *,
        vector_density: int,
    ) -> int:
        """Thread A — publish full pre-baked matrix generation."""
        if source.shape != self._matrix.shape:
            raise ValueError(f"matrix shape mismatch {source.shape} != {self._matrix.shape}")
        self._matrix[:] = source
        gen = int(self._meta[_META_COMPILE_GEN]) + 1
        self._meta[_META_COMPILE_GEN] = gen
        self._meta[_META_VECTOR_DENSITY] = int(max(0, vector_density))
        self._meta[_META_LAST_WRITE_NS] = time.perf_counter_ns()
        self._meta[_META_WRITE_SEQ] = int(self._meta[_META_WRITE_SEQ]) + 1
        self._meta[_META_ALIGNED] = 1
        return gen

    def write_recency_calibration(
        self,
        *,
        rsi_bias: float,
        atr_bias: float,
        mom_bias: float,
        recency_weight: float = 1.0,
    ) -> None:
        """Thread A — 1-second recency calibration slot (naked ring index)."""
        seq = int(self._meta[_META_WRITE_SEQ])
        slot = seq % RING_CALIB_SLOTS
        self._calibrations[slot, 0] = float(rsi_bias)
        self._calibrations[slot, 1] = float(atr_bias)
        self._calibrations[slot, 2] = float(mom_bias)
        self._calibrations[slot, 3] = float(recency_weight)
        self._meta[_META_LAST_WRITE_NS] = time.perf_counter_ns()

    def matrix_view(self) -> np.ndarray:
        """Thread B — naked pointer read of current matrix generation."""
        self._meta[_META_READ_SEQ] = int(self._meta[_META_WRITE_SEQ])
        self._meta[_META_LAST_READ_NS] = time.perf_counter_ns()
        return self._matrix

    def lookup_row(self, pattern_index: int) -> np.ndarray:
        """Instantaneous row lookup — records end-to-end latency in nanoseconds."""
        t0 = time.perf_counter_ns()
        row = self._matrix[int(pattern_index)]
        elapsed = time.perf_counter_ns() - t0
        self._record_e2e_ns(elapsed)
        self._meta[_META_READ_SEQ] = int(self._meta[_META_WRITE_SEQ])
        self._meta[_META_LAST_READ_NS] = time.perf_counter_ns()
        return row

    def _record_e2e_ns(self, elapsed_ns: int) -> None:
        self._meta[_META_E2E_LAST_NS] = int(elapsed_ns)
        self._e2e_samples_ns.append(int(elapsed_ns))
        if len(self._e2e_samples_ns) > 512:
            del self._e2e_samples_ns[:-512]
        ordered = sorted(self._e2e_samples_ns)
        self._meta[_META_E2E_P50_NS] = int(ordered[len(ordered) // 2])
        self._meta[_META_E2E_P99_NS] = int(ordered[max(0, int(len(ordered) * 0.99) - 1)])

    def thread_aligned(self) -> bool:
        write_seq = int(self._meta[_META_WRITE_SEQ])
        read_seq = int(self._meta[_META_READ_SEQ])
        return bool(int(self._meta[_META_ALIGNED])) and write_seq > 0 and read_seq == write_seq

    def vector_density(self) -> int:
        return int(self._meta[_META_VECTOR_DENSITY])

    def e2e_latency_profile_ns(self) -> dict[str, int]:
        return {
            "last_ns": int(self._meta[_META_E2E_LAST_NS]),
            "p50_ns": int(self._meta[_META_E2E_P50_NS]),
            "p99_ns": int(self._meta[_META_E2E_P99_NS]),
        }

    def telemetry(self) -> dict[str, Any]:
        return {
            "thread_a_write_seq": int(self._meta[_META_WRITE_SEQ]),
            "thread_b_read_seq": int(self._meta[_META_READ_SEQ]),
            "thread_aligned": self.thread_aligned(),
            "vector_density": self.vector_density(),
            "compile_generation": int(self._meta[_META_COMPILE_GEN]),
            "last_write_ns": int(self._meta[_META_LAST_WRITE_NS]),
            "last_read_ns": int(self._meta[_META_LAST_READ_NS]),
            "e2e_latency_ns": self.e2e_latency_profile_ns(),
            "matrix_bytes": int(self._matrix.nbytes),
            "calibration_slots": RING_CALIB_SLOTS,
            "quote_ring": self.quote_ring_telemetry(),
        }

    def row_to_lookup(self, row: np.ndarray) -> dict[str, float | bool | int]:
        samples = float(row[COL_SAMPLES])
        return {
            "hit": samples > 0.0,
            "approved": samples > 0.0 and float(row[COL_APPROVED]) >= 0.5,
            "signal_floor": float(row[COL_SIGNAL_FLOOR]),
            "fitness_floor": float(row[COL_FITNESS_FLOOR]),
            "ml_floor": float(row[COL_ML_FLOOR]),
            "win_probability": float(row[COL_WIN_PROB]),
            "samples": samples,
        }


def get_alpha_ring_buffer() -> UnifiedAlphaRingBuffer:
    global _RING_SINGLETON
    if _RING_SINGLETON is None:
        _RING_SINGLETON = UnifiedAlphaRingBuffer()
        log_engine(
            f"UnifiedAlphaRingBuffer: allocated matrix={TOTAL_CELLS}x{MATRIX_COLS} "
            f"bytes={_RING_SINGLETON._matrix.nbytes}"
        )
    return _RING_SINGLETON


def unified_engine_active() -> bool:
    import os

    return os.environ.get("IG_UNIFIED_ENGINE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def reset_alpha_ring_buffer_for_tests() -> None:
    global _RING_SINGLETON
    _RING_SINGLETON = None
