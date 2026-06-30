"""
Deterministic boot pipeline — stages A–G, subsystem tracking, trade-readiness contract.

All HTTP reads are O(1) copies of in-memory state refreshed by a background thread.
Never blocks boot on slow subsystems; marks degraded and retries via subsystem_healer.
"""

from __future__ import annotations

import enum
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from system.engine_log import log_engine

_BOOT_LOG_MAX = 200
_REFRESH_INTERVAL_SEC = 1.0
_HEAL_INTERVAL_SEC = 2.0
_STAGE_TIMEOUT_SEC = 120.0
_SUBSYSTEM_RETRY_MAX = 5

_lock = threading.Lock()
_refresher_thread: threading.Thread | None = None
_healer_thread: threading.Thread | None = None
_stop = threading.Event()
_boot_log: deque[dict[str, Any]] = deque(maxlen=_BOOT_LOG_MAX)
_boot_started_mono = time.monotonic()


class BootStage(str, enum.Enum):
    A = "A"  # Core agent startup
    B = "B"  # Feed acquisition
    C = "C"  # External API readiness
    D = "D"  # Routing warm-up
    E = "E"  # Governance checks
    F = "F"  # Execution loop activation
    G = "G"  # Trade-readiness confirmation


class SubsystemId(str, enum.Enum):
    CORE_AGENT = "core_agent"
    YAHOO = "yahoo"
    IG = "ig"
    FEEDS = "feeds"
    ROUTING = "routing"
    GOVERNANCE = "governance"
    EXECUTION = "execution"


class StepStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"
    HEALING = "healing"


@dataclass
class StageRecord:
    id: str
    label: str
    status: str = StepStatus.PENDING.value
    started_at: str = ""
    completed_at: str = ""
    elapsed_ms: float | None = None
    last_error: str = ""
    retry_count: int = 0
    critical: bool = True


@dataclass
class SubsystemRecord:
    id: str
    label: str
    status: str = StepStatus.PENDING.value
    last_error: str = ""
    retry_count: int = 0
    last_heal_at: str = ""
    heal_action: str = ""
    critical: bool = True
    updated_at: str = ""


_STAGE_DEFS: tuple[tuple[BootStage, str, bool], ...] = (
    (BootStage.A, "Core agent startup", True),
    (BootStage.B, "Feed acquisition", True),
    (BootStage.C, "External API readiness", True),
    (BootStage.D, "Routing warm-up", True),
    (BootStage.E, "Governance checks", True),
    (BootStage.F, "Execution loop activation", True),
    (BootStage.G, "Trade-readiness confirmation", True),
)

_SUBSYSTEM_DEFS: tuple[tuple[SubsystemId, str, bool], ...] = (
    (SubsystemId.CORE_AGENT, "Core agent", True),
    (SubsystemId.YAHOO, "Yahoo API", True),
    (SubsystemId.IG, "IG API", True),
    (SubsystemId.FEEDS, "Market feeds", True),
    (SubsystemId.ROUTING, "Execution routing", True),
    (SubsystemId.GOVERNANCE, "Governance", False),
    (SubsystemId.EXECUTION, "Execution loop", True),
)

_stages: dict[str, StageRecord] = {
    s.value: StageRecord(id=s.value, label=label, critical=crit)
    for s, label, crit in _STAGE_DEFS
}
_subsystems: dict[str, SubsystemRecord] = {
    s.value: SubsystemRecord(id=s.value, label=label, critical=crit)
    for s, label, crit in _SUBSYSTEM_DEFS
}
_trade_ready: bool = False
_trade_ready_at: str = ""
_current_stage: str = BootStage.A.value
_estimated_ready_sec: float | None = None
_snapshot: dict[str, Any] = {}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _publish_snapshot() -> None:
    """Refresh in-memory HTTP snapshot from current stage/subsystem state."""
    global _trade_ready, _trade_ready_at, _estimated_ready_sec
    contract_ok, blockers = _evaluate_trade_ready_contract()
    elapsed = time.monotonic() - _boot_started_mono
    with _lock:
        _snapshot.clear()
        _snapshot.update(
            {
                "ok": True,
                "ts": _utc_now_iso(),
                "boot_elapsed_sec": round(elapsed, 2),
                "current_stage": _current_stage,
                "trade_ready": _trade_ready,
                "trade_ready_at": _trade_ready_at,
                "estimated_ready_sec": _estimated_ready_sec,
                "stages": [
                    {
                        "id": r.id,
                        "label": r.label,
                        "status": r.status,
                        "started_at": r.started_at,
                        "completed_at": r.completed_at,
                        "elapsed_ms": r.elapsed_ms,
                        "last_error": r.last_error,
                        "retry_count": r.retry_count,
                        "critical": r.critical,
                    }
                    for r in _stages.values()
                ],
                "subsystems": [
                    {
                        "id": r.id,
                        "label": r.label,
                        "status": r.status,
                        "last_error": r.last_error,
                        "retry_count": r.retry_count,
                        "last_heal_at": r.last_heal_at,
                        "heal_action": r.heal_action,
                        "critical": r.critical,
                        "updated_at": r.updated_at,
                    }
                    for r in _subsystems.values()
                ],
                "blockers": blockers if not _trade_ready else [],
            }
        )


def record_boot_event(
    event: str,
    *,
    stage: str = "",
    subsystem: str = "",
    detail: str = "",
    level: str = "info",
) -> None:
    entry = {
        "ts": _utc_now_iso(),
        "event": event,
        "stage": stage,
        "subsystem": subsystem,
        "detail": detail,
        "level": level,
    }
    with _lock:
        _boot_log.append(entry)
    log_engine(
        f"boot:{event}"
        + (f" stage={stage}" if stage else "")
        + (f" subsystem={subsystem}" if subsystem else "")
        + (f" {detail}" if detail else "")
    )


def init_boot_pipeline() -> None:
    """Call once at process start."""
    global _boot_started_mono, _trade_ready, _trade_ready_at, _current_stage
    _boot_started_mono = time.monotonic()
    _trade_ready = False
    _trade_ready_at = ""
    _current_stage = BootStage.A.value
    with _lock:
        for rec in _stages.values():
            rec.status = StepStatus.PENDING.value
            rec.started_at = ""
            rec.completed_at = ""
            rec.elapsed_ms = None
            rec.last_error = ""
            rec.retry_count = 0
        for rec in _subsystems.values():
            rec.status = StepStatus.PENDING.value
            rec.last_error = ""
            rec.retry_count = 0
            rec.last_heal_at = ""
            rec.heal_action = ""
            rec.updated_at = ""
        _boot_log.clear()
    record_boot_event("pipeline_init", stage=BootStage.A.value)
    mark_stage_running(BootStage.A)
    _publish_snapshot()


def mark_stage_running(stage: BootStage | str) -> None:
    sid = stage.value if isinstance(stage, BootStage) else str(stage)
    global _current_stage
    _current_stage = sid
    now = _utc_now_iso()
    with _lock:
        rec = _stages.get(sid)
        if rec is None:
            return
        if rec.status == StepStatus.PENDING.value:
            rec.started_at = now
        rec.status = StepStatus.RUNNING.value
    record_boot_event("stage_running", stage=sid)
    _publish_snapshot()


def mark_stage_ok(stage: BootStage | str, *, detail: str = "") -> None:
    sid = stage.value if isinstance(stage, BootStage) else str(stage)
    now = _utc_now_iso()
    with _lock:
        rec = _stages.get(sid)
        if rec is None:
            return
        rec.status = StepStatus.OK.value
        rec.completed_at = now
        if rec.started_at:
            try:
                started = datetime.fromisoformat(rec.started_at.replace("Z", "+00:00"))
                done = datetime.fromisoformat(now.replace("Z", "+00:00"))
                rec.elapsed_ms = (done - started).total_seconds() * 1000.0
            except Exception:
                pass
        rec.last_error = ""
    record_boot_event("stage_ok", stage=sid, detail=detail)
    _publish_snapshot()


def mark_stage_degraded(stage: BootStage | str, *, error: str = "") -> None:
    sid = stage.value if isinstance(stage, BootStage) else str(stage)
    with _lock:
        rec = _stages.get(sid)
        if rec is None:
            return
        rec.status = StepStatus.DEGRADED.value
        rec.last_error = error
        rec.retry_count += 1
    record_boot_event("stage_degraded", stage=sid, detail=error, level="warn")
    _publish_snapshot()


def mark_stage_failed(stage: BootStage | str, *, error: str = "") -> None:
    sid = stage.value if isinstance(stage, BootStage) else str(stage)
    with _lock:
        rec = _stages.get(sid)
        if rec is None:
            return
        rec.status = StepStatus.FAILED.value
        rec.last_error = error
        rec.retry_count += 1
    record_boot_event("stage_failed", stage=sid, detail=error, level="error")
    _publish_snapshot()


def mark_subsystem(
    subsystem: SubsystemId | str,
    status: StepStatus | str,
    *,
    error: str = "",
    heal_action: str = "",
) -> None:
    sid = subsystem.value if isinstance(subsystem, SubsystemId) else str(subsystem)
    st = status.value if isinstance(status, StepStatus) else str(status)
    now = _utc_now_iso()
    with _lock:
        rec = _subsystems.get(sid)
        if rec is None:
            return
        rec.status = st
        rec.updated_at = now
        if error:
            rec.last_error = error
        if heal_action:
            rec.heal_action = heal_action
            rec.last_heal_at = now
    if st in (StepStatus.FAILED.value, StepStatus.DEGRADED.value):
        record_boot_event(
            f"subsystem_{st}",
            subsystem=sid,
            detail=error or heal_action,
            level="warn" if st == StepStatus.DEGRADED.value else "error",
        )
    _publish_snapshot()


def mark_subsystem_healing(subsystem: SubsystemId | str, *, action: str) -> None:
    mark_subsystem(subsystem, StepStatus.HEALING, heal_action=action)
    _publish_snapshot()


def _stage_index(stage_id: str) -> int:
    order = [s.value for s, _, _ in _STAGE_DEFS]
    try:
        return order.index(stage_id)
    except ValueError:
        return -1


def _advance_current_stage(target: BootStage) -> None:
    global _current_stage
    if _stage_index(target.value) > _stage_index(_current_stage):
        _current_stage = target.value


def _evaluate_trade_ready_contract() -> tuple[bool, list[str]]:
    """Strict readiness — all critical subsystems OK, no critical failures."""
    blockers: list[str] = []
    with _lock:
        for rec in _subsystems.values():
            if not rec.critical:
                continue
            if rec.status in (StepStatus.FAILED.value, StepStatus.PENDING.value):
                blockers.append(f"{rec.id}:{rec.status}")
        for rec in _stages.values():
            if not rec.critical:
                continue
            if rec.status == StepStatus.FAILED.value:
                blockers.append(f"stage_{rec.id}:failed")
    return (len(blockers) == 0, blockers)


def _sync_from_cached_sources() -> None:
    """Background only — read cached health_light + system_state."""
    global _trade_ready, _trade_ready_at, _estimated_ready_sec

    try:
        from api.health_light import get_health_light_response

        hl = get_health_light_response()
    except Exception:
        hl = {}

    try:
        from system.system_state import get_system_state

        ss = get_system_state().snapshot()
    except Exception:
        ss = {}

    ig_ok = hl.get("ig_available") is True
    yahoo_ok = hl.get("yahoo_available") is True
    feeds = hl.get("data_feeds") or {}
    hub = feeds.get("hub") or {}
    fresh = int(hub.get("fresh_count") or 0)
    total = int(hub.get("total") or 0)
    feeds_ok = fresh >= 1 or bool(hl.get("stack_tpm"))
    routing = hl.get("routing_state") or {}
    armed = int(routing.get("armed") or 0)
    routing_ok = armed > 0 and not (
        routing.get("degraded") and armed == 0
    )
    exec_ok = bool(hl.get("execution_loop_active"))
    stacked = bool(hl.get("stacked_sweep_alive"))
    sweep = int(hl.get("rotation_sweep_count") or 0)
    feed_stall = bool(hl.get("feed_stall"))
    ready_phase = bool(ss.get("ready")) or str(ss.get("phase") or "") in ("READY", "G5")
    if not ready_phase and exec_ok and stacked and sweep >= 50:
        ready_phase = True

    # Stage A — core agent
    if ready_phase or hl.get("agent_online"):
        mark_stage_ok(BootStage.A)
        mark_subsystem(SubsystemId.CORE_AGENT, StepStatus.OK)
        _advance_current_stage(BootStage.B)
    else:
        mark_stage_running(BootStage.A)
        mark_subsystem(SubsystemId.CORE_AGENT, StepStatus.RUNNING)

    # Stage B — feeds
    if feeds_ok and not feed_stall:
        mark_stage_ok(BootStage.B)
        mark_subsystem(SubsystemId.FEEDS, StepStatus.OK)
        _advance_current_stage(BootStage.C)
    elif feed_stall:
        mark_stage_degraded(BootStage.B, error="feed_stall")
        mark_subsystem(SubsystemId.FEEDS, StepStatus.DEGRADED, error="feed_stall")
    elif ready_phase:
        mark_stage_running(BootStage.B)
        mark_subsystem(SubsystemId.FEEDS, StepStatus.RUNNING)

    # Stage C — external APIs (cached)
    if ig_ok and yahoo_ok:
        mark_stage_ok(BootStage.C)
        mark_subsystem(SubsystemId.IG, StepStatus.OK)
        mark_subsystem(SubsystemId.YAHOO, StepStatus.OK)
        _advance_current_stage(BootStage.D)
    else:
        if not ig_ok:
            mark_subsystem(
                SubsystemId.IG,
                StepStatus.DEGRADED if ready_phase else StepStatus.RUNNING,
                error="ig_unavailable",
            )
        if not yahoo_ok:
            mark_subsystem(
                SubsystemId.YAHOO,
                StepStatus.DEGRADED if ready_phase else StepStatus.RUNNING,
                error="yahoo_unavailable",
            )
        if ready_phase and (ig_ok or yahoo_ok):
            mark_stage_degraded(BootStage.C, error="partial_api")
        elif ready_phase:
            mark_stage_running(BootStage.C)

    # Stage D — routing
    if routing_ok:
        mark_stage_ok(BootStage.D)
        mark_subsystem(SubsystemId.ROUTING, StepStatus.OK)
        _advance_current_stage(BootStage.E)
    elif ready_phase:
        mark_stage_degraded(BootStage.D, error=f"armed={armed}")
        mark_subsystem(SubsystemId.ROUTING, StepStatus.DEGRADED, error=f"armed={armed}")

    # Stage E — governance (non-critical subsystem)
    gov_ok = True
    try:
        from cockpit.emergency import COCKPIT_EMERGENCY_OVERRIDE_ACTIVE

        if COCKPIT_EMERGENCY_OVERRIDE_ACTIVE:
            gov_ok = False
    except Exception:
        pass
    if gov_ok:
        mark_stage_ok(BootStage.E)
        mark_subsystem(SubsystemId.GOVERNANCE, StepStatus.OK)
        _advance_current_stage(BootStage.F)
    elif ready_phase:
        mark_stage_degraded(BootStage.E, error="emergency_override")
        mark_subsystem(SubsystemId.GOVERNANCE, StepStatus.DEGRADED, error="override_active")

    # Stage F — execution loop
    if exec_ok and stacked and sweep > 0:
        mark_stage_ok(BootStage.F)
        mark_subsystem(SubsystemId.EXECUTION, StepStatus.OK)
        _advance_current_stage(BootStage.G)
    elif ready_phase:
        err = "exec_inactive" if not exec_ok else "sweep_stalled"
        mark_stage_degraded(BootStage.F, error=err)
        mark_subsystem(SubsystemId.EXECUTION, StepStatus.DEGRADED, error=err)

    # Stage G — trade-ready contract
    contract_ok, blockers = _evaluate_trade_ready_contract()
    extra_ok = (
        feeds_ok
        and routing_ok
        and exec_ok
        and stacked
        and ig_ok
        and yahoo_ok
        and gov_ok
        and ready_phase
    )
    if contract_ok and extra_ok:
        mark_stage_ok(BootStage.G, detail="trade_ready")
        if not _trade_ready:
            _trade_ready = True
            _trade_ready_at = _utc_now_iso()
            record_boot_event("trade_ready", stage=BootStage.G.value, detail="all_critical_ok")
    else:
        if ready_phase:
            mark_stage_running(BootStage.G)
        _estimated_ready_sec = max(5.0, 30.0 - (time.monotonic() - _boot_started_mono))

    elapsed = time.monotonic() - _boot_started_mono
    with _lock:
        _snapshot.clear()
        _snapshot.update(
            {
                "ok": True,
                "ts": _utc_now_iso(),
                "boot_elapsed_sec": round(elapsed, 2),
                "current_stage": _current_stage,
                "trade_ready": _trade_ready,
                "trade_ready_at": _trade_ready_at,
                "estimated_ready_sec": _estimated_ready_sec,
                "stages": [
                    {
                        "id": r.id,
                        "label": r.label,
                        "status": r.status,
                        "started_at": r.started_at,
                        "completed_at": r.completed_at,
                        "elapsed_ms": r.elapsed_ms,
                        "last_error": r.last_error,
                        "retry_count": r.retry_count,
                        "critical": r.critical,
                    }
                    for r in _stages.values()
                ],
                "subsystems": [
                    {
                        "id": r.id,
                        "label": r.label,
                        "status": r.status,
                        "last_error": r.last_error,
                        "retry_count": r.retry_count,
                        "last_heal_at": r.last_heal_at,
                        "heal_action": r.heal_action,
                        "critical": r.critical,
                        "updated_at": r.updated_at,
                    }
                    for r in _subsystems.values()
                ],
                "blockers": blockers if not (_trade_ready and extra_ok) else [],
                "health_light": {
                    "execution_loop_active": exec_ok,
                    "stacked_sweep_alive": stacked,
                    "rotation_sweep_count": sweep,
                    "routing_armed": armed,
                    "feed_fresh": f"{fresh}/{total}",
                },
            }
        )
    try:
        from system.unified_runtime_state import update_from_boot_snapshot, update_from_health_light

        snap_copy = dict(_snapshot)
        update_from_boot_snapshot(snap_copy)
        update_from_health_light(hl)
        snap_copy["startup_diagnostics"] = _build_startup_diagnostics(
            ig_ok=ig_ok,
            yahoo_ok=yahoo_ok,
            feeds_ok=feeds_ok,
            routing_ok=routing_ok,
            exec_ok=exec_ok,
            stacked=stacked,
            sweep=sweep,
            armed=armed,
            feed_stall=feed_stall,
        )
        with _lock:
            _snapshot["startup_diagnostics"] = snap_copy["startup_diagnostics"]
    except Exception:
        pass


def _build_startup_diagnostics(
    *,
    ig_ok: bool,
    yahoo_ok: bool,
    feeds_ok: bool,
    routing_ok: bool,
    exec_ok: bool,
    stacked: bool,
    sweep: int,
    armed: int,
    feed_stall: bool,
) -> dict[str, bool]:
    """Part 7 checklist — surfaced on splash via /api/boot_status."""
    try:
        from system.unified_runtime_state import snapshot as urs_snapshot

        diag = dict(urs_snapshot().get("startup_diagnostics") or {})
    except Exception:
        diag = {}
    diag["ig_connectivity_validated"] = bool(ig_ok)
    diag["feed_heartbeat_live"] = bool(feeds_ok and not feed_stall)
    diag["routing_armed"] = bool(routing_ok and armed > 0)
    diag["execution_loop_ready"] = bool(exec_ok and stacked)
    diag["rotation_logic_active"] = bool(sweep > 0)
    return diag


def _heal_loop() -> None:
    from system.boot.subsystem_healer import run_targeted_heal

    while not _stop.wait(_HEAL_INTERVAL_SEC):
        try:
            with _lock:
                subs = {
                    k: (v.status, v.retry_count, v.critical)
                    for k, v in _subsystems.items()
                }
            for sid, (status, retries, critical) in subs.items():
                if status not in (StepStatus.DEGRADED.value, StepStatus.FAILED.value):
                    continue
                if retries >= _SUBSYSTEM_RETRY_MAX:
                    continue
                run_targeted_heal(sid)
        except Exception as exc:
            log_engine(f"boot:healer_loop {type(exc).__name__}: {exc}")


def _refresh_loop() -> None:
    while not _stop.wait(_REFRESH_INTERVAL_SEC):
        try:
            _sync_from_cached_sources()
        except Exception as exc:
            log_engine(f"boot:refresh_loop {type(exc).__name__}: {exc}")


def get_boot_status_snapshot() -> dict[str, Any]:
    """O(1) copy — safe for HTTP hot path."""
    with _lock:
        return dict(_snapshot) if _snapshot else {"ok": True, "ts": _utc_now_iso(), "trade_ready": False}


def get_boot_log_entries(*, limit: int = 100) -> list[dict[str, Any]]:
    with _lock:
        items = list(_boot_log)
    return items[-limit:]


def start_boot_orchestrator() -> None:
    global _refresher_thread, _healer_thread
    init_boot_pipeline()
    if _refresher_thread is not None and _refresher_thread.is_alive():
        return
    _stop.clear()
    _refresher_thread = threading.Thread(
        target=_refresh_loop, name="boot-orchestrator-refresh", daemon=True
    )
    _healer_thread = threading.Thread(
        target=_heal_loop, name="boot-subsystem-healer", daemon=True
    )
    _refresher_thread.start()
    _healer_thread.start()
    record_boot_event("orchestrator_started")


def stop_boot_orchestrator() -> None:
    _stop.set()


def reset_boot_orchestrator_for_tests() -> None:
    stop_boot_orchestrator()
    global _refresher_thread, _healer_thread, _trade_ready, _trade_ready_at
    _refresher_thread = None
    _healer_thread = None
    _trade_ready = False
    _trade_ready_at = ""
    init_boot_pipeline()
