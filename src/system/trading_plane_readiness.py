"""
Trading plane readiness — single source of truth for live loop execution.

Gate 5 historically flipped SystemState.ready while V6 skeleton loops were still
placeholders (orchestrator registered but ``_running=False``). Health then reported
``trading_loops_not_running`` despite boot metrics showing 7/7 accepting ticks.
"""

from __future__ import annotations

import threading
import time
from typing import Any

_REPAIR_LOCK = threading.Lock()
_LAST_REPAIR_MONO = 0.0
_REPAIR_COOLDOWN_SEC = 45.0


def _loop_thread_alive(loop: Any) -> bool:
    try:
        return bool(loop.is_running())
    except Exception:
        return False


def describe_trading_plane() -> dict[str, Any]:
    """Structured snapshot for health, logs, and repair decisions."""
    from api.agent_control import get_trading_loop, is_paused

    orch = get_trading_loop()
    paused = is_paused()
    loops = list(getattr(orch, "loops", []) or []) if orch is not None else []
    skeleton = bool(loops and getattr(loops[0], "_skeleton", False))
    materialized = bool(getattr(orch, "_v6_materialized", False)) if orch else False
    skeleton_mode = bool(getattr(orch, "_v6_skeleton_mode", False)) if orch else False
    orch_running = bool(orch.is_running()) if orch is not None else False
    live_threads = sum(1 for lp in loops if _loop_thread_alive(lp))

    try:
        from system.gate_activity import seconds_since_last_gate_eval

        gate_age = seconds_since_last_gate_eval()
    except Exception:
        gate_age = None

    try:
        from system.system_state import get_system_state

        boot_ready = bool(get_system_state().snapshot_model().ready)
    except Exception:
        boot_ready = False

    blockers: list[str] = []
    if paused:
        blockers.append("api_paused")
    if orch is None:
        blockers.append("orchestrator_not_registered")
    if skeleton_mode and not materialized:
        blockers.append("v6_skeleton_not_materialized")
    if skeleton:
        blockers.append("skeleton_loops")
    if not orch_running:
        blockers.append("orchestrator_not_running")
    if live_threads == 0 and loops:
        blockers.append("no_live_loop_threads")

    live = len(blockers) == 0
    return {
        "live": live,
        "paused": paused,
        "boot_ready": boot_ready,
        "orchestrator_registered": orch is not None,
        "orchestrator_running": orch_running,
        "v6_skeleton_mode": skeleton_mode,
        "v6_materialized": materialized,
        "loop_count": len(loops),
        "live_loop_threads": live_threads,
        "gate_age_sec": gate_age,
        "blockers": blockers,
    }


def is_trading_plane_live() -> bool:
    """True only when real TradingLoop threads are running under a live orchestrator."""
    return bool(describe_trading_plane().get("live"))


def repair_trading_plane_if_stuck(*, reason: str = "auto") -> dict[str, Any]:
    """
    Idempotent recovery — re-schedule V6 handoff or start the orchestrator when
    boot marked READY but the trading plane never came online.
    """
    global _LAST_REPAIR_MONO

    status = describe_trading_plane()
    if status.get("live"):
        return {"ok": True, "action": "already_live", **status}

    now = time.monotonic()
    with _REPAIR_LOCK:
        if now - _LAST_REPAIR_MONO < _REPAIR_COOLDOWN_SEC:
            return {
                "ok": False,
                "action": "repair_cooldown",
                "cooldown_sec": _REPAIR_COOLDOWN_SEC,
                **status,
            }
        _LAST_REPAIR_MONO = now

    from api.agent_control import get_trading_loop
    from system.engine_log import log_engine

    orch = get_trading_loop()
    if orch is None:
        log_engine(f"trading_plane_repair: skipped ({reason}) — no orchestrator registered")
        return {"ok": False, "action": "no_orchestrator", **status}

    action = "none"
    try:
        skeleton_mode = bool(getattr(orch, "_v6_skeleton_mode", False))
        materialized = bool(getattr(orch, "_v6_materialized", False))
        if skeleton_mode and not materialized:
            from runtime.market_orchestrator import ensure_v6_trading_plane_materialized

            cfg = getattr(orch, "config", None) or getattr(orch, "_config", None)
            rest = getattr(orch, "_v6_rest_client", None)
            ensure_v6_trading_plane_materialized(orch, cfg=cfg, rest_client=rest)
            action = "blocking_v6_materialization"
        elif not orch.is_running():
            orch.start()
            action = "orchestrator_start"
        else:
            loops = list(getattr(orch, "loops", []) or [])
            for loop in loops:
                if not _loop_thread_alive(loop):
                    loop.start()
            action = "restarted_loop_threads"
    except Exception as exc:
        log_engine(
            f"trading_plane_repair: failed ({reason}) "
            f"{type(exc).__name__}: {exc}"
        )
        return {
            "ok": False,
            "action": action,
            "error": f"{type(exc).__name__}: {exc}",
            **describe_trading_plane(),
        }

    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if is_trading_plane_live():
            break
        time.sleep(0.25)

    final = describe_trading_plane()
    ok = bool(final.get("live"))
    log_engine(
        f"trading_plane_repair: {reason} action={action} live={ok} "
        f"blockers={final.get('blockers')}"
    )
    if ok:
        try:
            from system.system_state import get_system_state

            snap = get_system_state().snapshot_model()
            get_system_state().update_state(
                snap.phase,
                snap.percent,
                snap.phase_label,
                loops={
                    "built": final.get("loop_count") or snap.loops.built,
                    "running": True,
                    "accepting_ticks": True,
                },
            )
        except Exception:
            pass
    return {"ok": ok, "action": action, **final}


def reset_trading_plane_readiness_for_tests() -> None:
    global _LAST_REPAIR_MONO
    _LAST_REPAIR_MONO = 0.0
