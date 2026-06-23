"""
Zero-Gate Alpha Frontier — lock-less Thread A → Thread B tensor handoff.

Thread A compresses 5-day history + 1s recency into a contiguous frontier map
(``ig_agent_v30_alpha_frontier``) with fixed-width per-cell strategy slices.
Thread B performs a naked pointer read and extracts the full pre-baked payload.
"""

from __future__ import annotations

import ctypes
import os
import struct
import sys
import time
from dataclasses import dataclass
from multiprocessing import shared_memory
from typing import Any

import numpy as np

from intelligence.matrix_prebaker import (
    COL_APPROVED,
    COL_ATR_ANCHOR,
    COL_SAMPLES,
    COL_WIN_PROB,
    MATRIX_COLS,
    SLOT_TO_EPIC,
    TOTAL_CELLS,
    apply_streaming_ffill_to_matrix,
    matrix_row_with_streaming_ffill,
)
from system.market_data_hub import NIGHT_MATRIX_EPICS
from system.engine_log import log_engine

ALPHA_FRONTIER_NAME = "ig_agent_v30_alpha_frontier"
FAIL_ZONE = 0
WIN_ZONE = 1
UNMAPPED = 2

STRATEGY_SLICE_BYTES = 32
STRAT_OFF_ZONE = 0
STRAT_OFF_FLAGS = 1
STRAT_OFF_FLOAT0 = 4  # scalp lot size
# float32 slots at bytes 4,8,12,16,20,24,28

FLAG_SCALPER = 1
FLAG_TRAILING = 2
FLAG_DYNAMIC_TARGET = 4

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
_META_WIN_ZONES = 10
_META_FAIL_ZONES = 11
_META_LAST_COORD = 12
_META_LAST_ZONE = 13
_META_SIZE = 16

_RING_SINGLETON: "UnifiedAlphaRingBuffer | None" = None


@dataclass(frozen=True, slots=True)
class StrategyPayload:
    """Pre-baked multi-strategy vector extracted from a single RAM slice read."""

    coordinate: int
    zone: int
    flags: int
    scalp_lot: float
    trailing_stop_distance: float
    dynamic_profit_target: float
    atr_trail_multiplier: float
    win_probability: float
    breakeven_buffer: float
    recency_weight: float
    lookup_ns: int

    @property
    def scalper_enabled(self) -> bool:
        return bool(self.flags & FLAG_SCALPER)

    @property
    def trailing_enabled(self) -> bool:
        return bool(self.flags & FLAG_TRAILING)

    @property
    def dynamic_target_enabled(self) -> bool:
        return bool(self.flags & FLAG_DYNAMIC_TARGET)

    def as_dict(self) -> dict[str, Any]:
        return {
            "coordinate": self.coordinate,
            "zone": self.zone,
            "zone_label": "WIN_ZONE" if self.zone == WIN_ZONE else "FAIL_ZONE",
            "flags": self.flags,
            "scalper": self.scalper_enabled,
            "trailing": self.trailing_enabled,
            "dynamic_target": self.dynamic_target_enabled,
            "scalp_lot": round(self.scalp_lot, 4),
            "trailing_stop_distance": round(self.trailing_stop_distance, 4),
            "dynamic_profit_target": round(self.dynamic_profit_target, 4),
            "atr_trail_multiplier": round(self.atr_trail_multiplier, 4),
            "win_probability": round(self.win_probability, 4),
            "breakeven_buffer": round(self.breakeven_buffer, 4),
            "recency_weight": round(self.recency_weight, 4),
            "lookup_ns": self.lookup_ns,
        }


class UnifiedAlphaRingBuffer:
    """
    Multi-class tensor map — quote ring + float matrix + uint8 frontier +
    fixed-width ``uint8`` strategy slice per coordinate (32 bytes).
    """

    def __init__(self) -> None:
        self._matrix = np.zeros((TOTAL_CELLS, MATRIX_COLS), dtype=np.float32, order="C")
        self._frontier = np.full(TOTAL_CELLS, UNMAPPED, dtype=np.uint8, order="C")
        self._strategy_slice = np.zeros((TOTAL_CELLS, STRATEGY_SLICE_BYTES), dtype=np.uint8, order="C")
        self._calibrations = np.zeros((RING_CALIB_SLOTS, 4), dtype=np.float64, order="C")
        self._quote_ring = np.zeros((QUOTE_EPIC_SLOTS, QUOTE_SLOT_COLS), dtype=np.float32, order="C")
        self._meta = np.zeros(_META_SIZE, dtype=np.uint64)
        self._e2e_samples_ns: list[int] = []
        self._feed_latency_us: list[float] = []
        self._last_live_coord: dict[str, Any] = {}
        self._last_strategy_payload: dict[str, Any] = {}
        self._live_ticks_cached = 0

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
        latency_us: float = 0.0,
    ) -> bool:
        t0 = time.perf_counter_ns()
        ticks_before = int(self._live_ticks_cached)
        diag = _string_diag_view(create=True)

        if bid <= 0.0 or offer <= 0.0 or mid <= 0.0:
            if diag is not None:
                elapsed_us = int((time.perf_counter_ns() - t0) / 1000)
                record_phase1_drop(
                    diag, code=P1_NULL_TUPLE, latency_us=elapsed_us, ticks_before=ticks_before
                )
            return False

        slot = self.epic_quote_slot(epic)
        if slot is None:
            if diag is not None:
                elapsed_us = int((time.perf_counter_ns() - t0) / 1000)
                record_phase1_drop(
                    diag, code=P1_SOCKET, latency_us=elapsed_us, ticks_before=ticks_before
                )
            return False
        row = self._quote_ring[slot]
        row[QUOTE_COL_BID] = np.float32(bid)
        row[QUOTE_COL_OFFER] = np.float32(offer)
        row[QUOTE_COL_MID] = np.float32(mid)
        row[QUOTE_COL_SOURCE] = np.float32(source_id)
        row[QUOTE_COL_WIN_SEQ] = np.float32(float(row[QUOTE_COL_WIN_SEQ]) + 1.0)
        row[QUOTE_COL_UPDATED_NS] = np.float32(time.perf_counter_ns() % (2**32))
        self._live_ticks_cached += 1
        self._meta[_META_LAST_WRITE_NS] = time.perf_counter_ns()
        if latency_us > 0:
            self._feed_latency_us.append(float(latency_us))
            if len(self._feed_latency_us) > 256:
                del self._feed_latency_us[:-256]
        if diag is not None:
            elapsed_us = int((time.perf_counter_ns() - t0) / 1000)
            record_phase1_win(
                diag,
                latency_us=elapsed_us,
                source_id=int(source_id),
                ticks_before=ticks_before,
                ticks_after=int(self._live_ticks_cached),
            )
        return True

    def read_quote_for_epic(self, epic: str) -> tuple[float, float, int] | None:
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

    def live_ticks_cached(self) -> int:
        """Monotonic live ingress counter — climbs with every racing feed quote."""
        return int(self._live_ticks_cached)

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

    @property
    def frontier(self) -> np.ndarray:
        return self._frontier

    @property
    def strategy_slice(self) -> np.ndarray:
        return self._strategy_slice

    def _pack_strategy_cell(
        self,
        idx: int,
        *,
        zone: int,
        flags: int,
        scalp_lot: float,
        trail_dist: float,
        dyn_target: float,
        atr_mult: float,
        win_prob: float,
        breakeven: float,
        recency: float,
    ) -> None:
        packed = struct.pack(
            "<BB2x7f",
            int(zone) & 0xFF,
            int(flags) & 0xFF,
            float(scalp_lot),
            float(trail_dist),
            float(dyn_target),
            float(atr_mult),
            float(win_prob),
            float(breakeven),
            float(recency),
        )
        self._strategy_slice[idx] = np.frombuffer(packed, dtype=np.uint8)

    def _unpack_strategy_cell(self, idx: int) -> tuple[int, int, tuple[float, ...]]:
        raw = bytes(self._strategy_slice[idx])
        zone, flags = struct.unpack_from("<BB", raw, 0)
        floats = struct.unpack_from("<7f", raw, STRAT_OFF_FLOAT0)
        return int(zone), int(flags), floats

    def compile_strategy_tensor_from_matrix(self, cfg: Any | None = None) -> int:
        """Thread A — bake scalper / trailing / dynamic-target params per coordinate."""
        try:
            from execution.scalping.config import scalping_settings

            settings = scalping_settings(cfg)
        except Exception:
            settings = {}

        scalp_on = bool(settings.get("enabled", False))
        atr_mult = float(settings.get("atr_trail_multiplier", 0.5) or 0.5)
        breakeven = float(settings.get("breakeven_buffer_points", 2.0) or 2.0)
        default_lot = 0.1
        risk_mult = 2.5
        if cfg is not None:
            default_lot = float(getattr(cfg, "trade_size", 0.1) or 0.1)
            risk_mult = float(
                getattr(cfg, "adaptive_atr_risk_multiple", None)
                or getattr(cfg, "atr_multiplier", None)
                or 2.5
            )

        calib_slot = int(self._meta[_META_WRITE_SEQ]) % RING_CALIB_SLOTS
        recency_w = float(self._calibrations[calib_slot, 3] or 1.0)

        flags_base = FLAG_TRAILING | FLAG_DYNAMIC_TARGET
        if scalp_on:
            flags_base |= FLAG_SCALPER

        samples = self._matrix[:, COL_SAMPLES]
        approved = self._matrix[:, COL_APPROVED]
        win_prob = self._matrix[:, COL_WIN_PROB]
        atr_anchor = self._matrix[:, COL_ATR_ANCHOR]
        mapped = samples > 0.0

        written = 0
        for idx in range(TOTAL_CELLS):
            if not mapped[idx]:
                continue
            zone = int(self._frontier[idx])
            atr = max(float(atr_anchor[idx]), 0.5)
            wp = float(win_prob[idx])
            trail = max(atr * atr_mult, 1.0)
            target = max(atr * risk_mult, trail * 1.5)
            lot = default_lot * (1.0 + wp) if scalp_on else default_lot
            self._pack_strategy_cell(
                idx,
                zone=zone,
                flags=flags_base if zone == WIN_ZONE else 0,
                scalp_lot=lot,
                trail_dist=trail,
                dyn_target=target,
                atr_mult=atr_mult,
                win_prob=wp,
                breakeven=breakeven,
                recency=recency_w,
            )
            written += 1
        self._meta[_META_LAST_WRITE_NS] = time.perf_counter_ns()
        return written

    def rebuild_frontier_tensor(self) -> int:
        """Thread A — project float matrix into binary WIN/FAIL codespace."""
        samples = self._matrix[:, COL_SAMPLES]
        approved = self._matrix[:, COL_APPROVED]
        mapped = samples > 0.0
        self._frontier.fill(UNMAPPED)
        if np.any(mapped):
            wins = mapped & (approved >= 0.5)
            fails = mapped & ~wins
            self._frontier[wins] = WIN_ZONE
            self._frontier[fails] = FAIL_ZONE
        win_n = int(np.sum(self._frontier == WIN_ZONE))
        fail_n = int(np.sum(self._frontier == FAIL_ZONE))
        self._meta[_META_WIN_ZONES] = win_n
        self._meta[_META_FAIL_ZONES] = fail_n
        self._meta[_META_ALIGNED] = 1
        self._meta[_META_WRITE_SEQ] = int(self._meta[_META_WRITE_SEQ]) + 1
        return win_n

    def stamp_recency_coordinate(
        self,
        coordinate: int,
        *,
        zone: int,
        epic: str = "",
        rsi: float = 0.0,
        atr: float = 0.0,
        momentum: float = 0.0,
        direction: str = "",
        scalp_lot: float = 0.0,
        trail_dist: float = 0.0,
        dyn_target: float = 0.0,
    ) -> None:
        """Thread A — 1s recency vector tags coordinate + refreshes strategy slice."""
        idx = int(coordinate) % TOTAL_CELLS
        z = WIN_ZONE if int(zone) == WIN_ZONE else FAIL_ZONE
        self._frontier[idx] = np.uint8(z)
        if scalp_lot > 0 or trail_dist > 0:
            self._pack_strategy_cell(
                idx,
                zone=z,
                flags=FLAG_SCALPER | FLAG_TRAILING | FLAG_DYNAMIC_TARGET,
                scalp_lot=max(scalp_lot, 0.1),
                trail_dist=max(trail_dist, 1.0),
                dyn_target=max(dyn_target, 1.5),
                atr_mult=0.5,
                win_prob=1.0 if z == WIN_ZONE else 0.0,
                breakeven=2.0,
                recency=1.0,
            )
        self._last_live_coord = {
            "coordinate": idx,
            "epic": epic,
            "rsi": round(float(rsi), 2),
            "atr": round(float(atr), 4),
            "momentum": round(float(momentum), 5),
            "direction": direction,
            "zone": int(self._frontier[idx]),
        }
        self._meta[_META_LAST_WRITE_NS] = time.perf_counter_ns()

    def write_matrix_generation(
        self,
        source: np.ndarray,
        *,
        vector_density: int,
        cfg: Any | None = None,
    ) -> int:
        if source.shape != self._matrix.shape:
            raise ValueError(f"matrix shape mismatch {source.shape} != {self._matrix.shape}")
        try:
            from intelligence.matrix_prebaker import flush_stale_alpha_matrix_shm

            flush_stale_alpha_matrix_shm()
        except Exception:
            pass
        working = np.array(source, copy=True, order="C")
        apply_streaming_ffill_to_matrix(working)
        self._matrix[:] = working
        win_n = self.rebuild_frontier_tensor()
        self.compile_strategy_tensor_from_matrix(cfg)
        gen = int(self._meta[_META_COMPILE_GEN]) + 1
        self._meta[_META_COMPILE_GEN] = gen
        self._meta[_META_VECTOR_DENSITY] = int(max(0, vector_density))
        self._meta[_META_LAST_WRITE_NS] = time.perf_counter_ns()
        return gen

    def write_recency_calibration(
        self,
        *,
        rsi_bias: float,
        atr_bias: float,
        mom_bias: float,
        recency_weight: float = 1.0,
    ) -> None:
        seq = int(self._meta[_META_WRITE_SEQ])
        slot = seq % RING_CALIB_SLOTS
        self._calibrations[slot, 0] = float(rsi_bias)
        self._calibrations[slot, 1] = float(atr_bias)
        self._calibrations[slot, 2] = float(mom_bias)
        self._calibrations[slot, 3] = float(recency_weight)
        self._meta[_META_LAST_WRITE_NS] = time.perf_counter_ns()

    def naked_frontier_lookup(self, coordinate: int) -> tuple[int, int]:
        """Thread B — zone-only read (legacy)."""
        payload = self.naked_strategy_lookup(coordinate)
        return payload.zone, payload.lookup_ns

    def naked_strategy_lookup(self, coordinate: int) -> StrategyPayload:
        """Thread B — single contiguous slice read (target <1µs)."""
        t0 = time.perf_counter_ns()
        idx = int(coordinate) % TOTAL_CELLS
        zone = int(self._frontier[idx])
        z_byte, flags, floats = self._unpack_strategy_cell(idx)
        if z_byte in (WIN_ZONE, FAIL_ZONE):
            zone = z_byte
        elapsed = time.perf_counter_ns() - t0
        self._record_e2e_ns(elapsed)
        self._meta[_META_READ_SEQ] = int(self._meta[_META_WRITE_SEQ])
        self._meta[_META_LAST_READ_NS] = time.perf_counter_ns()
        self._meta[_META_LAST_COORD] = idx
        self._meta[_META_LAST_ZONE] = zone
        payload = StrategyPayload(
            coordinate=idx,
            zone=zone,
            flags=flags,
            scalp_lot=float(floats[0]),
            trailing_stop_distance=float(floats[1]),
            dynamic_profit_target=float(floats[2]),
            atr_trail_multiplier=float(floats[3]),
            win_probability=float(floats[4]),
            breakeven_buffer=float(floats[5]),
            recency_weight=float(floats[6]),
            lookup_ns=elapsed,
        )
        self._last_strategy_payload = payload.as_dict()
        return payload

    def matrix_view(self) -> np.ndarray:
        self._meta[_META_READ_SEQ] = int(self._meta[_META_WRITE_SEQ])
        return self._matrix

    def lookup_row(self, pattern_index: int) -> np.ndarray:
        t0 = time.perf_counter_ns()
        idx = int(pattern_index)
        epic_id = idx // CELLS_PER_EPIC
        epic = SLOT_TO_EPIC.get(epic_id, "")
        row = matrix_row_with_streaming_ffill(self._matrix, idx, epic=epic)
        self._record_e2e_ns(time.perf_counter_ns() - t0)
        return row

    def _record_e2e_ns(self, elapsed_ns: int) -> None:
        self._meta[_META_E2E_LAST_NS] = int(elapsed_ns)
        self._e2e_samples_ns.append(int(elapsed_ns))
        if len(self._e2e_samples_ns) > 512:
            del self._e2e_samples_ns[:-512]
        ordered = sorted(self._e2e_samples_ns)
        self._meta[_META_E2E_P50_NS] = int(ordered[len(ordered) // 2])
        self._meta[_META_E2E_P99_NS] = int(ordered[max(0, int(len(ordered) * 0.99) - 1)])

    def feed_race_profile_us(self) -> dict[str, float]:
        samples = list(self._feed_latency_us)
        if not samples:
            return {"last_us": 0.0, "p50_us": 0.0, "p99_us": 0.0}
        ordered = sorted(samples)
        return {
            "last_us": float(samples[-1]),
            "p50_us": float(ordered[len(ordered) // 2]),
            "p99_us": float(ordered[max(0, int(len(ordered) * 0.99) - 1)]),
        }

    def frontier_tracker(self) -> dict[str, Any]:
        zone = int(self._meta[_META_LAST_ZONE])
        # WIN_ZONE ≠ order dispatch — injecting is tracked in fulfillment cache only.
        win_zone_armed = zone == WIN_ZONE
        injecting = False
        valve = (
            "🟢 WIN ZONE — Trading Enabled"
            if win_zone_armed
            else "⚪ SCANNING FRONTIER"
        )
        valve_icon = "🟢" if win_zone_armed else "⚪"
        coord = int(self._meta[_META_LAST_COORD])
        e2e = self.e2e_latency_profile_ns()
        feed = self.feed_race_profile_us()
        return {
            "frontier_name": ALPHA_FRONTIER_NAME,
            "bytes": int(self._frontier.nbytes + self._strategy_slice.nbytes),
            "strategy_slice_bytes": STRATEGY_SLICE_BYTES,
            "total_cells": TOTAL_CELLS,
            "win_zones": int(self._meta[_META_WIN_ZONES]),
            "fail_zones": int(self._meta[_META_FAIL_ZONES]),
            "last_coordinate": coord,
            "last_zone": zone,
            "last_zone_label": (
                "WIN_ZONE" if zone == WIN_ZONE else ("FAIL_ZONE" if zone == FAIL_ZONE else "UNMAPPED")
            ),
            "execution_valve": valve,
            "execution_valve_icon": valve_icon,
            "injecting": injecting,
            "win_zone_armed": win_zone_armed,
            "lookup_latency_ns": e2e,
            "feed_race_us": feed,
            "live_vector": dict(self._last_live_coord),
            "last_strategy": dict(self._last_strategy_payload),
            "thread_aligned": bool(int(self._meta[_META_ALIGNED])),
        }

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
        ft = self.frontier_tracker()
        return {
            "thread_a_write_seq": int(self._meta[_META_WRITE_SEQ]),
            "thread_b_read_seq": int(self._meta[_META_READ_SEQ]),
            "thread_aligned": self.thread_aligned(),
            "vector_density": self.vector_density(),
            "compile_generation": int(self._meta[_META_COMPILE_GEN]),
            "e2e_latency_ns": ft["lookup_latency_ns"],
            "live_ticks_cached": self.live_ticks_cached(),
            "alpha_frontier": ft,
            "quote_ring": self.quote_ring_telemetry(),
        }


def get_alpha_ring_buffer() -> UnifiedAlphaRingBuffer:
    global _RING_SINGLETON
    if _RING_SINGLETON is None:
        _RING_SINGLETON = UnifiedAlphaRingBuffer()
        log_engine(
            f"AlphaFrontierTensor: allocated name={ALPHA_FRONTIER_NAME} "
            f"cells={TOTAL_CELLS} frontier_bytes={_RING_SINGLETON._frontier.nbytes} "
            f"strategy_bytes={_RING_SINGLETON._strategy_slice.nbytes}"
        )
    return _RING_SINGLETON


def unified_engine_active() -> bool:
    return os.environ.get("IG_UNIFIED_ENGINE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


# ── Cockpit SHM (Darwin-safe `multiprocessing.shared_memory` segment) ─────────

COCKPIT_SHM_NAME = "ig_agent_v30_shm"
COCKPIT_SHM_MAGIC = 0x30334749  # 'IG30'
COCKPIT_SHM_VERSION = 3
COCKPIT_SHM_FILL_SLOTS = 5
COCKPIT_FILL_SLOTS = COCKPIT_SHM_FILL_SLOTS
# Darwin maps contiguous ctypes pages via POSIX shm — never Linux /dev/shm paths.
COCKPIT_SHM_DARWIN_MIN_BYTES = 4096

VALVE_SCANNING = 0
VALVE_WIN_ZONE = 1
VALVE_FIRE = 2
VALVE_STALL = 3


class CockpitFillRow(ctypes.Structure):
    """Fixed-width fulfillment row — binary registry slot."""

    _pack_ = 1
    _fields_ = [
        ("epoch_ms", ctypes.c_uint64),
        ("entry_micro", ctypes.c_uint32),
        ("pnl_cents", ctypes.c_int32),
        ("epic", ctypes.c_char * 28),
        ("action", ctypes.c_char * 6),
        ("status", ctypes.c_char * 8),
        ("result", ctypes.c_char * 6),
    ]


class CockpitShmHeader(ctypes.Structure):
    """
    ctypes.Structure registry — naked pointer layout for view_live_brain.py.

    Fields: ticks_cached, signal_threshold, atr_multiplier, valve_status, ...
    """

    _pack_ = 1
    _fields_ = [
        ("magic", ctypes.c_uint32),
        ("version", ctypes.c_uint16),
        ("header_bytes", ctypes.c_uint16),
        ("write_seq", ctypes.c_uint64),
        ("agent_pid", ctypes.c_uint32),
        ("updated_ns", ctypes.c_uint64),
        ("ticks_cached", ctypes.c_uint32),
        ("live_ram_ticks", ctypes.c_uint32),
        ("signal_threshold", ctypes.c_float),
        ("atr_multiplier", ctypes.c_float),
        ("vector_density", ctypes.c_uint32),
        ("valve_status", ctypes.c_uint8),
        ("zone", ctypes.c_uint8),
        ("stall_active", ctypes.c_uint8),
        ("memory_aligned", ctypes.c_uint8),
        ("injecting", ctypes.c_uint8),
        ("coordinate", ctypes.c_uint32),
        ("lookup_ns", ctypes.c_uint32),
        ("win_zones", ctypes.c_uint32),
        ("pulse_serial", ctypes.c_uint32),
        ("fill_count", ctypes.c_uint8),
        ("_pad", ctypes.c_uint8 * 3),
        ("last_trade_pnl", ctypes.c_float),
    ]


from system.ipc.string_diagnostics import (  # noqa: E402
    P1_NULL_TUPLE,
    P1_SOCKET,
    StringPhaseDiag,
    attach_string_diag,
    record_phase1_drop,
    record_phase1_win,
    string_diag_offset,
    decode_string_diag,
)

_STRING_DIAG_BYTES = ctypes.sizeof(StringPhaseDiag)
COCKPIT_SHM_BYTES = (
    ctypes.sizeof(CockpitShmHeader)
    + COCKPIT_SHM_FILL_SLOTS * ctypes.sizeof(CockpitFillRow)
    + _STRING_DIAG_BYTES
)
COCKPIT_SHM_ALLOC_BYTES = max(COCKPIT_SHM_DARWIN_MIN_BYTES, COCKPIT_SHM_BYTES)

_COCKPIT_SHM: shared_memory.SharedMemory | None = None
_COCKPIT_WRITE_SEQ = 0
_STRING_DIAG_VIEW: StringPhaseDiag | None = None


def _normalize_shm_segment_name(name: str) -> str:
    """
    Cross-platform segment id — strip Linux ``/dev/shm/`` paths and leading slashes.

    Darwin ``SharedMemory`` expects a bare name (``ig_agent_v30_shm``); never a
  filesystem path under ``/dev/shm``.
    """
    raw = str(name or "").strip()
    if raw.startswith("/dev/shm/"):
        raw = raw[len("/dev/shm/") :]
    while raw.startswith("/"):
        raw = raw[1:]
    return raw or COCKPIT_SHM_NAME


def _cockpit_shm_namespace_label() -> str:
    seg = _normalize_shm_segment_name(COCKPIT_SHM_NAME)
    if sys.platform == "darwin":
        return f"Darwin-POSIX:{seg}"
    return f"posix-shm:{seg}"


def _close_cockpit_shm_singleton(*, unlink: bool = False) -> None:
    global _COCKPIT_SHM, _STRING_DIAG_VIEW
    if _COCKPIT_SHM is None:
        return
    try:
        _COCKPIT_SHM.close()
    except Exception:
        pass
    if unlink:
        try:
            _COCKPIT_SHM.unlink()
        except Exception:
            pass
    _COCKPIT_SHM = None
    _STRING_DIAG_VIEW = None


def cockpit_shm_map_status() -> dict[str, Any]:
    """Boot/cockpit diagnostic — confirm Darwin namespace mapping without /dev/shm."""
    name = _normalize_shm_segment_name(COCKPIT_SHM_NAME)
    mapped = False
    nbytes = 0
    try:
        probe = shared_memory.SharedMemory(name=name, create=False)
        try:
            mapped = True
            nbytes = len(probe.buf)
        finally:
            probe.close()
    except FileNotFoundError:
        pass
    except OSError:
        pass
    return {
        "platform": sys.platform,
        "segment_name": name,
        "namespace": _cockpit_shm_namespace_label(),
        "alloc_bytes": COCKPIT_SHM_ALLOC_BYTES,
        "layout_bytes": COCKPIT_SHM_BYTES,
        "mapped": mapped,
        "mapped_bytes": nbytes,
    }


def _evict_zombie_cockpit_shm(name: str) -> None:
    """Remove orphaned POSIX segment when publisher PID is dead or restarted."""
    try:
        probe = shared_memory.SharedMemory(name=name, create=False)
    except (FileNotFoundError, OSError):
        return
    try:
        raw = bytes(probe.buf[: ctypes.sizeof(CockpitShmHeader)])
        hdr = CockpitShmHeader.from_buffer_copy(raw)
        if hdr.magic != COCKPIT_SHM_MAGIC:
            try:
                probe.unlink()
            except Exception:
                pass
            return
        from system.ipc.cockpit_shm_passive import pid_is_alive

        pid = int(hdr.agent_pid)
        current = int(os.getpid()) & 0xFFFFFFFF
        stale_pid = not pid_is_alive(pid)
        pid_restart = pid > 0 and pid != current
        if not stale_pid and not pid_restart:
            return
        try:
            from system.engine_log import log_engine

            reason = "publisher dead" if stale_pid else f"pid restart {pid}->{current}"
            log_engine(f"CockpitSHM: evicting stale segment pid={pid} ({reason})")
        except Exception:
            pass
        try:
            probe.unlink()
        except Exception:
            pass
    finally:
        probe.close()


def _attach_cockpit_shm(*, create: bool) -> shared_memory.SharedMemory:
    global _COCKPIT_SHM
    name = _normalize_shm_segment_name(COCKPIT_SHM_NAME)
    size = COCKPIT_SHM_ALLOC_BYTES

    if _COCKPIT_SHM is not None:
        try:
            _ = _COCKPIT_SHM.buf[0]
            return _COCKPIT_SHM
        except (BufferError, OSError, ValueError):
            _close_cockpit_shm_singleton()

    if create:
        _evict_zombie_cockpit_shm(name)
        try:
            seg = shared_memory.SharedMemory(name=name, create=True, size=size)
        except FileExistsError:
            _evict_zombie_cockpit_shm(name)
            try:
                seg = shared_memory.SharedMemory(name=name, create=True, size=size)
            except FileExistsError:
                seg = shared_memory.SharedMemory(name=name, create=False)
                hdr_probe = CockpitShmHeader.from_buffer(seg.buf)
                if int(hdr_probe.agent_pid) != (int(os.getpid()) & 0xFFFFFFFF):
                    seg.close()
                    try:
                        seg.unlink()
                    except Exception:
                        pass
                    seg = shared_memory.SharedMemory(name=name, create=True, size=size)
        except FileNotFoundError:
            seg = shared_memory.SharedMemory(name=name, create=True, size=size)
    else:
        seg = shared_memory.SharedMemory(name=name, create=False)

    _COCKPIT_SHM = seg
    hdr = CockpitShmHeader.from_buffer(seg.buf)
    if hdr.magic != COCKPIT_SHM_MAGIC:
        hdr.magic = COCKPIT_SHM_MAGIC
        hdr.version = COCKPIT_SHM_VERSION
        hdr.header_bytes = ctypes.sizeof(CockpitShmHeader)
        hdr.memory_aligned = 1
    if create:
        try:
            from system.engine_log import log_engine

            log_engine(
                f"CockpitSHM: mapped {_cockpit_shm_namespace_label()} "
                f"alloc={size} layout={COCKPIT_SHM_BYTES} pid={os.getpid()}"
            )
        except Exception:
            pass
    return seg


def _string_diag_view(*, create: bool = True) -> StringPhaseDiag | None:
    global _STRING_DIAG_VIEW
    try:
        seg = _attach_cockpit_shm(create=create)
    except Exception:
        return None
    if _STRING_DIAG_VIEW is None:
        off = string_diag_offset(
            ctypes.sizeof(CockpitShmHeader),
            COCKPIT_SHM_FILL_SLOTS,
            ctypes.sizeof(CockpitFillRow),
        )
        _STRING_DIAG_VIEW = attach_string_diag(seg, off)
    return _STRING_DIAG_VIEW


def _fill_row_offset(slot: int) -> int:
    return ctypes.sizeof(CockpitShmHeader) + slot * ctypes.sizeof(CockpitFillRow)


def _encode_fill_row(buf: memoryview, slot: int, row: dict[str, Any]) -> None:
    off = _fill_row_offset(slot)
    fill = CockpitFillRow.from_buffer(buf, off)
    ts = str(row.get("executed_at") or row.get("closed_at") or "")
    epoch_ms = 0
    if ts:
        try:
            from datetime import datetime

            epoch_ms = int(
                datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000
            )
        except (TypeError, ValueError):
            epoch_ms = 0
    fill.epoch_ms = epoch_ms
    try:
        fill.entry_micro = int(float(row.get("entry") or 0) * 10_000)
    except (TypeError, ValueError):
        fill.entry_micro = 0
    try:
        fill.pnl_cents = int(float(row.get("pnl_gbp") or 0) * 100)
    except (TypeError, ValueError):
        fill.pnl_cents = 0
    fill.epic = str(row.get("epic") or "")[:28].encode("ascii", "replace")
    fill.action = str(row.get("action") or row.get("direction") or "")[:6].encode(
        "ascii", "replace"
    )
    fill.status = str(row.get("status") or "")[:8].encode("ascii", "replace")
    fill.result = str(row.get("result") or "")[:6].encode("ascii", "replace")


def _repair_cockpit_shm_namespace() -> None:
    """Re-publish POSIX name if singleton survived an unlink (external readers lost)."""
    global _COCKPIT_SHM
    if _COCKPIT_SHM is None:
        return
    name = _normalize_shm_segment_name(COCKPIT_SHM_NAME)
    try:
        probe = shared_memory.SharedMemory(name=name, create=False)
        probe.close()
    except (FileNotFoundError, OSError):
        try:
            _COCKPIT_SHM.close()
        except Exception:
            pass
        _COCKPIT_SHM = None


def publish_cockpit_shm(snap: dict[str, Any]) -> None:
    """Publisher — fulfillment refresh thread writes dashboard bytes (off hot path)."""
    global _COCKPIT_WRITE_SEQ
    try:
        _repair_cockpit_shm_namespace()
        seg = _attach_cockpit_shm(create=True)
    except Exception:
        return

    dv = snap.get("data_velocity") or {}
    tun = snap.get("tuning_variables") or {}
    frontier_snap = snap.get("alpha_frontier_tracker") or {}
    last_ft = frontier_snap.get("last") or {}
    frontier = frontier_snap.get("ring") or {}
    traffic = snap.get("traffic_light_hub") or {}
    stages = snap.get("stages") or []

    ticks = int(dv.get("ticks_cached") or snap.get("ticks_cached") or 0)
    live = int(dv.get("live_ram_ticks") or dv.get("watchdog_metric") or 0)
    stall = bool(dv.get("stall_active") or snap.get("trading_paused"))

    vector_density = 0
    for st in stages:
        if st.get("id") == 2:
            vector_density = int(st.get("vector_density") or 0)
            break
    if not vector_density:
        vector_density = int(frontier.get("win_zones") or 0)

    injecting = bool(last_ft.get("injecting"))
    zone = int(last_ft.get("zone") or frontier.get("last_zone") or 0)
    win_zone = (
        last_ft.get("zone_label") == "WIN_ZONE"
        or frontier.get("last_zone_label") == "WIN_ZONE"
        or zone == WIN_ZONE
    )

    if stall:
        valve = VALVE_STALL
    elif injecting:
        valve = VALVE_FIRE
    elif win_zone:
        valve = VALVE_WIN_ZONE
    else:
        valve = VALVE_SCANNING

    hdr = CockpitShmHeader.from_buffer(seg.buf)
    hdr.magic = COCKPIT_SHM_MAGIC
    hdr.version = COCKPIT_SHM_VERSION
    hdr.header_bytes = ctypes.sizeof(CockpitShmHeader)
    hdr.agent_pid = int(os.getpid()) & 0xFFFFFFFF
    hdr.updated_ns = time.perf_counter_ns()
    hdr.ticks_cached = ticks
    hdr.live_ram_ticks = live
    hdr.signal_threshold = float(tun.get("signal_threshold") or 52.5)
    hdr.atr_multiplier = float(tun.get("atr_multiplier") or 2.5)
    hdr.vector_density = vector_density
    hdr.valve_status = valve
    hdr.zone = zone
    hdr.stall_active = 1 if stall else 0
    hdr.memory_aligned = 1 if snap.get("memory_alignment") == "TRUE SYNC" else 0
    hdr.injecting = 1 if injecting else 0
    hdr.coordinate = int(
        traffic.get("matrix", {}).get("coordinate") or frontier.get("last_coordinate") or 0
    )
    e2e = snap.get("e2e_latency_ns") or {}
    hdr.lookup_ns = int(e2e.get("last_ns") or 0)
    hdr.win_zones = int(frontier.get("win_zones") or 0)
    hdr.pulse_serial = int(snap.get("pulse_serial") or 0)

    rows = list(snap.get("performance_rows") or [])[-COCKPIT_FILL_SLOTS:]
    hdr.fill_count = len(rows)
    last_pnl = 0.0
    if rows:
        try:
            last_pnl = float(rows[-1].get("pnl_gbp") or 0)
        except (TypeError, ValueError):
            last_pnl = 0.0
    hdr.last_trade_pnl = last_pnl

    for slot in range(COCKPIT_FILL_SLOTS):
        if slot < len(rows):
            _encode_fill_row(seg.buf, slot, rows[slot])
        else:
            _encode_fill_row(seg.buf, slot, {})

    _COCKPIT_WRITE_SEQ += 1
    hdr.write_seq = _COCKPIT_WRITE_SEQ


def publish_live_probe_cockpit(
    *,
    epic: str,
    direction: str,
    entry: float,
    size: float,
    status: str = "OPEN",
    signature: str = "",
) -> None:
    """Hot-path SHM write — instant fills row for LIVE_PROBING_ALPHA."""
    global _COCKPIT_WRITE_SEQ
    try:
        seg = _attach_cockpit_shm(create=True)
    except Exception:
        return

    from datetime import datetime, timezone

    hdr = CockpitShmHeader.from_buffer(seg.buf)
    hdr.magic = COCKPIT_SHM_MAGIC
    hdr.version = COCKPIT_SHM_VERSION
    hdr.header_bytes = ctypes.sizeof(CockpitShmHeader)
    hdr.agent_pid = int(os.getpid()) & 0xFFFFFFFF
    hdr.updated_ns = time.perf_counter_ns()
    hdr.valve_status = VALVE_FIRE
    hdr.zone = WIN_ZONE
    hdr.injecting = 1
    hdr.coordinate = 0

    row = {
        "executed_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "epic": epic,
        "action": direction,
        "direction": direction,
        "status": status,
        "result": "PROBE",
        "entry": float(entry),
        "pnl_gbp": 0.0,
        "size": float(size),
        "signature": signature,
    }
    _encode_fill_row(seg.buf, 0, row)
    hdr.fill_count = 1
    _COCKPIT_WRITE_SEQ += 1
    hdr.write_seq = _COCKPIT_WRITE_SEQ


def read_cockpit_shm() -> dict[str, Any] | None:
    """Naked reader — attach existing Darwin segment, unpack ctypes registry."""
    name = _normalize_shm_segment_name(COCKPIT_SHM_NAME)
    try:
        seg = shared_memory.SharedMemory(name=name, create=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if getattr(exc, "errno", None) == 2:
            return None
        return None

    try:
        raw = bytes(seg.buf[:COCKPIT_SHM_BYTES])
    finally:
        seg.close()

    hdr = CockpitShmHeader.from_buffer_copy(raw[: ctypes.sizeof(CockpitShmHeader)])
    if hdr.magic != COCKPIT_SHM_MAGIC:
        return None

    fills: list[dict[str, Any]] = []
    count = min(int(hdr.fill_count), COCKPIT_FILL_SLOTS)
    row_size = ctypes.sizeof(CockpitFillRow)
    base = ctypes.sizeof(CockpitShmHeader)
    for slot in range(count):
        chunk = raw[base + slot * row_size : base + (slot + 1) * row_size]
        row = CockpitFillRow.from_buffer_copy(chunk)
        epoch_ms = int(row.epoch_ms)
        ts = ""
        if epoch_ms > 0:
            from datetime import datetime, timezone

            ts = datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            )
        fills.append(
            {
                "executed_at": ts,
                "epic": bytes(row.epic).split(b"\0", 1)[0].decode("ascii", "replace"),
                "action": bytes(row.action).split(b"\0", 1)[0].decode("ascii", "replace"),
                "status": bytes(row.status).split(b"\0", 1)[0].decode("ascii", "replace"),
                "result": bytes(row.result).split(b"\0", 1)[0].decode("ascii", "replace"),
                "entry": float(row.entry_micro) / 10_000.0,
                "pnl_gbp": float(row.pnl_cents) / 100.0,
            }
        )

    valve = int(hdr.valve_status)
    diag_off = string_diag_offset(
        ctypes.sizeof(CockpitShmHeader),
        COCKPIT_SHM_FILL_SLOTS,
        ctypes.sizeof(CockpitFillRow),
    )
    string_diag = decode_string_diag(raw, diag_off)

    payload = {
        "magic": int(hdr.magic),
        "write_seq": int(hdr.write_seq),
        "agent_pid": int(hdr.agent_pid),
        "updated_ns": int(hdr.updated_ns),
        "ticks_cached": int(hdr.ticks_cached),
        "live_ram_ticks": int(hdr.live_ram_ticks),
        "signal_threshold": float(hdr.signal_threshold),
        "atr_multiplier": float(hdr.atr_multiplier),
        "vector_density": int(hdr.vector_density),
        "valve_status": valve,
        "zone": int(hdr.zone),
        "stall_active": bool(hdr.stall_active),
        "memory_aligned": bool(hdr.memory_aligned),
        "injecting": bool(hdr.injecting),
        "coordinate": int(hdr.coordinate),
        "lookup_ns": int(hdr.lookup_ns),
        "win_zones": int(hdr.win_zones),
        "pulse_serial": int(hdr.pulse_serial),
        "last_trade_pnl": float(hdr.last_trade_pnl),
        "performance_rows": fills,
        "memory_alignment": "TRUE SYNC" if hdr.memory_aligned else "WARMING",
    }
    if string_diag:
        payload["string_diag"] = string_diag
    return payload


def reset_cockpit_shm_for_tests() -> None:
    global _COCKPIT_WRITE_SEQ
    _close_cockpit_shm_singleton(unlink=True)
    _COCKPIT_WRITE_SEQ = 0


def reset_alpha_ring_buffer_for_tests() -> None:
    global _RING_SINGLETON
    _RING_SINGLETON = None
    reset_cockpit_shm_for_tests()
