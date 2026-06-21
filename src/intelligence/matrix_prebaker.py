"""
Pre-Baked Alpha Matrix compiler — shadow track (:9199) background job.

Parses the 5-day production tick archive in memory, labels each historical
pattern with True Win / True Loss look-ahead, maps RSI/ATR/Momentum state
vectors to optimized gate thresholds, and binds the table to POSIX shared
memory ``ig_agent_v30_alpha_matrix`` for zero-latency live lookup.
"""

from __future__ import annotations

import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception
from system.paths import project_root

# POSIX segment name (Python SharedMemory — no leading slash)
ALPHA_MATRIX_SHM_NAME = "ig_agent_v30_alpha_matrix"
ALPHA_MATRIX_POSIX_PATH = f"/{ALPHA_MATRIX_SHM_NAME}"

EPIC_SLOTS = 8
RSI_BINS = 32
ATR_BINS = 16
MOM_BINS = 16
DIR_SLOTS = 2
MATRIX_COLS = 8
CELLS_PER_EPIC = RSI_BINS * ATR_BINS * MOM_BINS * DIR_SLOTS
TOTAL_CELLS = EPIC_SLOTS * CELLS_PER_EPIC

# Columns: signal_floor, fitness_floor, ml_floor, win_prob, approved, samples, rsi_anchor, atr_anchor
COL_SIGNAL_FLOOR = 0
COL_FITNESS_FLOOR = 1
COL_ML_FLOOR = 2
COL_WIN_PROB = 3
COL_APPROVED = 4
COL_SAMPLES = 5
COL_RSI_ANCHOR = 6
COL_ATR_ANCHOR = 7

_HEADER_STRUCT = struct.Struct("!QIIIIIIIddQQQQd")
_MAGIC = 0x414C504830  # "ALPH0"
_VERSION = 1
_HEADER_BYTES = _HEADER_STRUCT.size
_MATRIX_BYTES = TOTAL_CELLS * MATRIX_COLS * np.dtype(np.float32).itemsize
_SHM_TOTAL_BYTES = _HEADER_BYTES + _MATRIX_BYTES

EPIC_TO_SLOT: dict[str, int] = {
    "CS.D.CFPGOLD.CFP.IP": 0,
    "IX.D.DOW.IFM.IP": 1,
    "IX.D.NIKKEI.IFM.IP": 2,
    "CS.D.EURUSD.CFD.IP": 3,
    "IX.D.NASDAQ.IFM.IP": 4,
    "IX.D.FTSE.IFM.IP": 5,
}

_COMPILE_LOCK = threading.Lock()
_COMPILER_THREAD: threading.Thread | None = None
_TELEMETRY: dict[str, Any] = {
    "status": "idle",
    "compile_ms": 0.0,
    "memory_bytes": _SHM_TOTAL_BYTES,
    "patterns_scanned": 0,
    "true_wins": 0,
    "true_losses": 0,
    "cells_populated": 0,
    "lookup_hits_live": 0,
    "last_compile_utc": None,
    "ingestion_ticks_per_sec": 0.0,
    "shm_name": ALPHA_MATRIX_SHM_NAME,
    "posix_path": ALPHA_MATRIX_POSIX_PATH,
}
_INGEST_TICK_TIMES: list[float] = []


@dataclass(frozen=True)
class CompileReport:
    compile_ms: float
    memory_bytes: int
    patterns_scanned: int
    true_wins: int
    true_losses: int
    cells_populated: int


def archive_path() -> Path:
    bundled = (
        project_root()
        / "src"
        / "simulation"
        / "data"
        / "production_5day_archive.jsonl"
    )
    return bundled if bundled.is_file() else bundled


def epic_slot(epic: str) -> int:
    return EPIC_TO_SLOT.get(str(epic or "").strip(), 7)


def quantize_rsi(rsi: float) -> int:
    return int(min(RSI_BINS - 1, max(0, (float(rsi) / 100.0) * RSI_BINS)))


def quantize_atr(atr: float, *, epic: str) -> int:
    from intelligence.matrix_backtuner import DEFAULT_EPIC_STOP

    stop = float(DEFAULT_EPIC_STOP.get(epic, 30.0) or 30.0)
    ratio = float(atr) / max(0.5, stop)
    return int(min(ATR_BINS - 1, max(0, ratio * ATR_BINS)))


def quantize_momentum(momentum: float) -> int:
    clamped = max(-0.02, min(0.02, float(momentum)))
    normalized = (clamped + 0.02) / 0.04
    return int(min(MOM_BINS - 1, max(0, normalized * MOM_BINS)))


def matrix_cell_index(
    *,
    epic_id: int,
    direction: str,
    rsi_q: int,
    atr_q: int,
    mom_q: int,
) -> int:
    dir_slot = 0 if str(direction).upper() == "BUY" else 1
    epic_id = int(min(EPIC_SLOTS - 1, max(0, epic_id)))
    offset = (
        rsi_q * (ATR_BINS * MOM_BINS)
        + atr_q * MOM_BINS
        + mom_q
    )
    return epic_id * CELLS_PER_EPIC + dir_slot * (RSI_BINS * ATR_BINS * MOM_BINS) + offset


class AlphaMatrixSegment:
    """Fixed-size POSIX shared memory binding for the pre-baked matrix."""

    def __init__(self, *, create: bool = False) -> None:
        from multiprocessing import shared_memory

        self._create = create
        self._shm: shared_memory.SharedMemory | None = None
        self._matrix: np.ndarray | None = None
        self._attach(create=create)

    def _attach(self, *, create: bool) -> None:
        from multiprocessing import shared_memory

        if self._shm is not None:
            return
        try:
            if create:
                try:
                    existing = shared_memory.SharedMemory(
                        name=ALPHA_MATRIX_SHM_NAME, create=False
                    )
                    existing.close()
                    existing.unlink()
                except FileNotFoundError:
                    pass
                self._shm = shared_memory.SharedMemory(
                    name=ALPHA_MATRIX_SHM_NAME,
                    create=True,
                    size=_SHM_TOTAL_BYTES,
                )
                log_engine(
                    f"AlphaMatrixSegment: created shm={ALPHA_MATRIX_SHM_NAME} "
                    f"bytes={_SHM_TOTAL_BYTES}"
                )
            else:
                self._shm = shared_memory.SharedMemory(
                    name=ALPHA_MATRIX_SHM_NAME, create=False
                )
        except FileNotFoundError:
            if not create:
                raise
            self._shm = shared_memory.SharedMemory(
                name=ALPHA_MATRIX_SHM_NAME,
                create=True,
                size=_SHM_TOTAL_BYTES,
            )
        assert self._shm is not None
        self._matrix = np.ndarray(
            (TOTAL_CELLS, MATRIX_COLS),
            dtype=np.float32,
            buffer=self._shm.buf,
            offset=_HEADER_BYTES,
        )

    @property
    def matrix(self) -> np.ndarray:
        if self._matrix is None:
            raise RuntimeError("alpha matrix not attached")
        return self._matrix

    def write_header(
        self,
        *,
        compile_ms: float,
        patterns_scanned: int,
        true_wins: int,
        true_losses: int,
        lookup_hits: int = 0,
    ) -> None:
        if self._shm is None:
            return
        packed = _HEADER_STRUCT.pack(
            _MAGIC,
            _VERSION,
            EPIC_SLOTS,
            RSI_BINS,
            ATR_BINS,
            MOM_BINS,
            DIR_SLOTS,
            MATRIX_COLS,
            float(compile_ms),
            float(_SHM_TOTAL_BYTES),
            int(patterns_scanned),
            int(true_wins),
            int(true_losses),
            int(lookup_hits),
            time.time(),
        )
        self._shm.buf[:_HEADER_BYTES] = packed

    def read_header(self) -> dict[str, Any]:
        if self._shm is None:
            return {}
        raw = bytes(self._shm.buf[:_HEADER_BYTES])
        if len(raw) < _HEADER_STRUCT.size:
            return {}
        (
            magic,
            version,
            epic_slots,
            rsi_bins,
            atr_bins,
            mom_bins,
            dir_slots,
            cols,
            compile_ms,
            memory_bytes,
            patterns,
            wins,
            losses,
            lookup_hits,
            compiled_epoch,
        ) = _HEADER_STRUCT.unpack(raw)
        return {
            "magic": magic,
            "version": version,
            "epic_slots": epic_slots,
            "rsi_bins": rsi_bins,
            "atr_bins": atr_bins,
            "mom_bins": mom_bins,
            "dir_slots": dir_slots,
            "cols": cols,
            "compile_ms": compile_ms,
            "memory_bytes": int(memory_bytes),
            "patterns_scanned": int(patterns),
            "true_wins": int(wins),
            "true_losses": int(losses),
            "lookup_hits_live": int(lookup_hits),
            "compiled_epoch": compiled_epoch,
            "ready": magic == _MAGIC and version == _VERSION,
        }

    def increment_lookup_hits(self) -> int:
        header = self.read_header()
        if not header.get("ready"):
            return 0
        hits = int(header.get("lookup_hits_live") or 0) + 1
        self.write_header(
            compile_ms=float(header.get("compile_ms") or 0),
            patterns_scanned=int(header.get("patterns_scanned") or 0),
            true_wins=int(header.get("true_wins") or 0),
            true_losses=int(header.get("true_losses") or 0),
            lookup_hits=hits,
        )
        return hits

    def close(self, *, unlink: bool = False) -> None:
        global _SHM_MAPPED
        if self._shm is None:
            _SHM_MAPPED = False
            return
        try:
            self._shm.close()
            if unlink:
                try:
                    self._shm.unlink()
                except FileNotFoundError:
                    pass
        except Exception as exc:
            log_guarded_exception("alpha_matrix_shm_close", exc)
        finally:
            self._shm = None
            self._matrix = None
            _SHM_MAPPED = False

    @staticmethod
    def unlink_segment() -> None:
        from multiprocessing import shared_memory

        try:
            existing = shared_memory.SharedMemory(name=ALPHA_MATRIX_SHM_NAME, create=False)
            existing.close()
            existing.unlink()
        except FileNotFoundError:
            pass


_SEGMENT_SINGLETON: AlphaMatrixSegment | None = None
_SEGMENT_LOCK = threading.Lock()
_SHM_MAPPED = False
_LAST_LOOKUP_LATENCY_US: float = 0.0
_LOOKUP_LATENCY_SAMPLES: list[float] = []


def get_alpha_matrix_segment(*, create: bool = False) -> AlphaMatrixSegment:
    global _SEGMENT_SINGLETON, _SHM_MAPPED
    with _SEGMENT_LOCK:
        if _SEGMENT_SINGLETON is None:
            _SEGMENT_SINGLETON = AlphaMatrixSegment(create=create)
            _SHM_MAPPED = True
        return _SEGMENT_SINGLETON


def alpha_matrix_mapped() -> bool:
    with _SEGMENT_LOCK:
        return bool(_SHM_MAPPED and _SEGMENT_SINGLETON is not None and _SEGMENT_SINGLETON._matrix is not None)


def force_unmap_alpha_matrix() -> bool:
    """OS-level unlink — live naked pointers fail on next lookup."""
    global _SEGMENT_SINGLETON, _SHM_MAPPED
    with _SEGMENT_LOCK:
        if _SEGMENT_SINGLETON is not None:
            _SEGMENT_SINGLETON.close(unlink=True)
            _SEGMENT_SINGLETON = None
        else:
            AlphaMatrixSegment.unlink_segment()
        _SHM_MAPPED = False
    return True


def record_lookup_latency_us(latency_us: float) -> None:
    global _LAST_LOOKUP_LATENCY_US, _LOOKUP_LATENCY_SAMPLES
    _LAST_LOOKUP_LATENCY_US = float(latency_us)
    _LOOKUP_LATENCY_SAMPLES.append(float(latency_us))
    if len(_LOOKUP_LATENCY_SAMPLES) > 200:
        del _LOOKUP_LATENCY_SAMPLES[:-200]


def lookup_latency_profile() -> dict[str, float]:
    samples = list(_LOOKUP_LATENCY_SAMPLES)
    if not samples:
        return {"last_us": _LAST_LOOKUP_LATENCY_US, "p50_us": 0.0, "p99_us": 0.0}
    ordered = sorted(samples)
    p50 = ordered[len(ordered) // 2]
    p99 = ordered[max(0, int(len(ordered) * 0.99) - 1)]
    return {
        "last_us": _LAST_LOOKUP_LATENCY_US,
        "p50_us": float(p50),
        "p99_us": float(p99),
    }


def _optimized_floors(cfg: dict[str, Any]) -> tuple[float, float, float]:
    from intelligence.matrix_backtuner import resolve_floor_bases

    bases = resolve_floor_bases(cfg)
    signal_floor = float(bases.signal_confidence_pct)
    fitness_floor = float(bases.environment_fitness_pct)
    ml_floor = float(bases.ml_veto_probability)
    try:
        report_path = project_root() / "src" / "data" / "matrix_backtuner_report.json"
        if report_path.is_file():
            import json

            raw = json.loads(report_path.read_text(encoding="utf-8"))
            best = raw.get("best_candidate") or {}
            signal_floor = float(best.get("signal_confidence_floor_pct") or signal_floor)
            fitness_floor = float(best.get("environment_fitness_floor_pct") or fitness_floor)
            ml_floor = float(best.get("ml_veto_floor_probability") or ml_floor)
    except Exception:
        pass
    return signal_floor, fitness_floor, ml_floor


def compile_prebaked_alpha_matrix(
    *,
    archive: Path | None = None,
    stride: int = 6,
) -> CompileReport:
    """Full in-memory archive compile → shared-memory matrix bind."""
    from intelligence.matrix_backtuner import (
        MAX_FORWARD_TICKS,
        _archive_features_at,
        _build_epic_feature_cache,
        _load_merged_config,
        _reward_multiple,
        _simulated_environment_fitness,
        _stop_and_tp,
        load_archive_tapes,
        resolve_first_touch_outcome,
    )

    t0 = time.perf_counter()
    path = archive or archive_path()
    cfg = _load_merged_config()
    reward_multiple = _reward_multiple(cfg)
    signal_floor, fitness_floor, ml_floor = _optimized_floors(cfg)
    tapes = load_archive_tapes(path)

    segment = get_alpha_matrix_segment(create=True)
    matrix = segment.matrix
    matrix.fill(0.0)

    patterns = 0
    wins = 0
    losses = 0
    populated = 0

    for epic, tape in tapes.items():
        slot = epic_slot(epic)
        if tape.mids.size < 64:
            continue
        stop_pts, _ = _stop_and_tp(
            epic=epic, atr=0.0, cfg=cfg, reward_multiple=reward_multiple
        )
        cache = _build_epic_feature_cache(tape, stop_points=stop_pts)
        limit = max(32, tape.mids.size - MAX_FORWARD_TICKS - 1)
        for idx in range(32, limit, max(1, int(stride))):
            patterns += 1
            _conf, rsi, atr = _archive_features_at(tape, idx, stop_points=stop_pts)
            if idx > 0 and tape.mids[idx - 1] > 0:
                momentum = (float(tape.mids[idx]) - float(tape.mids[idx - 1])) / float(
                    tape.mids[idx - 1]
                )
            else:
                momentum = 0.0
            direction = "BUY" if momentum >= 0 else "SELL"
            stop_pts, tp_pts = _stop_and_tp(
                epic=epic, atr=atr, cfg=cfg, reward_multiple=reward_multiple
            )
            outcome = resolve_first_touch_outcome(
                direction,
                tape.mids,
                idx,
                stop_pts=stop_pts,
                tp_pts=tp_pts,
            )
            if outcome == "true_win":
                wins += 1
            elif outcome == "true_loss":
                losses += 1

            rsi_q = quantize_rsi(rsi)
            atr_q = quantize_atr(atr, epic=epic)
            mom_q = quantize_momentum(momentum)
            cell_idx = matrix_cell_index(
                epic_id=slot,
                direction=direction,
                rsi_q=rsi_q,
                atr_q=atr_q,
                mom_q=mom_q,
            )
            row = matrix[cell_idx]
            samples = float(row[COL_SAMPLES]) + 1.0
            approved = 1.0 if outcome == "true_win" else 0.0
            if outcome == "true_win" or samples <= 1.0:
                row[COL_SIGNAL_FLOOR] = np.float32(signal_floor)
                row[COL_FITNESS_FLOOR] = np.float32(fitness_floor)
                row[COL_ML_FLOOR] = np.float32(ml_floor)
                row[COL_APPROVED] = np.float32(approved)
                row[COL_RSI_ANCHOR] = np.float32(rsi)
                row[COL_ATR_ANCHOR] = np.float32(atr)
            win_prob = (float(row[COL_WIN_PROB]) * (samples - 1.0) + approved) / samples
            row[COL_WIN_PROB] = np.float32(win_prob)
            row[COL_SAMPLES] = np.float32(samples)
            if samples == 1.0:
                populated += 1

    compile_ms = (time.perf_counter() - t0) * 1000.0
    header = segment.read_header()
    segment.write_header(
        compile_ms=compile_ms,
        patterns_scanned=patterns,
        true_wins=wins,
        true_losses=losses,
        lookup_hits=int(header.get("lookup_hits_live") or 0),
    )

    report = CompileReport(
        compile_ms=round(compile_ms, 2),
        memory_bytes=_SHM_TOTAL_BYTES,
        patterns_scanned=patterns,
        true_wins=wins,
        true_losses=losses,
        cells_populated=populated,
    )
    _update_telemetry(report, status="ready")
    global _SHM_MAPPED
    _SHM_MAPPED = True
    try:
        from system.ipc.ring_buffer import get_alpha_ring_buffer, unified_engine_active

        if unified_engine_active():
            get_alpha_ring_buffer().write_matrix_generation(
                segment.matrix.copy(),
                vector_density=populated,
            )
    except Exception:
        pass
    try:
        from system.ipc.shm_watchdog import emit_compiler_pulse

        emit_compiler_pulse()
    except Exception:
        pass
    log_engine(
        "AlphaMatrixPrebaker: compiled "
        f"patterns={patterns} wins={wins} losses={losses} "
        f"cells={populated} ms={compile_ms:.1f} ram={_SHM_TOTAL_BYTES}"
    )
    return report


def _update_telemetry(report: CompileReport, *, status: str) -> None:
    global _TELEMETRY
    _TELEMETRY = {
        **_TELEMETRY,
        "status": status,
        "compile_ms": report.compile_ms,
        "memory_bytes": report.memory_bytes,
        "memory_mb": round(report.memory_bytes / (1024 * 1024), 3),
        "patterns_scanned": report.patterns_scanned,
        "true_wins": report.true_wins,
        "true_losses": report.true_losses,
        "cells_populated": report.cells_populated,
        "last_compile_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def record_ingestion_tick() -> None:
    """Shadow track volumetrics — ticks observed during archive-style ingestion."""
    global _INGEST_TICK_TIMES
    now = time.time()
    _INGEST_TICK_TIMES.append(now)
    cutoff = now - 60.0
    _INGEST_TICK_TIMES = [t for t in _INGEST_TICK_TIMES if t >= cutoff]
    if _INGEST_TICK_TIMES:
        _TELEMETRY["ingestion_ticks_per_sec"] = round(
            len(_INGEST_TICK_TIMES) / max(1.0, now - _INGEST_TICK_TIMES[0]),
            2,
        )


def _compiler_loop(*, interval_sec: float) -> None:
    while True:
        try:
            with _COMPILE_LOCK:
                compile_prebaked_alpha_matrix()
        except Exception as exc:
            _TELEMETRY["status"] = "error"
            _TELEMETRY["last_error"] = str(exc)
            log_guarded_exception("alpha_matrix_compiler", exc)
        time.sleep(max(30.0, float(interval_sec)))


def start_alpha_matrix_compiler_async(*, interval_sec: float = 300.0) -> None:
    """Shadow (:9199) background compiler — non-blocking to the hot path."""
    global _COMPILER_THREAD
    if _COMPILER_THREAD is not None and _COMPILER_THREAD.is_alive():
        return

    def _bootstrap() -> None:
        try:
            with _COMPILE_LOCK:
                compile_prebaked_alpha_matrix()
        except Exception as exc:
            log_guarded_exception("alpha_matrix_bootstrap", exc)
        _compiler_loop(interval_sec=interval_sec)

    _COMPILER_THREAD = threading.Thread(
        target=_bootstrap,
        name="alpha-matrix-prebaker",
        daemon=True,
    )
    _COMPILER_THREAD.start()
    log_engine("AlphaMatrixPrebaker: async compiler thread started")


def matrix_compiler_telemetry() -> dict[str, Any]:
    """Read-only compiler stats for decoupled UI cache."""
    return dict(_TELEMETRY)


def alpha_matrix_dashboard_payload() -> dict[str, Any]:
    header: dict[str, Any] = {}
    mapped = alpha_matrix_mapped()
    try:
        if mapped:
            segment = get_alpha_matrix_segment(create=False)
            header = segment.read_header()
            _TELEMETRY["lookup_hits_live"] = int(header.get("lookup_hits_live") or 0)
            if header.get("ready"):
                _TELEMETRY["status"] = "ready"
                _TELEMETRY["compile_ms"] = float(header.get("compile_ms") or 0)
                _TELEMETRY["memory_bytes"] = int(header.get("memory_bytes") or _SHM_TOTAL_BYTES)
                _TELEMETRY["memory_mb"] = round(_TELEMETRY["memory_bytes"] / (1024 * 1024), 3)
    except Exception:
        mapped = False

    watchdog: dict[str, Any] = {}
    try:
        from system.ipc.shm_watchdog import watchdog_telemetry

        watchdog = watchdog_telemetry()
    except Exception:
        pass

    latency = lookup_latency_profile()
    shm_state = "mapped" if mapped else "unmapped"
    if watchdog.get("shm_state"):
        shm_state = str(watchdog.get("shm_state"))

    return {
        "mode": "ALPHA_MATRIX_TERMINAL",
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "active_cache_volume": int(_TELEMETRY.get("cells_populated") or header.get("patterns_scanned") or 0),
        "hardware_ring_buffer": {
            "state": shm_state,
            "posix_path": ALPHA_MATRIX_POSIX_PATH,
            "pulse_seq": int(watchdog.get("last_seq") or 0),
            "miss_cycles": int(watchdog.get("miss_cycles") or 0),
            "unmap_count": int(watchdog.get("unmap_count") or 0),
        },
        "processing_latency_us": latency,
        "compilation": {
            "status": _TELEMETRY.get("status"),
            "cells_populated": _TELEMETRY.get("cells_populated"),
            "patterns_scanned": _TELEMETRY.get("patterns_scanned"),
            "memory_mb": _TELEMETRY.get("memory_mb"),
            "header": header,
        },
    }


def reset_alpha_matrix_for_tests() -> None:
    global _SEGMENT_SINGLETON, _COMPILER_THREAD, _TELEMETRY, _INGEST_TICK_TIMES, _SHM_MAPPED
    with _SEGMENT_LOCK:
        if _SEGMENT_SINGLETON is not None:
            _SEGMENT_SINGLETON.close(unlink=True)
            _SEGMENT_SINGLETON = None
        else:
            AlphaMatrixSegment.unlink_segment()
        _SHM_MAPPED = False
    _COMPILER_THREAD = None
    _INGEST_TICK_TIMES = []
    _TELEMETRY["status"] = "idle"
