"""
Single-String execution profiler — zero-overhead ctypes writes to /ig_agent_v30_shm.

Four explicit phases: FPTP ingestion → geometry quantization → tensor look-ahead → broker tunnel.
"""

from __future__ import annotations

import ctypes
import time
from typing import Any

STRING_DIAG_MAGIC = 0x53545244  # 'STRD'

# Phase 1 error codes
P1_OK = 0
P1_SOCKET = 1
P1_NULL_TUPLE = 2
P1_TICKS_STALE = 3

# Phase 2 error codes
P2_OK = 0
P2_BOUNDARY = 1
P2_EMPTY_CELL = 2

# Phase 4 block codes
P4_OK = 0
P4_AUTH = 1
P4_RATE = 2
P4_MARGIN = 3
P4_OTHER = 4
P4_REGIME = 5

# Extended Phase 4 category strings (cockpit)
EXT_AUTH_EXPIRY = "AUTH_EXPIRY"
EXT_RATE_WALL = "RATE_WALL"
EXT_REGIME_MISMATCH = "REGIME_MISMATCH"
EXT_MARGIN_LOCK = "MARGIN_LOCK"
EXT_ROUTE_OPEN = "ROUTE_OPEN"

WEAKNESS_P1 = "[STRING_WEAKNESS] Phase 1 Ingestion Lock (Socket Corrupted or Null Tuple)"
WEAKNESS_P2 = "[STRING_WEAKNESS] Phase 2 Geometry Drop (Cell Empty or Index Boundary Lock)"
WEAKNESS_P3 = "[STRING_WEAKNESS] Phase 3 Tensor FAIL_ZONE (Frozen threshold / ATR multiplier)"
WEAKNESS_P4 = "[STRING_WEAKNESS] Phase 4 Broker Tunnel Block"


class StringPhaseDiag(ctypes.Structure):
    """Appended registry block at tail of ig_agent_v30_shm."""

    _pack_ = 1
    _fields_ = [
        ("diag_magic", ctypes.c_uint32),
        ("diag_version", ctypes.c_uint16),
        ("weakness_phase", ctypes.c_uint8),
        ("_pad0", ctypes.c_uint8),
        ("phase1_latency_us", ctypes.c_uint32),
        ("phase1_status", ctypes.c_uint8),
        ("phase1_code", ctypes.c_uint8),
        ("phase1_source_id", ctypes.c_uint8),
        ("_pad1", ctypes.c_uint8),
        ("phase1_ticks_before", ctypes.c_uint32),
        ("phase1_ticks_after", ctypes.c_uint32),
        ("phase2_latency_us", ctypes.c_uint32),
        ("phase2_status", ctypes.c_uint8),
        ("phase2_code", ctypes.c_uint8),
        ("_pad2", ctypes.c_uint8 * 2),
        ("phase2_coordinate", ctypes.c_uint32),
        ("phase2_rsi", ctypes.c_float),
        ("phase2_atr", ctypes.c_float),
        ("phase2_momentum", ctypes.c_float),
        ("phase3_latency_us", ctypes.c_uint32),
        ("phase3_zone", ctypes.c_uint8),
        ("phase3_fail_streak", ctypes.c_uint16),
        ("phase3_signal_threshold", ctypes.c_float),
        ("phase3_atr_multiplier", ctypes.c_float),
        ("phase4_latency_us", ctypes.c_uint32),
        ("phase4_status", ctypes.c_uint8),
        ("phase4_block_code", ctypes.c_uint8),
        ("phase4_http_status", ctypes.c_uint16),
        ("phase4_route_open", ctypes.c_uint8),
        ("_pad4", ctypes.c_uint8 * 3),
        ("phase4_extended_error", ctypes.c_char * 64),
        ("weakness_msg", ctypes.c_char * 96),
        ("updated_ns", ctypes.c_uint64),
    ]


_DIAG_VIEW: StringPhaseDiag | None = None
_DIAG_ATTACHED = False


def string_diag_offset(header_bytes: int, fill_slots: int, fill_row_bytes: int) -> int:
    return int(header_bytes) + int(fill_slots) * int(fill_row_bytes)


def _bind_diag(seg: Any, offset: int) -> StringPhaseDiag:
    global _DIAG_VIEW, _DIAG_ATTACHED
    if _DIAG_VIEW is None or not _DIAG_ATTACHED:
        view = StringPhaseDiag.from_buffer(seg.buf, offset)
        if int(view.diag_magic) != STRING_DIAG_MAGIC:
            view.diag_magic = STRING_DIAG_MAGIC
            view.diag_version = 2
        _DIAG_VIEW = view
        _DIAG_ATTACHED = True
    return _DIAG_VIEW


def _touch_weakness(diag: StringPhaseDiag, phase: int, msg: str) -> None:
    diag.weakness_phase = int(phase) & 0xFF
    encoded = str(msg or "")[:95].encode("ascii", "replace")
    diag.weakness_msg = encoded + b"\0" * (96 - len(encoded))
    diag.updated_ns = time.perf_counter_ns()


def attach_string_diag(seg: Any, offset: int) -> StringPhaseDiag:
    return _bind_diag(seg, offset)


def record_phase1_win(
    diag: StringPhaseDiag,
    *,
    latency_us: int,
    source_id: int,
    ticks_before: int,
    ticks_after: int,
) -> None:
    diag.phase1_latency_us = int(latency_us) & 0xFFFFFFFF
    if ticks_after <= ticks_before:
        diag.phase1_status = 1
        diag.phase1_code = P1_TICKS_STALE
        _touch_weakness(diag, 1, WEAKNESS_P1)
    else:
        diag.phase1_status = 0
        diag.phase1_code = P1_OK
    diag.phase1_source_id = int(source_id) & 0xFF
    diag.phase1_ticks_before = int(ticks_before) & 0xFFFFFFFF
    diag.phase1_ticks_after = int(ticks_after) & 0xFFFFFFFF
    diag.updated_ns = time.perf_counter_ns()


def record_phase1_drop(
    diag: StringPhaseDiag,
    *,
    code: int,
    latency_us: int = 0,
    ticks_before: int = 0,
) -> None:
    diag.phase1_latency_us = int(latency_us) & 0xFFFFFFFF
    diag.phase1_status = 1
    diag.phase1_code = int(code) & 0xFF
    diag.phase1_ticks_before = int(ticks_before) & 0xFFFFFFFF
    diag.phase1_ticks_after = int(ticks_before) & 0xFFFFFFFF
    _touch_weakness(diag, 1, WEAKNESS_P1)


def record_phase2(
    diag: StringPhaseDiag,
    *,
    latency_us: int,
    coordinate: int,
    rsi: float,
    atr: float,
    momentum: float,
    total_cells: int,
    cell_empty: bool,
) -> None:
    diag.phase2_latency_us = int(latency_us) & 0xFFFFFFFF
    diag.phase2_coordinate = int(coordinate) & 0xFFFFFFFF
    diag.phase2_rsi = float(rsi)
    diag.phase2_atr = float(atr)
    diag.phase2_momentum = float(momentum)
    coord = int(coordinate)
    if coord < 0 or coord >= int(total_cells):
        diag.phase2_status = 1
        diag.phase2_code = P2_BOUNDARY
        _touch_weakness(diag, 2, WEAKNESS_P2)
    elif cell_empty:
        diag.phase2_status = 1
        diag.phase2_code = P2_EMPTY_CELL
        _touch_weakness(diag, 2, WEAKNESS_P2)
    else:
        diag.phase2_status = 0
        diag.phase2_code = P2_OK
    diag.updated_ns = time.perf_counter_ns()


def record_phase3(
    diag: StringPhaseDiag,
    *,
    latency_us: int,
    zone: int,
    signal_threshold: float,
    atr_multiplier: float,
    prior_fail_streak: int = 0,
) -> int:
    streak = int(prior_fail_streak)
    if int(zone) == 0:
        streak += 1
    else:
        streak = 0
    diag.phase3_latency_us = int(latency_us) & 0xFFFFFFFF
    diag.phase3_zone = int(zone) & 0xFF
    diag.phase3_fail_streak = min(streak, 65535)
    diag.phase3_signal_threshold = float(signal_threshold)
    diag.phase3_atr_multiplier = float(atr_multiplier)
    if streak >= 3 and int(zone) == 0:
        _touch_weakness(
            diag,
            3,
            f"{WEAKNESS_P3} thr={signal_threshold:.2f} atr_mult={atr_multiplier:.2f} streak={streak}",
        )
    diag.updated_ns = time.perf_counter_ns()
    return streak


def record_phase4_block(
    diag: StringPhaseDiag,
    *,
    latency_us: int,
    http_status: int,
    block_code: int,
    detail: str = "",
) -> None:
    diag.phase4_latency_us = int(latency_us) & 0xFFFFFFFF
    diag.phase4_http_status = int(http_status) & 0xFFFF
    diag.phase4_block_code = int(block_code) & 0xFF
    diag.phase4_status = 0 if block_code == P4_OK else 1
    msg = WEAKNESS_P4
    if detail:
        msg = f"{msg}: {detail}"[:95]
    if block_code != P4_OK:
        _touch_weakness(diag, 4, msg)
    else:
        diag.updated_ns = time.perf_counter_ns()


def record_shadow_phase4(
    diag: StringPhaseDiag,
    *,
    route_open: bool,
    category: str,
    detail: str,
    latency_us: int,
    http_status: int = 0,
) -> None:
    """Shadow tracer — extended Phase 4 error stream."""
    cat = str(category or "").upper()[:63]
    msg = str(detail or cat)[:63]
    diag.phase4_latency_us = int(latency_us) & 0xFFFFFFFF
    diag.phase4_route_open = 1 if route_open else 0
    diag.phase4_http_status = int(http_status) & 0xFFFF
    diag.phase4_status = 0 if route_open else 1
    block = P4_OK if route_open else classify_phase4_block(http_status=http_status, reason=cat)
    if cat == EXT_REGIME_MISMATCH:
        block = P4_REGIME
    elif cat == EXT_AUTH_EXPIRY:
        block = P4_AUTH
    elif cat == EXT_RATE_WALL:
        block = P4_RATE
    elif cat == EXT_MARGIN_LOCK:
        block = P4_MARGIN
    diag.phase4_block_code = int(block) & 0xFF
    encoded = f"{cat}:{msg}"[:63].encode("ascii", "replace")
    diag.phase4_extended_error = encoded + b"\0" * (64 - len(encoded))
    if not route_open:
        _touch_weakness(diag, 4, f"{WEAKNESS_P4} [{cat}] {msg}")
    diag.updated_ns = time.perf_counter_ns()


def classify_phase4_block(*, http_status: int | None, reason: str) -> int:
    text = str(reason or "").lower()
    status = int(http_status or 0)
    if status in (401, 403) or "auth" in text or "session" in text or "token" in text:
        return P4_AUTH
    if status == 429 or "rate" in text or "budget" in text:
        return P4_RATE
    if "margin" in text or "insufficient" in text or "fund" in text:
        return P4_MARGIN
    if "regime" in text or "stale" in text or "quote_age" in text:
        return P4_REGIME
    if status or reason:
        return P4_OTHER
    return P4_OK


def emit_broker_tunnel_diag(
    *,
    http_status: int = 0,
    reason: str = "",
    latency_us: int = 0,
) -> None:
    """Phase 4 — hot-path broker tunnel weakness writer."""
    try:
        from system.ipc.ring_buffer import _string_diag_view

        diag = _string_diag_view(create=True)
        if diag is None:
            return
        code = classify_phase4_block(http_status=http_status, reason=reason)
        record_phase4_block(
            diag,
            latency_us=latency_us,
            http_status=http_status,
            block_code=code,
            detail=str(reason or "")[:80],
        )
    except Exception:
        pass


def decode_string_diag(raw: bytes, offset: int) -> dict[str, Any] | None:
    if len(raw) < offset + ctypes.sizeof(StringPhaseDiag):
        return None
    row = StringPhaseDiag.from_buffer_copy(raw[offset : offset + ctypes.sizeof(StringPhaseDiag)])
    if int(row.diag_magic) != STRING_DIAG_MAGIC:
        return None
    msg = bytes(row.weakness_msg).split(b"\0", 1)[0].decode("ascii", "replace").strip()
    ext = bytes(row.phase4_extended_error).split(b"\0", 1)[0].decode("ascii", "replace").strip()
    return {
        "phase1_latency_us": int(row.phase1_latency_us),
        "phase1_ok": int(row.phase1_status) == 0,
        "phase1_code": int(row.phase1_code),
        "phase1_ticks_before": int(row.phase1_ticks_before),
        "phase1_ticks_after": int(row.phase1_ticks_after),
        "phase2_latency_us": int(row.phase2_latency_us),
        "phase2_ok": int(row.phase2_status) == 0,
        "phase2_code": int(row.phase2_code),
        "phase2_coordinate": int(row.phase2_coordinate),
        "phase2_rsi": float(row.phase2_rsi),
        "phase2_atr": float(row.phase2_atr),
        "phase2_momentum": float(row.phase2_momentum),
        "phase3_latency_us": int(row.phase3_latency_us),
        "phase3_zone": int(row.phase3_zone),
        "phase3_fail_streak": int(row.phase3_fail_streak),
        "phase3_signal_threshold": float(row.phase3_signal_threshold),
        "phase3_atr_multiplier": float(row.phase3_atr_multiplier),
        "phase4_latency_us": int(row.phase4_latency_us),
        "phase4_ok": int(row.phase4_status) == 0,
        "phase4_block_code": int(row.phase4_block_code),
        "phase4_http_status": int(row.phase4_http_status),
        "phase4_route_open": bool(row.phase4_route_open),
        "phase4_extended_error": ext,
        "weakness_phase": int(row.weakness_phase),
        "weakness_msg": msg,
        "updated_ns": int(row.updated_ns),
    }
