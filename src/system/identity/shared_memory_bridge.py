"""
Native shared-memory telemetry bridge — zero-copy IPC between trading and API.

Uses ``multiprocessing.shared_memory`` (stdlib) with fixed 64 KiB segments per
parallel track (live + shadow). Producer writes complete JSON snapshots in under
1µs; API consumers read without touching the trading hot path.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import threading
import time
from typing import Any

from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception
from system.guard.security_errors import SharedMemoryOverflowAlert

_SHM_NAME_LIVE = "ig_agent_v30_live_state"
_SHM_NAME_SHADOW = "ig_agent_v30_shadow_state"
_SHM_SIZE = 65536
_MAGIC = 0x49475630  # "IGV0"
_HEADER = struct.Struct("!IIII")  # magic, seq, length, crc32_placeholder
_HEADER_BYTES = _HEADER.size
_MAX_PAYLOAD = _SHM_SIZE - _HEADER_BYTES
_HARD_EXIT_OVERFLOW = 99

_LIVE_TRACK_PREFIX = "[LIVE-TRACK]"
_MOCK_TRACK_PREFIX = "[MOCK-TRACK]"


def resolve_parallel_track_key() -> str:
    track = os.environ.get("IG_PARALLEL_TRACK", "").strip().lower()
    if track in ("live", "shadow", "unified"):
        return track
    port = os.environ.get("IG_API_PORT", "").strip()
    if port == "9199":
        return "shadow"
    return "live"


def _engine_scoped_shm_suffix() -> str | None:
    """Dual-port twins must not share ``ig_agent_v30_live_state``."""
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


def shm_name_for_track(track_key: str) -> str:
    base = _SHM_NAME_SHADOW if track_key == "shadow" else _SHM_NAME_LIVE
    suffix = _engine_scoped_shm_suffix()
    if suffix:
        return f"{base}_{suffix}"
    return base


def track_label_for_key(track_key: str) -> str:
    return "mock" if track_key == "shadow" else "live"


def track_prefix_for_key(track_key: str) -> str:
    return _MOCK_TRACK_PREFIX if track_key == "shadow" else _LIVE_TRACK_PREFIX


def _enforce_payload_bounds(raw: bytes, *, context: str, segment_name: str) -> None:
    """Fail-closed when telemetry JSON exceeds the fixed 64 KiB RAM segment."""
    if len(raw) <= _MAX_PAYLOAD:
        return
    message = (
        f"SharedMemoryOverflowAlert: {context} on segment={segment_name!r} "
        f"payload_bytes={len(raw)} max_payload={_MAX_PAYLOAD} segment_size={_SHM_SIZE}"
    )
    log_engine(message)
    alert = SharedMemoryOverflowAlert(message)
    log_guarded_exception("shared_memory_overflow", alert)
    sys.exit(_HARD_EXIT_OVERFLOW)


def _default_payload(*, track_key: str | None = None) -> dict[str, Any]:
    key = track_key or resolve_parallel_track_key()
    port_raw = os.environ.get("IG_API_PORT", "").strip()
    api_port = int(port_raw) if port_raw.isdigit() else (9199 if key == "shadow" else 8080)
    return {
        "schema_version": "1.0",
        "track": track_label_for_key(key),
        "api_port": api_port,
        "updated_at_epoch": time.time(),
        "trailing_stops": [],
        "ml_optimization": {},
        "system_health": {},
    }


class SharedMemoryStateBridge:
    """
    Fixed-size shared RAM segment with sequence-stamped atomic writes.

    Write protocol (producer):
      1. Serialize JSON to bytes (compact, sorted keys).
      2. Stamp odd sequence number (in-flight).
      3. Copy payload into segment.
      4. Stamp even sequence number (committed).

    Read protocol (consumer):
      1. Read sequence; retry if odd or changed mid-read.
      2. Decode JSON payload.
    """

    def __init__(self, *, create: bool = False, name: str | None = None) -> None:
        from multiprocessing import shared_memory

        self._lock = threading.Lock()
        self._seq = 0
        self._name = name if name else _SHM_NAME_LIVE
        self._shm: shared_memory.SharedMemory | None = None
        self._attach(create=create)

    @property
    def name(self) -> str:
        return self._name

    @property
    def size(self) -> int:
        return _SHM_SIZE

    def _attach(self, *, create: bool) -> None:
        from multiprocessing import shared_memory

        if create:
            self._unlink_existing(self._name)
            self._shm = shared_memory.SharedMemory(
                name=self._name, create=True, size=_SHM_SIZE
            )
            self._write_header(0, 0)
            log_engine(f"SharedMemoryStateBridge: created name={self._name} size={_SHM_SIZE}")
            return

        try:
            self._shm = shared_memory.SharedMemory(name=self._name, create=False)
            log_engine(f"SharedMemoryStateBridge: attached name={self._name}")
        except FileNotFoundError:
            self._shm = shared_memory.SharedMemory(
                name=self._name, create=True, size=_SHM_SIZE
            )
            self._write_header(0, 0)
            log_engine(f"SharedMemoryStateBridge: created (lazy) name={self._name}")

    @staticmethod
    def _unlink_existing(name: str) -> None:
        from multiprocessing import shared_memory

        try:
            existing = shared_memory.SharedMemory(name=name, create=False)
        except FileNotFoundError:
            return
        try:
            existing.close()
            existing.unlink()
        except Exception as exc:
            log_guarded_exception("shared_memory_unlink", exc)

    def _write_header(self, seq: int, length: int) -> None:
        if self._shm is None:
            return
        _HEADER.pack_into(self._shm.buf, 0, _MAGIC, int(seq), int(length), 0)

    def write_json(self, payload: dict[str, Any]) -> int:
        """
        Serialize and write payload into shared RAM.

        Returns bytes written (excluding header). Never raises on hot path.
        """
        if self._shm is None:
            return 0
        try:
            raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        except (TypeError, ValueError) as exc:
            log_guarded_exception("shared_memory_serialize", exc)
            return 0

        segment_name = getattr(self, "_name", getattr(self, "name", "shared_memory"))
        _enforce_payload_bounds(raw, context="write_json", segment_name=segment_name)

        with self._lock:
            odd_seq = (self._seq + 1) | 1
            self._write_header(odd_seq, len(raw))
            self._shm.buf[_HEADER_BYTES : _HEADER_BYTES + len(raw)] = raw
            even_seq = odd_seq + 1
            self._write_header(even_seq, len(raw))
            self._seq = even_seq

        return len(raw)

    def write_bytes(self, raw: bytes) -> int:
        """Write pre-serialized UTF-8 JSON bytes — fastest producer path."""
        if self._shm is None:
            return 0
        segment_name = getattr(self, "_name", getattr(self, "name", "shared_memory"))
        _enforce_payload_bounds(raw, context="write_bytes", segment_name=segment_name)
        with self._lock:
            odd_seq = (self._seq + 1) | 1
            self._write_header(odd_seq, len(raw))
            self._shm.buf[_HEADER_BYTES : _HEADER_BYTES + len(raw)] = raw
            even_seq = odd_seq + 1
            self._write_header(even_seq, len(raw))
            self._seq = even_seq
        return len(raw)

    def read_json(self) -> dict[str, Any] | None:
        """Decode latest committed snapshot from shared RAM."""
        if self._shm is None:
            return None
        for _ in range(3):
            try:
                magic, seq_a, length, _crc = _HEADER.unpack_from(self._shm.buf, 0)
                if magic != _MAGIC or seq_a == 0 or (seq_a & 1):
                    return None
                if length <= 0 or length > _MAX_PAYLOAD:
                    return None
                raw = bytes(self._shm.buf[_HEADER_BYTES : _HEADER_BYTES + length])
                magic_b, seq_b, length_b, _ = _HEADER.unpack_from(self._shm.buf, 0)
                if seq_a != seq_b or length != length_b or magic_b != _MAGIC:
                    continue
                data = json.loads(raw.decode("utf-8"))
                return data if isinstance(data, dict) else None
            except (json.JSONDecodeError, UnicodeDecodeError, struct.error):
                return None
        return None

    def read_bytes(self) -> bytes | None:
        if self._shm is None:
            return None
        try:
            magic, seq_a, length, _ = _HEADER.unpack_from(self._shm.buf, 0)
            if magic != _MAGIC or seq_a == 0 or (seq_a & 1):
                return None
            if length <= 0 or length > _MAX_PAYLOAD:
                return None
            raw = bytes(self._shm.buf[_HEADER_BYTES : _HEADER_BYTES + length])
            _, seq_b, length_b, _ = _HEADER.unpack_from(self._shm.buf, 0)
            if seq_a != seq_b or length != length_b:
                return None
            return raw
        except struct.error:
            return None

    def is_initialized(self) -> bool:
        if self._shm is None:
            return False
        try:
            magic, seq, length, _ = _HEADER.unpack_from(self._shm.buf, 0)
            return magic == _MAGIC and seq > 0 and (seq & 1) == 0 and length > 0
        except struct.error:
            return False

    def close(self, *, unlink: bool = False) -> None:
        if self._shm is None:
            return
        try:
            self._shm.close()
            if unlink:
                self._shm.unlink()
        except Exception as exc:
            log_guarded_exception("shared_memory_close", exc)
        finally:
            self._shm = None


_bridge_singletons: dict[str, SharedMemoryStateBridge] = {}
_bridge_lock = threading.Lock()


def get_shared_memory_bridge(
    *, create: bool = False, track: str | None = None
) -> SharedMemoryStateBridge:
    track_key = track if track in ("live", "shadow") else resolve_parallel_track_key()
    name = shm_name_for_track(track_key)
    with _bridge_lock:
        bridge = _bridge_singletons.get(name)
        if bridge is None:
            bridge = SharedMemoryStateBridge(create=create, name=name)
            _bridge_singletons[name] = bridge
        return bridge


def attach_shared_memory_consumer(*, track: str | None = None) -> SharedMemoryStateBridge:
    """API-side attach — never unlinks the producer segment."""
    return get_shared_memory_bridge(create=False, track=track)


def reset_shared_memory_bridge(*, unlink: bool = True, track: str | None = None) -> None:
    """Tests and clean reboot — drop singleton(s) and optionally unlink segment(s)."""
    global _bridge_singletons
    targets = (
        [track]
        if track in ("live", "shadow")
        else ["live", "shadow"]
    )
    with _bridge_lock:
        for key in targets:
            name = shm_name_for_track(key)
            bridge = _bridge_singletons.pop(name, None)
            if bridge is not None:
                bridge.close(unlink=unlink)
            if unlink:
                SharedMemoryStateBridge._unlink_existing(name)


def _read_persisted_track_state(track_key: str) -> dict[str, Any]:
    from system.identity.state_cache import read_persisted_live_state

    if track_key == "shadow":
        from pathlib import Path

        return read_persisted_live_state(Path("/tmp/ig_agent_shadow_state.json"))
    return read_persisted_live_state()


def read_track_state_payload(track_key: str) -> dict[str, Any]:
    """Read one track's telemetry — shared RAM first, then disk fallback."""
    if track_key not in ("live", "shadow"):
        track_key = "live"
    try:
        bridge = attach_shared_memory_consumer(track=track_key)
        payload = bridge.read_json()
        if payload:
            return payload
    except Exception as exc:
        log_guarded_exception("shared_memory_read", exc)
    return _read_persisted_track_state(track_key)


def wrap_track_telemetry_envelope(
    track_key: str, payload: dict[str, Any]
) -> dict[str, Any]:
    return {
        "track": track_label_for_key(track_key),
        "prefix": track_prefix_for_key(track_key),
        "payload": payload,
    }


def read_dual_track_telemetry_envelope() -> dict[str, Any]:
    """
    Merged cockpit envelope — both parallel tracks with distinct GUI prefixes.

    WebSocket consumers receive this structure on every poll tick when either
    track's ``updated_at_epoch`` advances.
    """
    live_payload = read_track_state_payload("live")
    shadow_payload = read_track_state_payload("shadow")
    live_epoch = float(live_payload.get("updated_at_epoch") or 0.0)
    shadow_epoch = float(shadow_payload.get("updated_at_epoch") or 0.0)
    return {
        "schema_version": "1.1",
        "updated_at_epoch": max(live_epoch, shadow_epoch),
        "streams": [
            wrap_track_telemetry_envelope("live", live_payload),
            wrap_track_telemetry_envelope("shadow", shadow_payload),
        ],
    }


def read_live_state_payload() -> dict[str, Any]:
    """
    Unified read path for API layer — dual-track envelope (schema 1.1).

    Legacy single-track callers should read ``streams[0].payload`` for live-only.
    """
    return read_dual_track_telemetry_envelope()
