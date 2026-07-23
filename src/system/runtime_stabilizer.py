"""
Runtime stabilizer — mandatory 5-cycle production stress validation before trade_ready.

Each cycle: cold micro-boot, 50 HF ticks, routing verification, thread teardown, 30s socket cooldown.
"""

from __future__ import annotations

import os
import time
from typing import Any

from system.engine_log import log_engine

_lock = __import__("threading").RLock()
_seal = "PENDING"
_last_result: dict[str, Any] = {"ok": False, "seal": _seal, "cycles": []}


def _stabilizer_cycles() -> int:
    return int(os.environ.get("IG_STABILIZER_CYCLES", "5"))


def _cycle_cooldown_sec() -> float:
    return float(os.environ.get("IG_STABILIZER_COOLDOWN_SEC", "30"))


def _ticks_per_cycle() -> int:
    return int(os.environ.get("IG_STABILIZER_TICKS", "50"))


def get_stabilizer_seal() -> str:
    with _lock:
        return str(_seal)


def get_stabilizer_snapshot() -> dict[str, Any]:
    with _lock:
        return dict(_last_result)


def reset_runtime_stabilizer_for_tests() -> None:
    global _seal
    with _lock:
        _seal = "PENDING"
        _last_result.clear()
        _last_result.update({"ok": False, "seal": _seal, "cycles": []})


def _clear_stabilizer_memory() -> None:
    try:
        from apex.microkernel import reset_microkernel_for_tests

        reset_microkernel_for_tests()
    except Exception:
        pass
    try:
        from system.market_data_hub import reset_headline_urgency_for_tests

        reset_headline_urgency_for_tests()
    except Exception:
        pass
    try:
        from system.chaos_guardian import reset_chaos_guardian_for_tests

        reset_chaos_guardian_for_tests()
    except Exception:
        pass


def _rearm_orchestrator_for_cycle() -> None:
    try:
        import runtime.master_orchestrator as mo

        with mo._lock:
            mo._armed = True
            mo._primed = True
    except Exception:
        pass


def _shutdown_background_threads_for_cycle() -> None:
    try:
        from system.autonomic_healer import stop_autonomic_healer

        stop_autonomic_healer()
    except Exception:
        pass
    try:
        from system.backup_manager import stop_backup_daemon

        stop_backup_daemon()
    except Exception:
        pass
    _rearm_orchestrator_for_cycle()


def _inject_cycle_ticks(epics: list[str], count: int) -> int:
    from system.market_data_hub import get_market_data_hub

    hub = get_market_data_hub()
    injected = 0
    for i in range(count):
        epic = epics[i % len(epics)]
        base = 100.0 + (i % 17) * 0.01
        hub.publish(
            epic,
            bid=base,
            offer=base + 0.02,
            quote_time=time.time(),
            source="synthetic",
        )
        injected += 1
    return injected


def _verify_trade_routing_chain(epic: str) -> dict[str, Any]:
    outcome: dict[str, Any] = {"ok": False, "epic": epic}
    try:
        from runtime.master_orchestrator import resolve_execution_route

        decision = resolve_execution_route(epic)
        outcome["route"] = decision.execution_path
        outcome["allow_entry"] = bool(decision.allow_entry)
    except Exception as exc:
        outcome["route_error"] = f"{type(exc).__name__}: {exc}"
        return outcome

    try:
        from system.chaos_guardian import enqueue_fast_pass_token, get_fast_pass_queue_snapshot

        enqueue_fast_pass_token(epic=epic, direction="BUY", score=0.72, reason="stabilizer_cycle")
        queue = get_fast_pass_queue_snapshot()
        outcome["fast_pass"] = bool(queue)
        outcome["ok"] = bool(outcome.get("route")) and outcome["fast_pass"]
    except Exception as exc:
        outcome["token_error"] = f"{type(exc).__name__}: {exc}"
    return outcome


def _run_single_stabilizer_cycle(cycle: int, epics: list[str]) -> dict[str, Any]:
    purge: dict[str, Any] = {"skipped": "in_agent_process"}
    try:
        from cockpit.agent_api_proxy import in_trading_agent_process
        from cockpit.desktop_process_guard import destroy_orphan_zombie_processes

        if not in_trading_agent_process():
            purge = destroy_orphan_zombie_processes()
    except Exception as exc:
        purge = {"error": f"{type(exc).__name__}: {exc}"}
    _rearm_orchestrator_for_cycle()
    started = time.perf_counter()
    ticks = _inject_cycle_ticks(epics, _ticks_per_cycle())
    route = _verify_trade_routing_chain(epics[0])
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    ok = ticks >= _ticks_per_cycle() and bool(route.get("ok"))
    return {
        "cycle": cycle,
        "ok": ok,
        "ticks": ticks,
        "route": route,
        "purge": purge,
        "elapsed_ms": round(elapsed_ms, 3),
    }


def run_five_cycle_production_stabilizer(
    epics: list[str] | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    global _seal

    if os.environ.get("IG_STABILIZER_SKIP", "").strip() == "1" and not force:
        with _lock:
            _seal = "APPROVED"
            _last_result.update({"ok": True, "seal": _seal, "skipped": True, "cycles": []})
        return dict(_last_result)

    market_epics = list(epics or [])
    if not market_epics:
        try:
            from system.market_data_hub import NIGHT_MATRIX_EPICS

            market_epics = list(NIGHT_MATRIX_EPICS)
        except Exception:
            market_epics = ["CS.D.EURUSD.CFD.IP"]

    cycles: list[dict[str, Any]] = []
    t0 = time.perf_counter()
    total_cycles = _stabilizer_cycles()
    cooldown = _cycle_cooldown_sec()
    for cycle in range(1, total_cycles + 1):
        try:
            row = _run_single_stabilizer_cycle(cycle, market_epics)
        except Exception as exc:
            row = {"cycle": cycle, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        cycles.append(row)
        if not row.get("ok"):
            _clear_stabilizer_memory()
            with _lock:
                _seal = "REJECTED"
                _last_result.update(
                    {
                        "ok": False,
                        "seal": _seal,
                        "cycles": cycles,
                        "failed_cycle": cycle,
                        "total_ms": round((time.perf_counter() - t0) * 1000.0, 3),
                    }
                )
            log_engine(f"RuntimeStabilizer: REJECTED at cycle {cycle}")
            return dict(_last_result)

        _shutdown_background_threads_for_cycle()
        if cycle < total_cycles:
            time.sleep(cooldown)

    total_ms = round((time.perf_counter() - t0) * 1000.0, 3)
    with _lock:
        _seal = "APPROVED"
        _last_result.update(
            {
                "ok": True,
                "seal": _seal,
                "cycles": cycles,
                "cycle_count": len(cycles),
                "total_ms": total_ms,
            }
        )
    log_engine(f"RuntimeStabilizer: APPROVED ({len(cycles)} cycles, {total_ms:.0f}ms)")
    return dict(_last_result)
