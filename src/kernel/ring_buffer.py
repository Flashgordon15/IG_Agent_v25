"""
v33 lock-free shared-memory ring buffer — position risk metrics SoT in RAM.

Hot execution loops (dual_core sweep, soft_loss / trail_floor, ATR limits) publish
and consume via this module instead of disk I/O. Cold paths (journal, learning DB,
broker_snapshot) remain unchanged.

Lock-free semantics (honest):
  - Slots are written then the header ``write_idx`` is advanced (single writer).
  - Readers load ``write_idx`` then copy the slot at ``(write_idx - 1) % capacity``.
  - Python's GIL serializes bytecode within one process; cross-process readers see
    best-effort consistency without POSIX atomics — sufficient for desk telemetry
    and sub-ms hot-path reads, not a substitute for broker REST SoT.
"""

from __future__ import annotations

import ctypes
import os
import re
import struct
import time
from typing import Any

try:
    from multiprocessing import shared_memory as _shm_mod

    _HAVE_SHM = True
except ImportError:  # pragma: no cover
    _HAVE_SHM = False

RING_CAPACITY = 512
SHM_NAME_DEFAULT = "ig_agent_v33_ring"
_SHM_NAME_SAFE = re.compile(r"[^A-Za-z0-9_]")
_MAGIC = b"IGR3"
_VERSION = 1
RECORD_EMPTY = 0
RECORD_TICK = 1
RECORD_POSITION = 2
ATR_LIMIT_MULT_DEFAULT = 3.5


class _RingHeader(ctypes.Structure):
    _fields_ = [
        ("magic", ctypes.c_char * 4),
        ("version", ctypes.c_uint32),
        ("capacity", ctypes.c_uint32),
        ("write_idx", ctypes.c_uint64),
        ("tick_seq", ctypes.c_uint64),
        ("pos_seq", ctypes.c_uint64),
    ]


class _RingSlot(ctypes.Structure):
    _fields_ = [
        ("record_type", ctypes.c_uint8),
        ("_pad0", ctypes.c_uint8 * 7),
        ("seq", ctypes.c_uint64),
        ("ts_ns", ctypes.c_uint64),
        ("epic", ctypes.c_char * 32),
        ("deal_id", ctypes.c_char * 32),
        ("soft_loss_gbp", ctypes.c_double),
        ("trail_floor_gbp", ctypes.c_double),
        ("atr_limit_gbp", ctypes.c_double),
        ("atr_limit_pts", ctypes.c_double),
        ("atr_mult", ctypes.c_double),
        ("pnl_gbp", ctypes.c_double),
        ("peak_profit_gbp", ctypes.c_double),
        ("bid", ctypes.c_double),
        ("offer", ctypes.c_double),
    ]


_SLOT_SIZE = ctypes.sizeof(_RingSlot)
_HEADER_SIZE = ctypes.sizeof(_RingHeader)


def _sanitize_shm_token(raw: str) -> str:
    token = _SHM_NAME_SAFE.sub("_", str(raw or "").strip())
    return token.strip("_") or "default"


def dual_port_shm_lane_from(
    port: int,
    *,
    origin: str = "",
    account: str = "",
) -> str:
    """Resolve cfd_8080 / sb_8081 lane token from explicit port + origin/account."""
    origin_u = str(origin or "").strip().upper()
    account_u = str(account or "").strip().upper()
    port_s = str(int(port))
    if origin_u == "QUANT_SNIPER" and port_s == "8080":
        return "cfd_8080"
    if origin_u == "MACRO_SENTINEL" and port_s == "8081":
        return "sb_8081"
    if account_u == "Z6BAH4" and port_s == "8080":
        return "cfd_8080"
    if account_u == "Z6BAH3" and port_s == "8081":
        return "sb_8081"
    if origin_u:
        lane = (
            "cfd"
            if origin_u == "QUANT_SNIPER"
            else "sb"
            if origin_u == "MACRO_SENTINEL"
            else _sanitize_shm_token(origin_u.lower())
        )
        return f"{lane}_{port_s}"
    return f"eng_{port_s}"


def resolve_dual_port_shm_lane_token() -> str | None:
    """
    Dual-port desk lane token — ``cfd_8080`` / ``sb_8081`` style isolation.

    Returns None when not in v32 dual-port mode or port/origin cannot be resolved.
    """
    if os.environ.get("IG_V32_DUAL_PORT", "").strip() != "1":
        return None
    port = os.environ.get("IG_API_PORT", os.environ.get("PORT", "")).strip()
    if not port.isdigit():
        return None
    origin = os.environ.get("IG_ENGINE_ORIGIN", "").strip().upper()
    account = os.environ.get("IG_ACCOUNT_ID", "").strip().upper()
    return dual_port_shm_lane_from(int(port), origin=origin, account=account)


def resolve_position_ring_shm_name() -> str:
    """
    Per-engine ring segment — avoids twin collision on dual-port desk.

    Priority: ``IG_SHM_RING_NAME`` override → dual lane (cfd_8080/sb_8081) →
    account → origin+port → default.
    """
    override = os.environ.get("IG_SHM_RING_NAME", "").strip()
    if override:
        return override
    lane = resolve_dual_port_shm_lane_token()
    if lane:
        return f"ig_agent_v33_shm_{lane}"
    account = os.environ.get("IG_ACCOUNT_ID", "").strip().upper()
    if account:
        return f"ig_agent_v33_shm_{_sanitize_shm_token(account)}"
    origin = os.environ.get("IG_ENGINE_ORIGIN", "").strip().upper()
    port = os.environ.get("IG_API_PORT", os.environ.get("PORT", "")).strip()
    if origin and port.isdigit():
        return (
            f"ig_agent_v33_shm_{_sanitize_shm_token(origin.lower())}_{port}"
        )
    if origin:
        return f"ig_agent_v33_shm_{_sanitize_shm_token(origin.lower())}"
    return SHM_NAME_DEFAULT


def _encode_str32(value: str) -> bytes:
    raw = str(value or "")[:31].encode("ascii", errors="replace")
    return raw.ljust(32, b"\x00")


def _decode_str32(buf: bytes | ctypes.Array) -> str:
    if isinstance(buf, (bytes, bytearray)):
        data = bytes(buf)
    else:
        data = bytes(buf)
    return data.split(b"\x00", 1)[0].decode("ascii", errors="replace")


class PositionRingBuffer:
    """Fixed-layout SHM ring — strict ``__slots__`` wrapper."""

    __slots__ = ("_name", "_capacity", "_shm", "_buf", "_header", "_slots", "_owner")

    def __init__(
        self,
        *,
        name: str,
        capacity: int,
        shm: Any,
        buf: memoryview,
        owner: bool,
    ) -> None:
        self._name = name
        self._capacity = int(capacity)
        self._shm = shm
        self._buf = buf
        self._header = _RingHeader.from_buffer(self._buf, 0)
        self._slots = [
            _RingSlot.from_buffer(self._buf, _HEADER_SIZE + i * _SLOT_SIZE)
            for i in range(self._capacity)
        ]
        self._owner = owner

    @property
    def name(self) -> str:
        return self._name

    @property
    def capacity(self) -> int:
        return self._capacity

    @classmethod
    def create(
        cls,
        name: str = SHM_NAME_DEFAULT,
        *,
        capacity: int = RING_CAPACITY,
    ) -> PositionRingBuffer:
        if not _HAVE_SHM:
            raise RuntimeError("multiprocessing.shared_memory unavailable")
        cap = max(16, int(capacity))
        size = _HEADER_SIZE + cap * _SLOT_SIZE
        try:
            existing = _shm_mod.SharedMemory(name=name, create=False)
            existing.close()
            existing.unlink()
        except FileNotFoundError:
            pass
        shm = _shm_mod.SharedMemory(name=name, create=True, size=size)
        buf = memoryview(shm.buf)
        hdr = _RingHeader.from_buffer(buf, 0)
        hdr.magic = _MAGIC
        hdr.version = _VERSION
        hdr.capacity = cap
        hdr.write_idx = 0
        hdr.tick_seq = 0
        hdr.pos_seq = 0
        return cls(name=name, capacity=cap, shm=shm, buf=buf, owner=True)

    @classmethod
    def attach(
        cls,
        name: str = SHM_NAME_DEFAULT,
        *,
        capacity: int | None = None,
    ) -> PositionRingBuffer:
        if not _HAVE_SHM:
            raise RuntimeError("multiprocessing.shared_memory unavailable")
        shm = _shm_mod.SharedMemory(name=name, create=False)
        buf = memoryview(shm.buf)
        hdr = _RingHeader.from_buffer(buf, 0)
        if bytes(hdr.magic) != _MAGIC:
            raise ValueError(f"SHM {name!r} magic mismatch")
        cap = int(hdr.capacity if capacity is None else capacity)
        return cls(name=name, capacity=cap, shm=shm, buf=buf, owner=False)

    @classmethod
    def try_attach(cls, name: str = SHM_NAME_DEFAULT) -> PositionRingBuffer | None:
        try:
            return cls.attach(name=name)
        except Exception:
            return None

    def close(self, *, unlink: bool = False) -> None:
        if self._shm is None:
            return
        try:
            self._shm.close()
        except Exception:
            pass
        if unlink and self._owner:
            try:
                self._shm.unlink()
            except Exception:
                pass
        self._shm = None  # type: ignore[assignment]

    def _write_slot(self, slot: _RingSlot, *, record_type: int) -> int:
        idx = int(self._header.write_idx) % self._capacity
        target = self._slots[idx]
        ctypes.memmove(ctypes.addressof(target), ctypes.addressof(slot), _SLOT_SIZE)
        target.record_type = record_type
        new_idx = int(self._header.write_idx) + 1
        self._header.write_idx = new_idx
        return new_idx

    def publish_tick(
        self,
        *,
        epic: str,
        bid: float,
        offer: float,
        ts_ns: int | None = None,
    ) -> int:
        """Publish one quote tick — returns monotonic tick sequence."""
        slot = _RingSlot()
        slot.record_type = RECORD_TICK
        slot.seq = int(self._header.tick_seq) + 1
        slot.ts_ns = int(ts_ns if ts_ns is not None else time.time_ns())
        slot.epic = _encode_str32(epic)
        slot.bid = float(bid)
        slot.offer = float(offer)
        self._header.tick_seq = slot.seq
        self._write_slot(slot, record_type=RECORD_TICK)
        return int(slot.seq)

    def publish_position_snapshot(
        self,
        *,
        deal_id: str,
        epic: str,
        soft_loss_gbp: float = 0.0,
        trail_floor_gbp: float = 0.0,
        atr_limit_gbp: float = 0.0,
        atr_limit_pts: float = 0.0,
        atr_mult: float = ATR_LIMIT_MULT_DEFAULT,
        pnl_gbp: float | None = None,
        peak_profit_gbp: float | None = None,
        bid: float = 0.0,
        offer: float = 0.0,
        ts_ns: int | None = None,
    ) -> int:
        """Dual-write friendly publish — returns position sequence."""
        slot = _RingSlot()
        slot.record_type = RECORD_POSITION
        slot.seq = int(self._header.pos_seq) + 1
        slot.ts_ns = int(ts_ns if ts_ns is not None else time.time_ns())
        slot.epic = _encode_str32(epic)
        slot.deal_id = _encode_str32(deal_id)
        slot.soft_loss_gbp = float(soft_loss_gbp or 0.0)
        slot.trail_floor_gbp = float(trail_floor_gbp or 0.0)
        slot.atr_limit_gbp = float(atr_limit_gbp or 0.0)
        slot.atr_limit_pts = float(atr_limit_pts or 0.0)
        slot.atr_mult = float(atr_mult or ATR_LIMIT_MULT_DEFAULT)
        slot.pnl_gbp = float(pnl_gbp or 0.0)
        slot.peak_profit_gbp = float(
            peak_profit_gbp if peak_profit_gbp is not None else 0.0
        )
        slot.bid = float(bid or 0.0)
        slot.offer = float(offer or 0.0)
        self._header.pos_seq = slot.seq
        self._write_slot(slot, record_type=RECORD_POSITION)
        return int(slot.seq)

    def consume_latest(
        self,
        *,
        deal_id: str | None = None,
        record_type: int | None = None,
    ) -> dict[str, Any] | None:
        """Return newest matching record — scans backward from write_idx."""
        widx = int(self._header.write_idx)
        if widx <= 0:
            return None
        want_deal = str(deal_id or "").strip()
        for back in range(min(widx, self._capacity)):
            idx = (widx - 1 - back) % self._capacity
            slot = self._slots[idx]
            rtype = int(slot.record_type)
            if rtype == RECORD_EMPTY:
                continue
            if record_type is not None and rtype != record_type:
                continue
            rec = self._slot_to_dict(slot)
            if want_deal and rec.get("deal_id") != want_deal:
                continue
            return rec
        return None

    def consume_latest_position(self, deal_id: str | None = None) -> dict[str, Any] | None:
        return self.consume_latest(deal_id=deal_id, record_type=RECORD_POSITION)

    def consume_latest_tick(self, epic: str | None = None) -> dict[str, Any] | None:
        rec = self.consume_latest(record_type=RECORD_TICK)
        if rec is None:
            return None
        if epic and rec.get("epic") != epic:
            widx = int(self._header.write_idx)
            for back in range(min(widx, self._capacity)):
                idx = (widx - 1 - back) % self._capacity
                slot = self._slots[idx]
                if int(slot.record_type) != RECORD_TICK:
                    continue
                candidate = self._slot_to_dict(slot)
                if candidate.get("epic") == epic:
                    return candidate
            return None
        return rec

    def snapshot_positions(self, *, limit: int = 32) -> list[dict[str, Any]]:
        """Latest position record per deal_id (newest wins)."""
        widx = int(self._header.write_idx)
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for back in range(min(widx, self._capacity)):
            if len(out) >= limit:
                break
            idx = (widx - 1 - back) % self._capacity
            slot = self._slots[idx]
            if int(slot.record_type) != RECORD_POSITION:
                continue
            rec = self._slot_to_dict(slot)
            deal = str(rec.get("deal_id") or "")
            if not deal or deal in seen:
                continue
            seen.add(deal)
            out.append(rec)
        return out

    def header_stats(self) -> dict[str, Any]:
        return {
            "name": self._name,
            "capacity": self._capacity,
            "write_idx": int(self._header.write_idx),
            "tick_seq": int(self._header.tick_seq),
            "pos_seq": int(self._header.pos_seq),
            "slot_bytes": _SLOT_SIZE,
            "header_bytes": _HEADER_SIZE,
        }

    @staticmethod
    def _slot_to_dict(slot: _RingSlot) -> dict[str, Any]:
        return {
            "record_type": int(slot.record_type),
            "seq": int(slot.seq),
            "ts_ns": int(slot.ts_ns),
            "epic": _decode_str32(slot.epic),
            "deal_id": _decode_str32(slot.deal_id),
            "soft_loss_gbp": float(slot.soft_loss_gbp),
            "trail_floor_gbp": float(slot.trail_floor_gbp),
            "atr_limit_gbp": float(slot.atr_limit_gbp),
            "atr_limit_pts": float(slot.atr_limit_pts),
            "atr_mult": float(slot.atr_mult),
            "pnl_gbp": float(slot.pnl_gbp),
            "peak_profit_gbp": float(slot.peak_profit_gbp),
            "bid": float(slot.bid),
            "offer": float(slot.offer),
        }

    def latency_probe_ns(self) -> int | None:
        """Publish→consume latency for the latest tick (same process)."""
        t0 = time.time_ns()
        seq = self.publish_tick(epic="LATENCY.PROBE", bid=1.0, offer=1.0, ts_ns=t0)
        rec = self.consume_latest(record_type=RECORD_TICK)
        if rec is None or int(rec.get("seq") or 0) != seq:
            return None
        return time.time_ns() - t0


# In-process singleton for hot-path writers (lazy attach/create in tests).
_ring_singleton: PositionRingBuffer | None = None


def get_position_ring_buffer(*, create: bool = False) -> PositionRingBuffer | None:
    global _ring_singleton
    if _ring_singleton is not None:
        return _ring_singleton
    name = resolve_position_ring_shm_name()
    if create:
        try:
            _ring_singleton = PositionRingBuffer.create(name=name)
            return _ring_singleton
        except Exception:
            pass
    _ring_singleton = PositionRingBuffer.try_attach(name=name)
    if _ring_singleton is None and (
        create
        or os.environ.get("IG_SHM_RING_CREATE", "").lower() in ("1", "true", "yes")
    ):
        try:
            _ring_singleton = PositionRingBuffer.create(name=name)
        except Exception:
            pass
    return _ring_singleton


def reset_ring_buffer_for_tests() -> None:
    global _ring_singleton
    if _ring_singleton is not None:
        try:
            _ring_singleton.close(unlink=True)
        except Exception:
            pass
    _ring_singleton = None


def layout_doc() -> dict[str, Any]:
    return {
        "magic": _MAGIC.decode("ascii"),
        "version": _VERSION,
        "capacity_default": RING_CAPACITY,
        "header_fmt": struct.calcsize("<4sIIQQQ"),
        "slot_size_bytes": _SLOT_SIZE,
        "fields": [
            "soft_loss_gbp",
            "trail_floor_gbp",
            "atr_limit_gbp",
            "atr_limit_pts",
            "atr_mult",
            "pnl_gbp",
            "peak_profit_gbp",
            "epic",
            "deal_id",
            "bid",
            "offer",
        ],
    }
