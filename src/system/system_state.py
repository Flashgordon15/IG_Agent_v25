"""
Thread-safe boot progress container — single source of truth for the BootState machine.

Replaces fragmented startup_tracker / boot_metrics / init_force_cleared signals.
Gate modules populate this via BootCoordinator; API layers read snapshots only.
"""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, ClassVar, Literal, Self

GateId = Literal["G1", "G2", "G3", "G4", "G5"]
GATE_IDS: tuple[GateId, ...] = ("G1", "G2", "G3", "G4", "G5")


class GateStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class BootPhase(StrEnum):
    """High-level boot phase exposed to dashboard and /api/health."""

    BOOTING = "BOOTING"
    WARMING = "WARMING"
    G1 = "G1"
    G2 = "G2"
    G3_STREAMING = "G3_STREAMING"
    G4 = "G4"
    G5 = "G5"
    READY = "READY"
    FAILED = "FAILED"


@dataclass
class GateSnapshot:
    status: GateStatus = GateStatus.PENDING
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"status": str(self.status), "detail": self.detail}


@dataclass
class StreamingSnapshot:
    transport: str = ""
    heartbeat_ok: bool = False
    first_tick_epic: str | None = None
    first_tick_at: str | None = None
    market_closed_exempt: bool = False
    hydration_mode: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HydrationSnapshot:
    positions_synced: bool = False
    orders_synced: bool = False
    ohlc_epics_ready: int = 0
    ohlc_epics_total: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LoopsSnapshot:
    built: int = 0
    running: bool = False
    accepting_ticks: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BackgroundVerifySnapshot:
    pytest_status: str = "not_started"
    last_run_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _format_wall_time_iso(ts: float) -> str:
    """UTC ISO timestamp with millisecond precision from ``time.time()``."""
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


_BOOT_STARTED_AT_WALL: float | None = None


def stamp_process_boot_start() -> float:
    """
    Anchor boot timing at ``main()`` entry — before Gate 1 or FastAPI lifespan.

    Idempotent: first call wins for the process lifetime.
    """
    global _BOOT_STARTED_AT_WALL
    if _BOOT_STARTED_AT_WALL is None:
        _BOOT_STARTED_AT_WALL = time.time()
    return _BOOT_STARTED_AT_WALL


def get_boot_started_at_wall() -> float | None:
    """Return the process boot anchor, if ``stamp_process_boot_start()`` ran."""
    return _BOOT_STARTED_AT_WALL


def _resolve_boot_started() -> tuple[str, float]:
    ts = _BOOT_STARTED_AT_WALL if _BOOT_STARTED_AT_WALL is not None else time.time()
    return _format_wall_time_iso(ts), ts


def _default_gates() -> dict[str, GateSnapshot]:
    return {gid: GateSnapshot() for gid in GATE_IDS}


def _default_gate_completed_at() -> dict[str, str | None]:
    return {gid: None for gid in GATE_IDS}


@dataclass
class SystemStateSnapshot:
    """Immutable-friendly view of the full system_state JSON contract."""

    phase: BootPhase = BootPhase.BOOTING
    phase_label: str = "System Booting"
    percent: int = 0
    ready: bool = False
    error: str | None = None
    error_gate: GateId | None = None
    started_at: str = ""
    started_at_epoch: float = 0.0
    gate_completed_at: dict[str, str | None] = field(
        default_factory=_default_gate_completed_at
    )
    gates: dict[str, GateSnapshot] = field(default_factory=_default_gates)
    streaming: StreamingSnapshot = field(default_factory=StreamingSnapshot)
    hydration: HydrationSnapshot = field(default_factory=HydrationSnapshot)
    loops: LoopsSnapshot = field(default_factory=LoopsSnapshot)
    background_verify: BackgroundVerifySnapshot = field(
        default_factory=BackgroundVerifySnapshot
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": str(self.phase),
            "phase_label": self.phase_label,
            "percent": int(self.percent),
            "ready": bool(self.ready),
            "error": self.error,
            "error_gate": self.error_gate,
            "started_at": self.started_at,
            "started_at_epoch": float(self.started_at_epoch),
            "gate_completed_at": dict(self.gate_completed_at),
            "gates": {k: v.to_dict() for k, v in self.gates.items()},
            "streaming": self.streaming.to_dict(),
            "hydration": self.hydration.to_dict(),
            "loops": self.loops.to_dict(),
            "background_verify": self.background_verify.to_dict(),
        }


def _coerce_gate_status(value: GateStatus | str) -> GateStatus:
    if isinstance(value, GateStatus):
        return value
    return GateStatus(str(value))


def _coerce_boot_phase(value: BootPhase | str) -> BootPhase:
    if isinstance(value, BootPhase):
        return value
    return BootPhase(str(value))


def _normalize_gates_dict(
    gates_dict: dict[str, GateSnapshot | dict[str, Any]] | None,
) -> dict[str, GateSnapshot]:
    if gates_dict is None:
        return _default_gates()
    out: dict[str, GateSnapshot] = _default_gates()
    for gid in GATE_IDS:
        raw = gates_dict.get(gid)
        if raw is None:
            continue
        if isinstance(raw, GateSnapshot):
            out[gid] = GateSnapshot(status=raw.status, detail=raw.detail)
        elif isinstance(raw, dict):
            out[gid] = GateSnapshot(
                status=_coerce_gate_status(raw.get("status", GateStatus.PENDING)),
                detail=str(raw.get("detail") or ""),
            )
    return out


def _merge_dataclass(
    current: Any,
    patch: dict[str, Any] | Any | None,
    cls: type,
) -> Any:
    if patch is None:
        return current
    if isinstance(patch, cls):
        return patch
    if not isinstance(patch, dict):
        return current
    data = asdict(current)
    data.update(patch)
    return cls(**data)


class SystemState:
    """
    Process-wide boot state singleton.

    All mutations must go through update_state() or the typed helpers below so
    readers always observe a consistent snapshot.
    """

    _instance: ClassVar[SystemState | None] = None
    _instance_lock: ClassVar[threading.Lock] = threading.Lock()

    def __new__(cls) -> Self:
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init_once()
        return cls._instance

    def _init_once(self) -> None:
        self._lock = threading.RLock()
        started_iso, started_epoch = _resolve_boot_started()
        self._snapshot = SystemStateSnapshot(
            started_at=started_iso,
            started_at_epoch=started_epoch,
        )

    @classmethod
    def get(cls) -> SystemState:
        return cls()

    @classmethod
    def reset_singleton_for_tests(cls) -> None:
        """Drop the singleton — for pytest isolation only."""
        global _BOOT_STARTED_AT_WALL
        with cls._instance_lock:
            cls._instance = None
            _BOOT_STARTED_AT_WALL = None

    def snapshot(self) -> dict[str, Any]:
        """Return a deep copy of the public JSON contract."""
        with self._lock:
            return copy.deepcopy(self._snapshot.to_dict())

    def snapshot_model(self) -> SystemStateSnapshot:
        with self._lock:
            return copy.deepcopy(self._snapshot)

    def reset(
        self,
        *,
        started_at: str | None = None,
        started_at_epoch: float | None = None,
    ) -> None:
        """Return to initial booting state (tests and cold pipeline restart)."""
        with self._lock:
            epoch = (
                float(started_at_epoch)
                if started_at_epoch is not None
                else (_BOOT_STARTED_AT_WALL or time.time())
            )
            iso = started_at or _format_wall_time_iso(epoch)
            self._snapshot = SystemStateSnapshot(
                started_at=iso,
                started_at_epoch=epoch,
            )

    def update_state(
        self,
        phase: BootPhase | str,
        percent: int,
        label: str,
        gates_dict: dict[str, GateSnapshot | dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> None:
        """
        Atomically update top-level boot fields under the instance lock.

        ``gates_dict`` replaces the entire gates map when provided.
        Nested objects (streaming, hydration, loops, background_verify,
        gate_completed_at) are shallow-merged when passed as dict patches in kwargs.
        """
        with self._lock:
            self._snapshot.phase = _coerce_boot_phase(phase)
            self._snapshot.percent = max(0, min(100, int(percent)))
            self._snapshot.phase_label = str(label)
            if gates_dict is not None:
                self._snapshot.gates = _normalize_gates_dict(gates_dict)

            if "ready" in kwargs:
                self._snapshot.ready = bool(kwargs.pop("ready"))
            if "error" in kwargs:
                err = kwargs.pop("error")
                self._snapshot.error = None if err is None else str(err)
            if "error_gate" in kwargs:
                gate = kwargs.pop("error_gate")
                self._snapshot.error_gate = gate if gate in GATE_IDS else None
            if "started_at" in kwargs:
                self._snapshot.started_at = str(kwargs.pop("started_at"))
            if "started_at_epoch" in kwargs:
                self._snapshot.started_at_epoch = float(kwargs.pop("started_at_epoch"))
            if "gate_completed_at" in kwargs:
                patch = kwargs.pop("gate_completed_at")
                if isinstance(patch, dict):
                    merged = _default_gate_completed_at()
                    merged.update(self._snapshot.gate_completed_at)
                    for gid in GATE_IDS:
                        if gid in patch:
                            merged[gid] = patch[gid]
                    self._snapshot.gate_completed_at = merged

            self._snapshot.streaming = _merge_dataclass(
                self._snapshot.streaming,
                kwargs.pop("streaming", None),
                StreamingSnapshot,
            )
            self._snapshot.hydration = _merge_dataclass(
                self._snapshot.hydration,
                kwargs.pop("hydration", None),
                HydrationSnapshot,
            )
            self._snapshot.loops = _merge_dataclass(
                self._snapshot.loops,
                kwargs.pop("loops", None),
                LoopsSnapshot,
            )
            self._snapshot.background_verify = _merge_dataclass(
                self._snapshot.background_verify,
                kwargs.pop("background_verify", None),
                BackgroundVerifySnapshot,
            )

            if kwargs:
                raise TypeError(
                    f"update_state() got unexpected keyword arguments: {sorted(kwargs)}"
                )

    def mark_gate_running(self, gate_id: GateId, *, detail: str = "") -> None:
        with self._lock:
            gate = self._snapshot.gates[gate_id]
            gate.status = GateStatus.RUNNING
            if detail:
                gate.detail = detail

    def mark_gate_complete(self, gate_id: GateId, *, detail: str = "") -> None:
        with self._lock:
            gate = self._snapshot.gates[gate_id]
            gate.status = GateStatus.COMPLETE
            if detail:
                gate.detail = detail
            self._snapshot.gate_completed_at[gate_id] = _utc_now_iso()

    def mark_gate_failed(
        self,
        gate_id: GateId,
        *,
        error: str,
        detail: str = "",
    ) -> None:
        with self._lock:
            gate = self._snapshot.gates[gate_id]
            gate.status = GateStatus.FAILED
            if detail:
                gate.detail = detail
            self._snapshot.phase = BootPhase.FAILED
            self._snapshot.ready = False
            self._snapshot.error = error
            self._snapshot.error_gate = gate_id
        try:
            from apex.warmup_progress import mark_warmup_failed

            mark_warmup_failed(error)
        except Exception:
            pass

    def gate_complete(self, gate_id: GateId) -> bool:
        with self._lock:
            return self._snapshot.gates[gate_id].status == GateStatus.COMPLETE

    def try_gate_complete(self, gate_id: GateId, *, timeout: float = 0.0) -> bool:
        """Non-blocking gate completion probe for heal/watchdog paths."""
        if self._lock.acquire(timeout=max(0.0, float(timeout))):
            try:
                return self._snapshot.gates[gate_id].status == GateStatus.COMPLETE
            finally:
                self._lock.release()
        return is_gate_sideband_fallback(gate_id)

    def try_snapshot(self, *, timeout: float = 0.5) -> dict[str, Any] | None:
        """Best-effort snapshot without blocking boot workers indefinitely."""
        if self._lock.acquire(timeout=max(0.0, float(timeout))):
            try:
                return copy.deepcopy(self._snapshot.to_dict())
            finally:
                self._lock.release()
        return None

    def set_ready(self, *, label: str = "ACTIVE") -> None:
        """Atomic READY flip — G5 completion."""
        with self._lock:
            self._snapshot.ready = True
            self._snapshot.phase = BootPhase.READY
            self._snapshot.percent = 100
            self._snapshot.phase_label = label
            self._snapshot.error = None
            self._snapshot.error_gate = None


def is_gate_sideband_fallback(gate_id: str) -> bool:
    try:
        from system.boot.gate_sideband import is_gate_sideband

        return is_gate_sideband(gate_id)
    except Exception:
        return False


def get_system_state() -> SystemState:
    """Return the process-wide SystemState singleton."""
    return SystemState.get()
