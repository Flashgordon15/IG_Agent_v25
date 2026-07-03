"""Gate progress watchdog — advances wedged G3/G4/G5 off the hydration thread."""

from __future__ import annotations

import threading
import time
from typing import Any

from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception
from system.system_state import GATE_IDS, GateStatus

_WATCHED_COORDINATOR: Any | None = None
_GATE_WATCHDOG_THREAD: threading.Thread | None = None
_watchdog_started_mono: float | None = None

_G3_FORCE_BOOT_SEC = 30.0
_G4_STUCK_SEC = 25.0
_G5_STUCK_SEC = 35.0


def _gate_status(snap: dict[str, Any], gate_id: str) -> str:
    g = (snap.get("gates") or {}).get(gate_id) or {}
    return str(g.get("status") or "").lower()


def _boot_elapsed_sec(snap: dict[str, Any] | None) -> float:
    if snap is not None:
        epoch = float(snap.get("started_at_epoch") or 0)
        if epoch > 0:
            return time.time() - epoch
    if _watchdog_started_mono is not None:
        return time.monotonic() - _watchdog_started_mono
    return 0.0


def _gate_progress_watchdog_loop() -> None:
    while True:
        time.sleep(3.0)
        coord = _WATCHED_COORDINATOR
        if coord is None:
            continue
        try:
            from system.boot.gate3_runner import _apply_g3_boot_heal, try_heal_stuck_g3
            from system.boot.gate4_runner import try_heal_stuck_g4
            from system.boot.gate5_runner import force_gate5_ready_degraded, try_heal_stuck_g5
            from system.boot.gate_sideband import gate_is_done, is_gate_sideband

            state = coord.state

            # Heal first — these paths tolerate SystemState lock contention.
            try_heal_stuck_g3(min_elapsed_sec=0)
            try_heal_stuck_g4(min_elapsed_sec=0)
            try_heal_stuck_g5(min_elapsed_sec=0, context=coord.context)
            coord.sync_all_sideband_gates()

            snap = state.try_snapshot(timeout=0.5)
            boot_elapsed = _boot_elapsed_sec(snap)

            if (
                not gate_is_done(state, "G3")
                and boot_elapsed >= _G3_FORCE_BOOT_SEC
            ):
                log_engine(
                    f"gate_watchdog: forcing G3 complete boot_elapsed={boot_elapsed:.0f}s"
                )
                _apply_g3_boot_heal(state, epic=None, detail="watchdog_force")

            if not gate_is_done(state, "G5") and boot_elapsed >= _G5_STUCK_SEC:
                snap_percent = int((snap or {}).get("percent") or 0)
                g5_running = (
                    _gate_status(snap, "G5") == "running"
                    if snap is not None
                    else not gate_is_done(state, "G5")
                )
                if g5_running or snap_percent >= 92:
                    log_engine(
                        f"gate_watchdog: forcing G5 READY boot_elapsed={boot_elapsed:.0f}s"
                    )
                    force_gate5_ready_degraded(
                        state,
                        context=coord.context,
                        detail="watchdog_force",
                    )

            from system.boot.gate4_runner import _gate4_loops_built

            g4_stuck = (
                gate_is_done(state, "G3")
                and not gate_is_done(state, "G4")
                and _gate4_loops_built() <= 0
                and boot_elapsed >= _G4_STUCK_SEC
            )
            if g4_stuck:
                from system.boot.gate4_runner import force_gate4_orchestrator_build

                log_engine(
                    f"gate_watchdog: G4 incomplete {boot_elapsed:.0f}s loops=0 — force-build"
                )
                force_gate4_orchestrator_build(state=state, context=coord.context)

            if snap is None:
                for gate_id in GATE_IDS[3:]:
                    prev = GATE_IDS[GATE_IDS.index(gate_id) - 1]
                    if not (gate_is_done(state, prev) or is_gate_sideband(prev)):
                        break
                    if gate_is_done(state, gate_id) or is_gate_sideband(gate_id):
                        continue
                    runner = coord._gate_runners.get(gate_id)
                    if runner is None:
                        continue
                    log_engine(
                        f"gate_watchdog: spawning {gate_id} runner "
                        f"(prev={prev} done, snap=lock_busy)"
                    )
                    threading.Thread(
                        target=lambda g=gate_id: coord._run_gate(g),
                        name=f"gate-watchdog-{gate_id}",
                        daemon=True,
                    ).start()
                    break
                continue

            snap = state.try_snapshot(timeout=0.5) or snap
            for gate_id in GATE_IDS[3:]:
                prev = GATE_IDS[GATE_IDS.index(gate_id) - 1]
                prev_done = gate_is_done(state, prev) or is_gate_sideband(prev)
                if not prev_done and _gate_status(snap, prev) != "complete":
                    break
                if gate_is_done(state, gate_id) or _gate_status(snap, gate_id) == "complete":
                    continue
                status = _gate_status(snap, gate_id)
                if status == "running":
                    continue
                stuck_sec = _G4_STUCK_SEC if gate_id == "G4" else _G5_STUCK_SEC
                if boot_elapsed < stuck_sec and gate_id == "G4":
                    continue
                runner = coord._gate_runners.get(gate_id)
                if runner is None:
                    continue
                log_engine(f"gate_watchdog: spawning {gate_id} runner (prev={prev} done)")
                threading.Thread(
                    target=lambda g=gate_id: coord._run_gate(g),
                    name=f"gate-watchdog-{gate_id}",
                    daemon=True,
                ).start()
                break
        except Exception as exc:
            log_guarded_exception("gate_progress_watchdog", exc)


def start_gate_progress_watchdog(coordinator: Any) -> None:
    global _GATE_WATCHDOG_THREAD, _WATCHED_COORDINATOR, _watchdog_started_mono
    _WATCHED_COORDINATOR = coordinator
    _watchdog_started_mono = time.monotonic()
    log_engine("gate_watchdog: progress monitor started")
    if _GATE_WATCHDOG_THREAD is not None and _GATE_WATCHDOG_THREAD.is_alive():
        return
    _GATE_WATCHDOG_THREAD = threading.Thread(
        target=_gate_progress_watchdog_loop,
        name="gate-progress-watchdog",
        daemon=True,
    )
    _GATE_WATCHDOG_THREAD.start()
