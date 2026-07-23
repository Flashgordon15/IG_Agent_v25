"""
Passive Darwin SHM reader for desktop / terminal cockpits — stdlib only (no numpy).

Duplicates the ctypes layout from ``ring_buffer`` so GUI processes can attach
without importing the full ring-buffer module graph.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
from datetime import datetime, timezone
from multiprocessing import shared_memory
from typing import Any

# Linkage states returned to desktop / terminal cockpits.
LINK_LIVE = "LIVE"
LINK_STALE_SHM = "STALE_SHM"
LINK_NO_SEGMENT = "NO_SEGMENT"

from system.ipc.string_diagnostics import StringPhaseDiag, decode_string_diag, string_diag_offset

COCKPIT_SHM_NAME = "ig_agent_v30_shm"
COCKPIT_SHM_MAGIC = 0x30334749  # 'IG30'
COCKPIT_SHM_FILL_SLOTS = 5
# CFD primary for desktop SHM when dual-port marker present (no env override).
_DUAL_CFD_COCKPIT_ACCOUNT = "Z6BAH4"

VALVE_SCANNING = 0
VALVE_WIN_ZONE = 1
VALVE_FIRE = 2
VALVE_STALL = 3


class CockpitFillRow(ctypes.Structure):
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


COCKPIT_SHM_BYTES = (
    ctypes.sizeof(CockpitShmHeader)
    + COCKPIT_SHM_FILL_SLOTS * ctypes.sizeof(CockpitFillRow)
    + ctypes.sizeof(StringPhaseDiag)
)


def _normalize_shm_segment_name(name: str) -> str:
    raw = str(name or "").strip()
    if raw.startswith("/dev/shm/"):
        raw = raw[len("/dev/shm/") :]
    while raw.startswith("/"):
        raw = raw[1:]
    return raw or COCKPIT_SHM_NAME


def _sanitize_shm_token(raw: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_]", "_", str(raw or "").strip())
    return token.strip("_") or "default"


def resolve_cockpit_shm_name() -> str:
    """
    Per-engine cockpit segment — avoids twin collision on dual-port desk.

    Priority: ``IG_COCKPIT_SHM_NAME`` → dual lane (cfd_8080/sb_8081) → account → legacy default.
    """
    override = os.environ.get("IG_COCKPIT_SHM_NAME", "").strip()
    if override:
        return _normalize_shm_segment_name(override)
    if os.environ.get("IG_V32_DUAL_PORT", "").strip() == "1":
        try:
            from kernel.ring_buffer import resolve_dual_port_shm_lane_token

            lane = resolve_dual_port_shm_lane_token()
            if lane:
                return f"ig_agent_v33_cockpit_{lane}"
        except Exception:
            pass
        account = os.environ.get("IG_ACCOUNT_ID", "").strip().upper()
        if account:
            return f"ig_agent_v33_cockpit_{_sanitize_shm_token(account)}"
        port = os.environ.get("IG_API_PORT", os.environ.get("PORT", "")).strip()
        origin = os.environ.get("IG_ENGINE_ORIGIN", "").strip().upper()
        if origin and port.isdigit():
            return (
                f"ig_agent_v33_cockpit_{_sanitize_shm_token(origin.lower())}_{port}"
            )
        if origin:
            return f"ig_agent_v33_cockpit_{_sanitize_shm_token(origin.lower())}"
    return COCKPIT_SHM_NAME


def resolve_cockpit_shm_name_for_reader() -> str:
    """Passive reader default — CFD twin on dual desk unless env names segment."""
    override = os.environ.get("IG_COCKPIT_SHM_NAME", "").strip()
    if override:
        return _normalize_shm_segment_name(override)
    if os.environ.get("IG_V32_DUAL_PORT", "").strip() == "1":
        return f"ig_agent_v33_cockpit_{_sanitize_shm_token(_DUAL_CFD_COCKPIT_ACCOUNT)}"
    return COCKPIT_SHM_NAME


def pid_is_alive(pid: int) -> bool:
    """True when the publishing agent process is still running."""
    if int(pid or 0) <= 0:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def classify_cockpit_shm(view: dict[str, Any] | None) -> tuple[str, str]:
    """
    Classify SHM trust for cockpit rendering.

    Returns (link_state, detail). STALE_SHM means a zombie segment from a dead PID.
    """
    if view is None:
        seg = resolve_cockpit_shm_name_for_reader()
        return LINK_NO_SEGMENT, f"POSIX segment {seg} not published"
    pid = int(view.get("agent_pid") or 0)
    if not pid_is_alive(pid):
        ticks = int(view.get("ticks_cached") or 0)
        return (
            LINK_STALE_SHM,
            f"stale SHM from dead pid {pid} (ticks={ticks:,} — do not trust)",
        )
    return LINK_LIVE, f"pid {pid} publishing"


def cockpit_shm_map_status() -> dict[str, str]:
    seg = _normalize_shm_segment_name(resolve_cockpit_shm_name_for_reader())
    if sys.platform == "darwin":
        return {"namespace": f"Darwin-POSIX:{seg}", "segment": seg}
    return {"namespace": f"posix-shm:{seg}", "segment": seg}


def read_cockpit_shm() -> dict[str, Any] | None:
    """Attach existing segment, unpack header + fill rows, close — read-only."""
    name = _normalize_shm_segment_name(resolve_cockpit_shm_name_for_reader())
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
    row_size = ctypes.sizeof(CockpitFillRow)
    base = ctypes.sizeof(CockpitShmHeader)
    # Scan every fixed slot — fill_count can lag behind valve/fire writes.
    for slot in range(COCKPIT_SHM_FILL_SLOTS):
        chunk = raw[base + slot * row_size : base + (slot + 1) * row_size]
        row = CockpitFillRow.from_buffer_copy(chunk)
        epoch_ms = int(row.epoch_ms)
        if epoch_ms <= 0:
            continue
        ts = datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc).isoformat(
            timespec="milliseconds"
        )
        epic = bytes(row.epic).split(b"\0", 1)[0].decode("ascii", "replace").strip()
        if not epic:
            continue
        fills.append(
            {
                "executed_at": ts,
                "epoch_ms": epoch_ms,
                "epic": epic,
                "action": bytes(row.action).split(b"\0", 1)[0].decode("ascii", "replace"),
                "status": bytes(row.status).split(b"\0", 1)[0].decode("ascii", "replace"),
                "result": bytes(row.result).split(b"\0", 1)[0].decode("ascii", "replace"),
                "entry": float(row.entry_micro) / 10_000.0,
                "pnl_gbp": float(row.pnl_cents) / 100.0,
            }
        )
    fills.sort(key=lambda r: int(r.get("epoch_ms") or 0))

    valve = int(hdr.valve_status)
    diag_off = string_diag_offset(
        ctypes.sizeof(CockpitShmHeader),
        COCKPIT_SHM_FILL_SLOTS,
        ctypes.sizeof(CockpitFillRow),
    )
    string_diag = decode_string_diag(raw, diag_off)

    payload: dict[str, Any] = {
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
    link_state, link_detail = classify_cockpit_shm(payload)
    payload["link_state"] = link_state
    payload["link_detail"] = link_detail
    payload["publisher_alive"] = link_state == LINK_LIVE
    return payload
