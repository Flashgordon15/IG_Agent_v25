"""
Pre-Baked Alpha Matrix compiler — shadow track (:9199) background job.

Parses the 5-day production tick archive in memory, labels each historical
pattern with True Win / True Loss look-ahead, maps RSI/ATR/Momentum state
vectors to optimized gate thresholds, and binds the table to POSIX shared
memory ``ig_agent_v30_alpha_matrix`` for zero-latency live lookup.
"""

from __future__ import annotations

import os
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
# macOS SharedMemory names must be <=30 chars (IPC_NAME_MAX); scoped dual names
# like ig_agent_v30_alpha_matrix_Z6BAH4 exceed the limit and fail with EFNAMETOOLONG.
_MACOS_SHM_NAME_MAX = 30
_KNOWN_ACCOUNT_SHM_TAGS = {
    "Z6BAH4": "h4",
    "Z6BAH3": "h3",
}


def _engine_scoped_alpha_suffix() -> str | None:
    """Dual-port twins must not share one alpha-matrix segment or publisher marker."""
    if os.environ.get("IG_V32_DUAL_PORT", "").strip() != "1":
        return None
    account = os.environ.get("IG_ACCOUNT_ID", "").strip().upper()
    if account:
        return account
    port = os.environ.get("IG_API_PORT", os.environ.get("PORT", "")).strip()
    if port.isdigit():
        return f"p{port}"
    origin = os.environ.get("IG_ENGINE_ORIGIN", "").strip().upper()
    if origin:
        return origin.lower()
    return None


def _shm_tag_for_scope(scope: str) -> str:
    """Compact tag for POSIX SHM segment names (filesystem markers may stay verbose)."""
    upper = scope.strip().upper()
    if upper in _KNOWN_ACCOUNT_SHM_TAGS:
        return _KNOWN_ACCOUNT_SHM_TAGS[upper]
    if upper.startswith("P") and upper[1:].isdigit():
        return upper[-2:]
    if len(upper) <= 4:
        return upper.lower()
    return upper[-2:].lower()


def resolve_alpha_matrix_shm_name() -> str:
    suffix = _engine_scoped_alpha_suffix()
    if suffix:
        tag = _shm_tag_for_scope(suffix)
        name = f"{ALPHA_MATRIX_SHM_NAME}_{tag}"
        if len(name) > _MACOS_SHM_NAME_MAX:
            trim = _MACOS_SHM_NAME_MAX - len(tag) - 1
            name = f"{ALPHA_MATRIX_SHM_NAME[:trim]}_{tag}"
        return name
    return ALPHA_MATRIX_SHM_NAME


def resolve_alpha_matrix_posix_path() -> str:
    return f"/{resolve_alpha_matrix_shm_name()}"


ALPHA_MATRIX_POSIX_PATH = resolve_alpha_matrix_posix_path()


def alpha_matrix_publisher_marker_path() -> Path:
    from system.paths import data_dir

    suffix = _engine_scoped_alpha_suffix()
    if suffix:
        return data_dir() / "state" / f"alpha_matrix_publisher_{suffix}.pid"
    return data_dir() / "state" / "alpha_matrix_publisher.pid"


def should_publish_alpha_matrix() -> bool:
    """Dual-port: only the CFD orchestrator twin compiles/publishes alpha SHM."""
    if os.environ.get("IG_V32_DUAL_PORT", "").strip() != "1":
        return True
    return os.environ.get("IG_AGENT_ORCHESTRATOR", "").strip() == "1"

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
    "CS.D.CRUDE.CFD.IP": 6,
    "IX.D.DAX.IFM.IP": 7,
}

SLOT_TO_EPIC: dict[int, str] = {v: k for k, v in EPIC_TO_SLOT.items()}

# Night-matrix streaming epics — sparse archive cells forward-filled along mom_q.
FFILL_STREAMING_EPICS: frozenset[str] = frozenset(
    {
        "CS.D.CFPGOLD.CFP.IP",
        "IX.D.DOW.IFM.IP",
        "IX.D.NIKKEI.IFM.IP",
        "CS.D.EURUSD.CFD.IP",
        "CS.D.CRUDE.CFD.IP",
        "IX.D.FTSE.IFM.IP",
        "IX.D.DAX.IFM.IP",
    }
)

# Lightstreamer latency gap — Nikkei / EURUSD require ffill + bfill population.
LATENCY_PACKET_FFILL_EPICS: frozenset[str] = frozenset(
    {
        "IX.D.NIKKEI.IFM.IP",
        "CS.D.EURUSD.CFD.IP",
    }
)

_COMPILE_LOCK = threading.RLock()
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


def _matrix_parts_from_cell_index(cell_idx: int) -> tuple[int, int, int, int, int]:
    """Return (epic_id, dir_slot, rsi_q, atr_q, mom_q) for a flat cell index."""
    epic_id = int(cell_idx) // CELLS_PER_EPIC
    remainder = int(cell_idx) % CELLS_PER_EPIC
    dir_stride = RSI_BINS * ATR_BINS * MOM_BINS
    dir_slot = remainder // dir_stride
    offset = remainder % dir_stride
    rsi_q = offset // (ATR_BINS * MOM_BINS)
    atr_mom = offset % (ATR_BINS * MOM_BINS)
    atr_q = atr_mom // MOM_BINS
    mom_q = atr_mom % MOM_BINS
    return epic_id, dir_slot, rsi_q, atr_q, mom_q


def default_matrix_fallback_row(
    *,
    signal_floor: float = 43.2,
    fitness_floor: float = 43.2,
    ml_floor: float = 0.40,
    win_prob: float = 0.55,
) -> np.ndarray:
    """Cold-start viable cell when archive compile has not yet populated SHM."""
    row = np.zeros(MATRIX_COLS, dtype=np.float32)
    row[COL_SIGNAL_FLOOR] = np.float32(signal_floor)
    row[COL_FITNESS_FLOOR] = np.float32(fitness_floor)
    row[COL_ML_FLOOR] = np.float32(ml_floor)
    row[COL_WIN_PROB] = np.float32(win_prob)
    row[COL_APPROVED] = np.float32(1.0)
    row[COL_SAMPLES] = np.float32(1.0)
    row[COL_RSI_ANCHOR] = np.float32(50.0)
    row[COL_ATR_ANCHOR] = np.float32(1.5)
    return row


def _epic_slot_has_samples(matrix: np.ndarray, epic_id: int) -> bool:
    start = int(epic_id) * CELLS_PER_EPIC
    end = start + CELLS_PER_EPIC
    if end > matrix.shape[0]:
        return False
    return bool(np.any(matrix[start:end, COL_SAMPLES] > 0.0))


def matrix_row_with_streaming_ffill(
    matrix: np.ndarray,
    cell_idx: int,
    *,
    epic: str | None = None,
) -> np.ndarray:
    """
    Forward-fill fallback for Nikkei / EURUSD streaming matrix slices.

    Walks mom_q → atr_q → rsi_q within the epic slot (pandas ``ffill`` semantics).
    """
    row = matrix[int(cell_idx)]
    if float(row[COL_SAMPLES]) > 0.0:
        return row
    epic_name = str(epic or "").strip()
    if not epic_name:
        epic_id, _, _, _, _ = _matrix_parts_from_cell_index(cell_idx)
        epic_name = SLOT_TO_EPIC.get(epic_id, "")
    if epic_name not in FFILL_STREAMING_EPICS:
        return row

    epic_id, dir_slot, rsi_q, atr_q, mom_q = _matrix_parts_from_cell_index(cell_idx)
    for mq in range(mom_q - 1, -1, -1):
        idx = matrix_cell_index(
            epic_id=epic_id,
            direction="BUY" if dir_slot == 0 else "SELL",
            rsi_q=rsi_q,
            atr_q=atr_q,
            mom_q=mq,
        )
        candidate = matrix[idx]
        if float(candidate[COL_SAMPLES]) > 0.0:
            return candidate
    for aq in range(atr_q - 1, -1, -1):
        for mq in range(MOM_BINS - 1, -1, -1):
            idx = matrix_cell_index(
                epic_id=epic_id,
                direction="BUY" if dir_slot == 0 else "SELL",
                rsi_q=rsi_q,
                atr_q=aq,
                mom_q=mq,
            )
            candidate = matrix[idx]
            if float(candidate[COL_SAMPLES]) > 0.0:
                return candidate
    for rq in range(rsi_q - 1, -1, -1):
        for aq in range(ATR_BINS - 1, -1, -1):
            for mq in range(MOM_BINS - 1, -1, -1):
                idx = matrix_cell_index(
                    epic_id=epic_id,
                    direction="BUY" if dir_slot == 0 else "SELL",
                    rsi_q=rq,
                    atr_q=aq,
                    mom_q=mq,
                )
                candidate = matrix[idx]
                if float(candidate[COL_SAMPLES]) > 0.0:
                    return candidate
    if epic_name in FFILL_STREAMING_EPICS:
        slot = epic_slot(epic_name) if epic_name else epic_id
        if not _epic_slot_has_samples(matrix, slot):
            return default_matrix_fallback_row()
    return row


def apply_streaming_ffill_to_matrix(matrix: np.ndarray) -> int:
    """In-place ffill (+ bfill for latency epics) — returns cells filled."""
    sanitize_matrix_nan_inf(matrix)
    filled = 0
    for epic in FFILL_STREAMING_EPICS:
        filled += _apply_slot_ffill(matrix, epic=epic)
    for epic in LATENCY_PACKET_FFILL_EPICS:
        filled += _apply_slot_bfill(matrix, epic=epic)
    return filled


def _bootstrap_empty_epic_slots(matrix: np.ndarray) -> int:
    """Seed wholly empty night-matrix slots so WebKit never reads NaN/zero blocks."""
    seeded = 0
    fallback = default_matrix_fallback_row()
    for epic in FFILL_STREAMING_EPICS:
        slot = epic_slot(epic)
        if _epic_slot_has_samples(matrix, slot):
            continue
        for dir_slot in (0, 1):
            direction = "BUY" if dir_slot == 0 else "SELL"
            for rsi_q in range(RSI_BINS):
                for atr_q in range(ATR_BINS):
                    for mom_q in range(MOM_BINS):
                        idx = matrix_cell_index(
                            epic_id=slot,
                            direction=direction,
                            rsi_q=rsi_q,
                            atr_q=atr_q,
                            mom_q=mom_q,
                        )
                        matrix[idx] = fallback
                        seeded += 1
    return seeded


def calibrate_live_tick_features(
    epic: str,
    rsi: float,
    atr: float,
    momentum: float,
    *,
    matrix: np.ndarray | None = None,
) -> tuple[float, float, float, dict[str, Any]]:
    """V2 drift calibration — re-align live features before alpha / ML inference."""
    try:
        from platform_v2 import platform_v2_enabled

        if not platform_v2_enabled():
            return float(rsi), float(atr), float(momentum), {}
        from platform_v2.feature_drift_calibration import calibrate_live_features

        result = calibrate_live_features(
            epic=str(epic or ""),
            rsi=float(rsi),
            atr=float(atr),
            momentum=float(momentum),
            matrix=matrix,
        )
        meta = {
            "drifted": result.drifted,
            "max_z": result.max_z,
            "scale_multiplier": result.scale_multiplier,
            **result.details,
        }
        return result.rsi, result.atr, result.momentum, meta
    except Exception as exc:
        log_guarded_exception("matrix_prebaker_drift", exc)
        return float(rsi), float(atr), float(momentum), {}


def secure_fill_matrix_update(
    matrix: np.ndarray,
    *,
    prior: np.ndarray | None = None,
) -> int:
    """
    Secure matrix buffer fill — NaN scrub, prior-generation restore, ffill, cold bootstrap.

    When a REST polling slice returns empty metrics, forward-fill along mom_q and
    restore the last compiled generation for still-empty cells.
    """
    sanitize_matrix_nan_inf(matrix)
    restored = 0
    if prior is not None and prior.shape == matrix.shape:
        empty_mask = matrix[:, COL_SAMPLES] <= 0.0
        if np.any(empty_mask):
            matrix[empty_mask] = prior[empty_mask]
            restored = int(np.sum(empty_mask))
    filled = apply_streaming_ffill_to_matrix(matrix)
    seeded = _bootstrap_empty_epic_slots(matrix)
    return int(filled + restored + seeded)


def sanitize_matrix_nan_inf(matrix: np.ndarray) -> None:
    """Zero NaN/inf cells — prevents NaN blocks on live packet loss."""
    np.nan_to_num(matrix, copy=False, nan=0.0, posinf=0.0, neginf=0.0)


def _apply_slot_ffill(matrix: np.ndarray, *, epic: str) -> int:
    filled = 0
    slot = epic_slot(epic)
    for dir_slot in (0, 1):
        direction = "BUY" if dir_slot == 0 else "SELL"
        for rsi_q in range(RSI_BINS):
            for atr_q in range(ATR_BINS):
                last_row: np.ndarray | None = None
                for mom_q in range(MOM_BINS):
                    idx = matrix_cell_index(
                        epic_id=slot,
                        direction=direction,
                        rsi_q=rsi_q,
                        atr_q=atr_q,
                        mom_q=mom_q,
                    )
                    row = matrix[idx]
                    if float(row[COL_SAMPLES]) > 0.0:
                        last_row = row
                        continue
                    if last_row is not None:
                        matrix[idx] = last_row
                        filled += 1
    return filled


def _apply_slot_bfill(matrix: np.ndarray, *, epic: str) -> int:
    """Backward-fill along mom_q (pandas ``bfill``) after forward pass."""
    filled = 0
    slot = epic_slot(epic)
    for dir_slot in (0, 1):
        direction = "BUY" if dir_slot == 0 else "SELL"
        for rsi_q in range(RSI_BINS):
            for atr_q in range(ATR_BINS):
                next_row: np.ndarray | None = None
                for mom_q in range(MOM_BINS - 1, -1, -1):
                    idx = matrix_cell_index(
                        epic_id=slot,
                        direction=direction,
                        rsi_q=rsi_q,
                        atr_q=atr_q,
                        mom_q=mom_q,
                    )
                    row = matrix[idx]
                    if float(row[COL_SAMPLES]) > 0.0:
                        next_row = row
                        continue
                    if next_row is not None:
                        matrix[idx] = next_row
                        filled += 1
    return filled


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
        shm_name = resolve_alpha_matrix_shm_name()
        try:
            if create:
                try:
                    existing = shared_memory.SharedMemory(
                        name=shm_name, create=False
                    )
                    existing.close()
                    existing.unlink()
                except FileNotFoundError:
                    pass
                self._shm = shared_memory.SharedMemory(
                    name=shm_name,
                    create=True,
                    size=_SHM_TOTAL_BYTES,
                )
                log_engine(
                    f"AlphaMatrixSegment: created shm={shm_name} "
                    f"bytes={_SHM_TOTAL_BYTES}"
                )
            else:
                self._shm = shared_memory.SharedMemory(
                    name=shm_name, create=False
                )
        except FileNotFoundError:
            if not create:
                raise
            self._shm = shared_memory.SharedMemory(
                name=shm_name,
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
            existing = shared_memory.SharedMemory(
                name=resolve_alpha_matrix_shm_name(), create=False
            )
            existing.close()
            existing.unlink()
        except FileNotFoundError:
            pass


_SEGMENT_SINGLETON: AlphaMatrixSegment | None = None
_SEGMENT_LOCK = threading.RLock()
_SHM_MAPPED = False
_LAST_LOOKUP_LATENCY_US: float = 0.0
_LOOKUP_LATENCY_SAMPLES: list[float] = []


def get_alpha_matrix_segment(*, create: bool = False) -> AlphaMatrixSegment:
    global _SEGMENT_SINGLETON, _SHM_MAPPED
    with _SEGMENT_LOCK:
        if create:
            flush_stale_alpha_matrix_shm()
        if _SEGMENT_SINGLETON is None:
            _SEGMENT_SINGLETON = AlphaMatrixSegment(create=create)
            _SHM_MAPPED = True
        return _SEGMENT_SINGLETON


def flush_stale_alpha_matrix_shm(*, current_pid: int | None = None) -> bool:
    """Unbind alpha-matrix POSIX segment when publisher PID changes after restart."""
    import os

    pid = int(current_pid if current_pid is not None else os.getpid())
    marker = alpha_matrix_publisher_marker_path()
    stale = False
    try:
        if marker.is_file():
            old = int(marker.read_text(encoding="ascii").strip())
            stale = old != pid
        else:
            stale = True
    except (OSError, ValueError):
        stale = True
    if stale:
        force_unmap_alpha_matrix()
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(pid), encoding="ascii")
    except OSError:
        pass
    return stale


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
    signal_floor = min(float(bases.signal_confidence_pct), 52.5)
    fitness_floor = min(float(bases.environment_fitness_pct), 52.5)
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
    if not should_publish_alpha_matrix():
        try:
            segment = get_alpha_matrix_segment(create=False)
            header = segment.read_header()
            return CompileReport(
                compile_ms=0.0,
                memory_bytes=_SHM_TOTAL_BYTES,
                patterns_scanned=int(header.get("patterns_scanned") or 0),
                true_wins=int(header.get("true_wins") or 0),
                true_losses=int(header.get("true_losses") or 0),
                cells_populated=int(np.sum(segment.matrix[:, COL_SAMPLES] > 0)),
            )
        except FileNotFoundError:
            return CompileReport(
                compile_ms=0.0,
                memory_bytes=_SHM_TOTAL_BYTES,
                patterns_scanned=0,
                true_wins=0,
                true_losses=0,
                cells_populated=0,
            )
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
    prior_gen: np.ndarray | None = None
    if np.any(matrix[:, COL_SAMPLES] > 0.0):
        prior_gen = np.array(matrix, copy=True, order="C")
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

    secure_fill_matrix_update(matrix, prior=prior_gen)
    populated = int(np.sum(matrix[:, COL_SAMPLES] > 0.0))

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


def fast_bootstrap_alpha_matrix_if_empty(*, stride: int = 48) -> bool:
    """Synchronous fast compile when SHM is empty — unblocks live matrix lookups."""
    if not should_publish_alpha_matrix():
        return False
    try:
        with _COMPILE_LOCK:
            if alpha_matrix_mapped():
                segment = get_alpha_matrix_segment(create=False)
                if int(np.sum(segment.matrix[:, COL_SAMPLES] > 0)) > 64:
                    return False
            _TELEMETRY["status"] = "compiling"
            report = compile_prebaked_alpha_matrix(stride=max(8, int(stride)))
        log_engine(
            f"AlphaMatrixPrebaker: fast bootstrap cells={report.cells_populated} "
            f"ms={report.compile_ms:.0f}"
        )
        return report.cells_populated > 0
    except Exception as exc:
        _TELEMETRY["status"] = "error"
        _TELEMETRY["last_error"] = str(exc)
        log_guarded_exception("alpha_matrix_fast_bootstrap", exc)
        return False


_COMPILE_API_THREAD: threading.Thread | None = None
_COMPILE_API_LOCK = threading.Lock()


def schedule_inprocess_alpha_compile(
    *,
    stride: int = 48,
    force: bool = True,
) -> dict[str, Any]:
    """
    Queue alpha-matrix compile on the live agent process (no external lock contention).
    Returns immediately; compile runs on a daemon thread inside this PID.
    """
    global _COMPILE_API_THREAD

    stride = max(8, min(int(stride), 256))
    with _COMPILE_API_LOCK:
        if _COMPILE_API_THREAD is not None and _COMPILE_API_THREAD.is_alive():
            return {
                "ok": False,
                "accepted": False,
                "error": "compile already in progress",
                "telemetry": matrix_compiler_telemetry(),
                "mapped": alpha_matrix_mapped(),
            }

        def _worker() -> None:
            try:
                with _COMPILE_LOCK:
                    _TELEMETRY["status"] = "compiling"
                    if force:
                        report = compile_prebaked_alpha_matrix(stride=stride)
                        log_engine(
                            "AlphaMatrixPrebaker: in-process API compile "
                            f"cells={report.cells_populated} ms={report.compile_ms:.0f}"
                        )
                    else:
                        fast_bootstrap_alpha_matrix_if_empty(stride=stride)
            except Exception as exc:
                _TELEMETRY["status"] = "error"
                _TELEMETRY["last_error"] = str(exc)
                log_guarded_exception("alpha_matrix_inprocess_compile", exc)

        _COMPILE_API_THREAD = threading.Thread(
            target=_worker,
            name="inprocess-alpha-compile",
            daemon=True,
        )
        _COMPILE_API_THREAD.start()

    return {
        "ok": True,
        "accepted": True,
        "agent_pid": os.getpid(),
        "force": force,
        "stride": stride,
        "memory_bytes": _SHM_TOTAL_BYTES,
        "slots": TOTAL_CELLS,
        "mapped": alpha_matrix_mapped(),
        "telemetry": matrix_compiler_telemetry(),
    }


def start_alpha_matrix_compiler_async(*, interval_sec: float = 300.0) -> None:
    """Shadow (:9199) background compiler — non-blocking to the hot path."""
    if not should_publish_alpha_matrix():
        log_engine(
            "AlphaMatrixPrebaker: compiler skipped (dual-port non-publisher twin)"
        )
        return
    global _COMPILER_THREAD
    if _COMPILER_THREAD is not None and _COMPILER_THREAD.is_alive():
        return

    def _bootstrap() -> None:
        try:
            fast_bootstrap_alpha_matrix_if_empty(stride=48)
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
