"""
Iron Gauge — unified startup cage around G1–G5, post-ready critical path, and telemetry.

Single source of truth for:
  - ordered boot phases and stall budgets
  - recovery dispatch (reuses gate_watchdog + subsystem_healer + orchestrator lazy arm)
  - launcher tier (green / amber / red)
  - /api/iron_gauge snapshot for cockpit + launcher
"""

from __future__ import annotations

import enum
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from system.engine_log import log_engine

_GAUGE_LOG_MAX = 128
_LOCK = threading.RLock()
_BOOT_STARTED_MONO = time.monotonic()
_RECOVERY_LOG: deque[dict[str, Any]] = deque(maxlen=_GAUGE_LOG_MAX)
_HEAL_COOLDOWN_SEC = 15.0
_last_heal_mono: dict[str, float] = {}


class GaugePhase(str, enum.Enum):
    BIND = "bind"
    G1 = "G1"
    G2 = "G2"
    G3 = "G3"
    G4 = "G4"
    G5 = "G5"
    POST_EXECUTION_PLANE = "post_execution_plane"
    POST_HEALTH_LIGHT = "post_health_light"
    POST_ORCHESTRATOR = "post_orchestrator"
    POST_KERNEL = "post_kernel"
    POST_TAIL = "post_tail"
    TELEMETRY = "telemetry"
    COMPLETE = "complete"


class PhaseStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"


# Ordered post-G5 critical path — must not block later steps on slow optional work.
POST_READY_CRITICAL: tuple[GaugePhase, ...] = (
    GaugePhase.POST_EXECUTION_PLANE,
    GaugePhase.POST_HEALTH_LIGHT,
    GaugePhase.POST_ORCHESTRATOR,
    GaugePhase.POST_KERNEL,
)

# Stall budgets (seconds) before recovery dispatch.
STALL_BUDGET_SEC: dict[str, float] = {
    GaugePhase.G3.value: 30.0,
    GaugePhase.G4.value: 25.0,
    GaugePhase.G5.value: 35.0,
    GaugePhase.POST_EXECUTION_PLANE.value: 20.0,
    GaugePhase.POST_HEALTH_LIGHT.value: 10.0,
    GaugePhase.POST_ORCHESTRATOR.value: 45.0,
    GaugePhase.POST_KERNEL.value: 120.0,
    GaugePhase.TELEMETRY.value: 90.0,
}

_GATE_TO_BOOT_STAGE: dict[str, str] = {
    "G1": "A",
    "G2": "C",
    "G3": "B",
    "G4": "D",
    "G5": "F",
}

_phase_status: dict[str, str] = {p.value: PhaseStatus.PENDING.value for p in GaugePhase}
_phase_started_mono: dict[str, float] = {}
_phase_detail: dict[str, str] = {}
_heal_attempts: dict[str, int] = {}
_post_ready_index: int = -1
_sealed_logged: bool = False
_snapshot_cache: dict[str, Any] = {}
_snapshot_cache_mono: float = 0.0
_SNAPSHOT_CACHE_SEC = 0.5

# Launcher splash steps 1–9 (aligned with launcher_status.sh).
LAUNCHER_STEP_FOR_PHASE: dict[str, int] = {
    GaugePhase.BIND.value: 1,
    GaugePhase.G1.value: 2,
    GaugePhase.G2.value: 3,
    GaugePhase.G3.value: 4,
    GaugePhase.G4.value: 5,
    GaugePhase.G5.value: 6,
    GaugePhase.POST_EXECUTION_PLANE.value: 7,
    GaugePhase.POST_HEALTH_LIGHT.value: 7,
    GaugePhase.POST_ORCHESTRATOR.value: 7,
    GaugePhase.POST_KERNEL.value: 8,
    GaugePhase.POST_TAIL.value: 8,
    GaugePhase.TELEMETRY.value: 8,
    GaugePhase.COMPLETE.value: 9,
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _record_recovery(action: str, *, phase: str = "", detail: str = "") -> None:
    entry = {
        "ts": _utc_now_iso(),
        "action": action,
        "phase": phase,
        "detail": detail[:240],
    }
    with _LOCK:
        _RECOVERY_LOG.append(entry)
    log_engine(f"IronGauge: recovery {action} phase={phase} {detail[:120]}")


def reset_iron_gauge_for_tests() -> None:
    global _BOOT_STARTED_MONO, _post_ready_index, _sealed_logged
    _BOOT_STARTED_MONO = time.monotonic()
    _post_ready_index = -1
    _sealed_logged = False
    global _snapshot_cache, _snapshot_cache_mono
    _snapshot_cache = {}
    _snapshot_cache_mono = 0.0
    with _LOCK:
        for p in GaugePhase:
            _phase_status[p.value] = PhaseStatus.PENDING.value
        _phase_started_mono.clear()
        _phase_detail.clear()
        _heal_attempts.clear()
        _RECOVERY_LOG.clear()
    _last_heal_mono.clear()


def init_iron_gauge() -> None:
    """Process start — first gauge milestones after API bind."""
    global _BOOT_STARTED_MONO
    _BOOT_STARTED_MONO = time.monotonic()
    iron_gauge_mark(GaugePhase.BIND, PhaseStatus.RUNNING)
    iron_gauge_mark(GaugePhase.BIND, PhaseStatus.OK, detail="api_bind_scheduled")


def iron_gauge_mark(
    phase: GaugePhase | str,
    status: PhaseStatus | str = PhaseStatus.OK,
    *,
    detail: str = "",
) -> None:
    pid = phase.value if isinstance(phase, GaugePhase) else str(phase)
    st = status.value if isinstance(status, PhaseStatus) else str(status)
    now = time.monotonic()
    with _LOCK:
        prev = _phase_status.get(pid, PhaseStatus.PENDING.value)
        if st == PhaseStatus.RUNNING.value and prev == PhaseStatus.PENDING.value:
            _phase_started_mono[pid] = now
        if st in (PhaseStatus.OK.value, PhaseStatus.DEGRADED.value, PhaseStatus.FAILED.value):
            if pid not in _phase_started_mono:
                _phase_started_mono[pid] = now
        _phase_status[pid] = st
        if detail:
            _phase_detail[pid] = detail[:240]
        if pid in {p.value for p in POST_READY_CRITICAL} and st == PhaseStatus.OK.value:
            global _post_ready_index
            try:
                idx = [p.value for p in POST_READY_CRITICAL].index(pid)
                _post_ready_index = max(_post_ready_index, idx)
            except ValueError:
                pass
    if st == PhaseStatus.OK.value and pid.startswith("G"):
        try:
            from system.boot.boot_orchestrator import BootStage, mark_stage_ok

            stage = _GATE_TO_BOOT_STAGE.get(pid)
            if stage:
                mark_stage_ok(BootStage(stage), detail=detail or f"iron_gauge:{pid}")
        except Exception:
            pass


def _phase_elapsed_sec(phase_id: str) -> float:
    with _LOCK:
        started = _phase_started_mono.get(phase_id)
    if started is None:
        return 0.0
    return time.monotonic() - started


def _sync_gates_from_system_state() -> None:
    try:
        from system.system_state import GateStatus, get_system_state

        snap = get_system_state().try_snapshot(timeout=0.25)
        if snap is None:
            return
        gates = snap.get("gates") or {}
        for gid in ("G1", "G2", "G3", "G4", "G5"):
            g = gates.get(gid) or {}
            raw = str(g.get("status") or "").lower()
            if raw == GateStatus.COMPLETE.value:
                iron_gauge_mark(gid, PhaseStatus.OK, detail=str(g.get("detail") or "")[:120])
            elif raw == GateStatus.RUNNING.value:
                iron_gauge_mark(gid, PhaseStatus.RUNNING)
            elif raw == GateStatus.FAILED.value:
                iron_gauge_mark(gid, PhaseStatus.FAILED, detail=str(g.get("detail") or ""))
    except Exception:
        pass


def _dispatch_recovery(phase_id: str) -> bool:
    now = time.monotonic()
    last = _last_heal_mono.get(phase_id, 0.0)
    if now - last < _HEAL_COOLDOWN_SEC:
        return False
    _last_heal_mono[phase_id] = now
    with _LOCK:
        _heal_attempts[phase_id] = _heal_attempts.get(phase_id, 0) + 1

    try:
        if phase_id == GaugePhase.G3.value:
            from system.boot.gate3_runner import try_heal_stuck_g3

            if try_heal_stuck_g3(min_elapsed_sec=0):
                _record_recovery("heal_g3", phase=phase_id)
                return True
        if phase_id == GaugePhase.G4.value:
            from system.boot.gate4_runner import try_heal_stuck_g4

            if try_heal_stuck_g4(min_elapsed_sec=0):
                _record_recovery("heal_g4", phase=phase_id)
                return True
        if phase_id == GaugePhase.G5.value:
            from system.boot.gate5_runner import try_heal_stuck_g5

            if try_heal_stuck_g5(min_elapsed_sec=0):
                _record_recovery("heal_g5", phase=phase_id)
                return True
        if phase_id == GaugePhase.POST_ORCHESTRATOR.value:
            from runtime.master_orchestrator import ensure_orchestrator_armed_lazy

            if ensure_orchestrator_armed_lazy():
                _record_recovery("lazy_arm_orchestrator", phase=phase_id)
                return True
        if phase_id in (
            GaugePhase.POST_EXECUTION_PLANE.value,
            GaugePhase.TELEMETRY.value,
        ):
            from system.boot.subsystem_healer import run_targeted_heal

            for sid in ("feeds", "routing", "execution"):
                run_targeted_heal(sid)
            _record_recovery("heal_feeds_routing", phase=phase_id)
            return True
    except Exception as exc:
        _record_recovery(
            f"recovery_failed:{type(exc).__name__}",
            phase=phase_id,
            detail=str(exc),
        )
    return False


def _detect_stalled_phases() -> list[str]:
    stalled: list[str] = []
    with _LOCK:
        statuses = dict(_phase_status)
        started = dict(_phase_started_mono)
    for phase_id, budget in STALL_BUDGET_SEC.items():
        st = statuses.get(phase_id, PhaseStatus.PENDING.value)
        if st not in (PhaseStatus.PENDING.value, PhaseStatus.RUNNING.value):
            continue
        if phase_id not in started and st == PhaseStatus.PENDING.value:
            continue
        elapsed = _phase_elapsed_sec(phase_id) if phase_id in started else (
            time.monotonic() - _BOOT_STARTED_MONO
        )
        if elapsed >= budget:
            stalled.append(phase_id)
    return stalled


def _compute_blockers() -> list[str]:
    blockers: list[str] = []
    with _LOCK:
        statuses = dict(_phase_status)
    for phase in (GaugePhase.G1, GaugePhase.G2, GaugePhase.G3, GaugePhase.G4, GaugePhase.G5):
        st = statuses.get(phase.value, PhaseStatus.PENDING.value)
        if st not in (PhaseStatus.OK.value, PhaseStatus.DEGRADED.value):
            blockers.append(f"gauge:{phase.value}:{st}")
    for phase in POST_READY_CRITICAL:
        st = statuses.get(phase.value, PhaseStatus.PENDING.value)
        if st != PhaseStatus.OK.value:
            blockers.append(f"gauge:{phase.value}:{st}")
    if statuses.get(GaugePhase.TELEMETRY.value) != PhaseStatus.OK.value:
        blockers.append("gauge:telemetry_pending")
    return blockers


def _current_launcher_step() -> int:
    with _LOCK:
        statuses = dict(_phase_status)
    step = 1
    for pid in [p.value for p in GaugePhase]:
        st = statuses.get(pid, PhaseStatus.PENDING.value)
        if st == PhaseStatus.OK.value:
            step = max(step, LAUNCHER_STEP_FOR_PHASE.get(pid, step))
        elif st in (PhaseStatus.RUNNING.value, PhaseStatus.DEGRADED.value):
            step = max(step, LAUNCHER_STEP_FOR_PHASE.get(pid, step))
    return min(step, 9)


def _sync_orchestrator_telemetry() -> None:
    """Mirror RAG stage progress into gauge when iron ledger is warming."""
    try:
        from runtime.master_orchestrator import get_orchestrator_state_snapshot

        orch = get_orchestrator_state_snapshot() or {}
    except Exception:
        return
    stages = orch.get("stage_status") or orch.get("phase_status") or {}
    if not stages:
        return
    pending = sum(1 for v in stages.values() if str(v).upper() == "PENDING")
    success = sum(1 for v in stages.values() if str(v).upper() == "SUCCESS")
    if orch.get("primed") or success >= 7:
        iron_gauge_mark(
            GaugePhase.TELEMETRY,
            PhaseStatus.OK,
            detail=f"orchestrator primed={orch.get('primed')}",
        )
    elif success > 0 or pending < len(stages):
        iron_gauge_mark(GaugePhase.TELEMETRY, PhaseStatus.RUNNING, detail=f"rag={success}/{len(stages)}")


def iron_gauge_tick() -> None:
    """Background tick — sync gates, detect stalls, dispatch recovery, seal complete."""
    if os.environ.get("IG_AGENT_PYTEST", "").strip() == "1":
        return
    global _sealed_logged
    _sync_gates_from_system_state()
    for phase_id in _detect_stalled_phases():
        _dispatch_recovery(phase_id)

    try:
        from api.health_light import get_health_light_response

        hl = get_health_light_response() or {}
    except Exception:
        hl = {}

    exec_ok = bool(hl.get("execution_loop_active"))
    armed = int((hl.get("routing_state") or {}).get("armed") or 0)
    hub = (hl.get("data_feeds") or {}).get("hub") or {}
    fresh = int(hub.get("fresh_count") or 0)

    g5_ok = _phase_status.get(GaugePhase.G5.value) == PhaseStatus.OK.value
    post_ok = _post_ready_seal_met()

    if g5_ok and not post_ok:
        iron_gauge_mark(GaugePhase.POST_EXECUTION_PLANE, PhaseStatus.RUNNING)

    _sync_orchestrator_telemetry()

    if exec_ok and armed > 0 and fresh >= 1:
        if _phase_status.get(GaugePhase.TELEMETRY.value) != PhaseStatus.OK.value:
            iron_gauge_mark(
                GaugePhase.TELEMETRY,
                PhaseStatus.OK,
                detail=f"routes={armed} feeds={fresh}",
            )
    elif g5_ok and _phase_status.get(GaugePhase.TELEMETRY.value) == PhaseStatus.PENDING.value:
        iron_gauge_mark(GaugePhase.TELEMETRY, PhaseStatus.RUNNING)

    critical_ok = g5_ok and post_ok and exec_ok and armed > 0
    if critical_ok and not _sealed_logged:
        iron_gauge_mark(GaugePhase.COMPLETE, PhaseStatus.OK, detail="iron_gauge_sealed")
        _sealed_logged = True
        try:
            from system.boot.boot_orchestrator import record_boot_event

            record_boot_event("iron_gauge_sealed", detail="startup_cage_complete")
        except Exception:
            pass
        _sync_launcher_status_from_gauge(tier="green")


def _sync_launcher_status_from_gauge(*, tier: str = "green") -> None:
    """Mirror gauge launcher_step into logs/launcher_status.json for native splash parity."""
    try:
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        path = root / "logs" / "launcher_status.json"
        step = _current_launcher_step()
        tier_l = str(tier or "green").lower()
        detail = "Iron Gauge sealed — startup cage complete"
        if tier_l == "amber":
            detail = "Iron Gauge sealed (degraded)"
        payload = {
            "phase": "ready" if step >= 9 else "post_ready",
            "status": "Launch complete" if step >= 9 else "Iron Gauge progress",
            "detail": detail,
            "step": step,
            "total_steps": 9,
            "tier": tier_l,
            "ts": _utc_now_iso(),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        import json

        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def _post_ready_critical_met() -> bool:
    return all(
        _phase_status.get(p.value) == PhaseStatus.OK.value for p in POST_READY_CRITICAL
    )


def _post_ready_seal_met() -> bool:
    """Seal when trading plane is live — POST_KERNEL may still be arming in background."""
    required = (
        GaugePhase.POST_EXECUTION_PLANE,
        GaugePhase.POST_HEALTH_LIGHT,
        GaugePhase.POST_ORCHESTRATOR,
    )
    if not all(_phase_status.get(p.value) == PhaseStatus.OK.value for p in required):
        return False
    kernel = _phase_status.get(GaugePhase.POST_KERNEL.value, PhaseStatus.PENDING.value)
    return kernel in (
        PhaseStatus.OK.value,
        PhaseStatus.RUNNING.value,
        PhaseStatus.DEGRADED.value,
    )


def evaluate_startup_tier(*, port: int | None = None, in_process: bool = False) -> str:
    """
    Launcher acceptance tier — green | amber | red.
    Used by boot_acceptance.sh and launcher_post_ready.

    When ``in_process=True`` (called from get_iron_gauge_snapshot), never
    performs HTTP self-fetch — avoids single-worker deadlock on /api/iron_gauge.
    """
    api_port = int(port or os.environ.get("IG_API_PORT", "8080") or 8080)
    in_agent = in_process or os.environ.get("IG_AGENT_IN_PROCESS", "").strip() == "1"
    try:
        import socket

        with socket.create_connection(("127.0.0.1", api_port), timeout=0.5):
            pass
    except OSError:
        return "red"

    # In-agent: use live gauge state (O(1)).
    if _phase_status.get(GaugePhase.COMPLETE.value) == PhaseStatus.OK.value:
        return "green"
    if _post_ready_critical_met():
        try:
            from api.health_light import get_health_light_response

            hl = get_health_light_response() or {}
            if bool(hl.get("execution_loop_active")) and int(
                (hl.get("routing_state") or {}).get("armed") or 0
            ) > 0:
                return "green"
        except Exception:
            pass

    try:
        from api.health_light import get_health_light_response

        hl = get_health_light_response() or {}
    except Exception:
        hl = {}

    if not hl and not in_agent:
        try:
            import json
            import urllib.request

            with urllib.request.urlopen(
                f"http://127.0.0.1:{api_port}/api/iron_gauge", timeout=2.0
            ) as resp:
                gauge = json.loads(resp.read().decode("utf-8"))
            tier = str(gauge.get("tier") or "").strip().lower()
            if tier in ("green", "amber", "red"):
                return tier
        except Exception:
            pass
        try:
            import json
            import urllib.request

            with urllib.request.urlopen(
                f"http://127.0.0.1:{api_port}/api/health_light", timeout=2.0
            ) as resp:
                hl = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return "amber"

    if not hl:
        return "amber"

    exec_ok = bool(hl.get("execution_loop_active"))
    armed = int((hl.get("routing_state") or {}).get("armed") or 0)

    try:
        from system.boot.boot_orchestrator import get_boot_status_snapshot

        boot = get_boot_status_snapshot()
        if bool(boot.get("trade_ready")) and exec_ok:
            return "green"
    except Exception:
        pass

    try:
        from system.system_state import get_system_state

        snap = get_system_state().try_snapshot(timeout=0.25)
        if snap:
            phase = str(snap.get("phase") or "")
            ready = bool(snap.get("ready"))
            status = str(snap.get("phase_label") or snap.get("status") or "").upper()
            if ready and exec_ok and (armed > 0 or phase in ("READY", "G5")):
                return "green"
            if phase in ("G3", "G4", "G5", "READY") or status in (
                "HYDRATING",
                "OPERATIONAL",
                "DEGRADED",
            ):
                return "amber"
    except Exception:
        pass

    if hl.get("agent_online") is not False:
        return "amber"
    return "red"


def get_iron_gauge_snapshot() -> dict[str, Any]:
    """O(1) snapshot for /api/iron_gauge — cockpit + launcher."""
    global _snapshot_cache, _snapshot_cache_mono
    now = time.monotonic()
    if _snapshot_cache and (now - _snapshot_cache_mono) < _SNAPSHOT_CACHE_SEC:
        return dict(_snapshot_cache)

    iron_cage: dict[str, Any] = {}
    boot: dict[str, Any] = {}
    hl: dict[str, Any] = {}
    orchestrator: dict[str, Any] = {}
    try:
        from system.iron_cage_readiness import fast_iron_cage_status_snapshot

        iron_cage = fast_iron_cage_status_snapshot()
    except Exception:
        pass
    try:
        from system.boot.boot_orchestrator import get_boot_status_snapshot

        boot = get_boot_status_snapshot()
    except Exception:
        pass
    try:
        from api.health_light import get_health_light_response

        hl = get_health_light_response() or {}
    except Exception:
        pass
    try:
        from runtime.master_orchestrator import read_orchestrator_snapshot_fast

        orchestrator = read_orchestrator_snapshot_fast() or {}
    except Exception:
        try:
            from runtime.master_orchestrator import get_orchestrator_state_snapshot

            orchestrator = get_orchestrator_state_snapshot() or {}
        except Exception:
            orchestrator = {}

    with _LOCK:
        phases = {
            pid: {
                "status": _phase_status.get(pid, PhaseStatus.PENDING.value),
                "elapsed_sec": round(_phase_elapsed_sec(pid), 2)
                if pid in _phase_started_mono
                else None,
                "detail": _phase_detail.get(pid, ""),
                "heal_attempts": _heal_attempts.get(pid, 0),
                "stall_budget_sec": STALL_BUDGET_SEC.get(pid),
            }
            for pid in [p.value for p in GaugePhase]
        }
        recovery = list(_RECOVERY_LOG)[-32:]
        post_index = _post_ready_index

    tier = evaluate_startup_tier(in_process=True)
    sealed = _phase_status.get(GaugePhase.COMPLETE.value) == PhaseStatus.OK.value
    blockers = _compute_blockers()
    if not sealed:
        blockers = blockers + list(iron_cage.get("blockers") or boot.get("blockers") or [])

    body = {
        "ok": True,
        "ts": _utc_now_iso(),
        "tier": tier,
        "sealed": sealed,
        "boot_elapsed_sec": round(time.monotonic() - _BOOT_STARTED_MONO, 2),
        "launcher_step": _current_launcher_step(),
        "launcher_total_steps": 9,
        "post_ready_critical_index": post_index,
        "post_ready_critical_met": _post_ready_critical_met(),
        "phases": phases,
        "post_ready_order": [p.value for p in POST_READY_CRITICAL],
        "recovery_log": recovery,
        "blockers": blockers[:24],
        "trade_ready": bool(iron_cage.get("trade_ready") or boot.get("trade_ready")),
        "iron_cage": iron_cage,
        "orchestrator": {
            "primed": orchestrator.get("primed"),
            "healthy": orchestrator.get("healthy"),
            "stage_status": orchestrator.get("stage_status") or {},
            "iron_ledger": orchestrator.get("iron_ledger"),
        },
        "boot_status": {
            "current_stage": boot.get("current_stage"),
            "trade_ready": boot.get("trade_ready"),
            "blockers": boot.get("blockers") or [],
        },
        "execution": {
            "loop_active": bool(hl.get("execution_loop_active")),
            "routes_armed": int((hl.get("routing_state") or {}).get("armed") or 0),
            "feeds_fresh": int(
                ((hl.get("data_feeds") or {}).get("hub") or {}).get("fresh_count") or 0
            ),
        },
    }
    _snapshot_cache = dict(body)
    _snapshot_cache_mono = now
    return body


def enforce_post_ready_order(phase: GaugePhase) -> None:
    """Raise if critical post-ready steps run out of order (programmer error)."""
    if phase not in POST_READY_CRITICAL:
        return
    idx = POST_READY_CRITICAL.index(phase)
    if idx == 0:
        return
    required = POST_READY_CRITICAL[idx - 1]
    prev = _phase_status.get(required.value)
    if prev != PhaseStatus.OK.value:
        log_engine(
            f"IronGauge: WARN post-ready order — {phase.value} before {required.value} "
            f"(prev={prev}); continuing (recovery will re-arm)"
        )
