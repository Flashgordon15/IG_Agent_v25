"""
Iron Cage readiness contract — single source of truth for trade_ready and global ok.

All checks are O(1) from in-memory caches (no external HTTP on hot path).
"""

from __future__ import annotations

import time
from typing import Any

_CACHE: dict[str, Any] | None = None
_CACHE_AT: float = 0.0
_CACHE_TTL_SEC = 8.0
_COCKPIT_CACHE_TTL_SEC = 15.0

_GATE_LABELS = {
    "G1": "Core init",
    "G2": "Auth + metadata",
    "G3": "Data feeds",
    "G4": "Routing warm-up",
    "G5": "Execution ready",
}


def _gate_color(status: str) -> str:
    s = str(status or "").lower()
    if s in ("complete", "ok", "passed"):
        return "green"
    if s in ("running", "degraded", "partial", "warming", "amber"):
        return "amber"
    if s in ("failed", "error", "offline", "blocked"):
        return "red"
    return "grey"


def _evaluate_gates() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        from system.system_state import get_system_state

        snap = get_system_state().snapshot_model()
        for gid in ("G1", "G2", "G3", "G4", "G5"):
            g = snap.gates.get(gid)
            status = str(g.status.value if g else "pending")
            rows.append(
                {
                    "id": gid,
                    "label": _GATE_LABELS.get(gid, gid),
                    "status": status,
                    "detail": str(g.detail or "") if g else "",
                    "color": _gate_color(status),
                    "completed_at": (snap.gate_completed_at or {}).get(gid),
                }
            )
        return rows
    except Exception:
        pass
    for gid in ("G1", "G2", "G3", "G4", "G5"):
        rows.append(
            {
                "id": gid,
                "label": _GATE_LABELS.get(gid, gid),
                "status": "pending",
                "detail": "",
                "color": "grey",
                "completed_at": None,
            }
        )
    return rows


def peek_iron_cage_readiness() -> dict[str, Any] | None:
    """Return cached iron-cage snapshot without recomputing (O(1), never blocks)."""
    if _CACHE is not None:
        return dict(_CACHE)
    return None


def fast_iron_cage_status_snapshot() -> dict[str, Any]:
    """
    Non-blocking iron-cage snapshot for HTTP/cockpit hot paths.

    When health_light shows an operational plane, return that truth immediately
  instead of running full evaluate (which can block on master_orchestrator).
    """
    try:
        from api.health_light import get_health_light_response, iron_cage_from_health_light_snapshot

        hl = get_health_light_response()
        ic = iron_cage_from_health_light_snapshot(hl)
        if ic.get("trade_ready"):
            cached = peek_iron_cage_readiness() or {}
            hub = (hl.get("data_feeds") or {}).get("hub") or {}
            body: dict[str, Any] = {
                **cached,
                "ok": True,
                "trade_ready": True,
                "blockers": list(ic.get("blockers") or []),
                "execution": {
                    "loop_active": bool(hl.get("execution_loop_active")),
                    "stacked_sweep_alive": bool(hl.get("stacked_sweep_alive")),
                    "rotation_sweep_count": int(hl.get("rotation_sweep_count") or 0),
                    "routes_armed": int((hl.get("routing_state") or {}).get("armed") or 0),
                },
                "feeds": {
                    **(
                        cached.get("feeds")
                        if isinstance(cached.get("feeds"), dict)
                        else {}
                    ),
                    "health": "ok",
                    "fresh_count": int(hub.get("fresh_count") or 0),
                    "total_epics": int(hub.get("total") or 0),
                },
                "ts": time.time(),
                "source": "health_light_fast",
            }
            publish_operational_iron_cage_cache(body)
            return body
    except Exception:
        pass
    cached = peek_iron_cage_readiness()
    if cached:
        return dict(cached)
    try:
        from api.health_light import get_health_light_response, iron_cage_from_health_light_snapshot

        hl = get_health_light_response()
        if hl:
            ic = iron_cage_from_health_light_snapshot(hl)
            if ic.get("trade_ready"):
                body = {
                    **ic,
                    "ts": time.time(),
                    "source": "health_light_peek_fallback",
                }
                publish_operational_iron_cage_cache(body)
                return body
    except Exception:
        pass
    return {
        "ok": False,
        "trade_ready": False,
        "blockers": ["iron_cage_warming"],
        "warnings": [],
        "ts": time.time(),
        "source": "peek_empty",
    }


def publish_operational_iron_cage_cache(body: dict[str, Any]) -> None:
    """Keep peek cache aligned with health_light operational telemetry."""
    global _CACHE, _CACHE_AT
    if not body.get("trade_ready"):
        return
    _CACHE = dict(body)
    _CACHE_AT = time.monotonic()


def _read_execution_light_snapshot() -> tuple[bool, bool, int, int]:
    """Execution/routing telemetry without circular health_light import."""
    exec_active = False
    stacked = False
    sweep = 0
    armed = 0
    try:
        from runtime.dual_core_execution import (
            _ticks_per_minute,
            get_active_stack_epics,
            get_rotation_state,
            is_stacked_sweep_thread_alive,
        )
        from runtime.unified_execution import cached_unified_routes

        stack = get_active_stack_epics()
        rot = get_rotation_state()
        sweep = int(rot.get("rotation_sweep_count") or 0)
        stacked = is_stacked_sweep_thread_alive()
        min_tpm = min((_ticks_per_minute(e) for e in stack), default=0) if stack else 0
        exec_active = stacked and (min_tpm >= 5 or sweep > 0)
        routes = cached_unified_routes() or []
        armed = sum(
            1
            for r in routes
            if isinstance(r, dict)
            and str(r.get("execution_path") or "NONE").upper() != "NONE"
        )
    except Exception:
        pass
    return exec_active, stacked, sweep, armed


def evaluate_iron_cage_readiness(*, force_refresh: bool = False, cockpit: bool = False) -> dict[str, Any]:
    """Return full iron-cage snapshot: ok, trade_ready, blockers, subsystems."""
    global _CACHE, _CACHE_AT
    now = time.monotonic()
    ttl = _COCKPIT_CACHE_TTL_SEC if cockpit else _CACHE_TTL_SEC
    if not force_refresh and _CACHE is not None and (now - _CACHE_AT) < ttl:
        return dict(_CACHE)

    blockers: list[str] = []
    warnings: list[str] = []

    gates = _evaluate_gates()
    gates_complete = all(
        str(g.get("status") or "").lower() == "complete" for g in gates
    )
    if not gates_complete:
        try:
            from system.autonomic_healer import get_transport_recovery_state

            if get_transport_recovery_state().get("failover_engaged"):
                gates_complete = True
        except Exception:
            pass
        if not gates_complete:
            blockers.append("gates_incomplete")

    try:
        from system.system_state import get_system_state

        boot_ready = bool(get_system_state().snapshot_model().ready)
    except Exception:
        boot_ready = False
    if not boot_ready:
        blockers.append("boot_not_ready")

    feed_health = "offline"
    feed_fresh = 0
    feed_total = 0
    primary_feed = ""
    fallback_active = False
    retry_backoff_sec = 0.0
    ig_on_signal = False
    try:
        from system.feeds.data_feed_orchestrator import (
            get_data_feed_state,
            ig_used_for_signal_path,
        )

        feed = get_data_feed_state()
        feed_health = str(feed.get("health") or "offline")
        feed_fresh = int(feed.get("fresh_count") or 0)
        feed_total = int(feed.get("total_epics") or 0)
        primary_feed = str(feed.get("primary_feed") or "")
        fallback_active = bool(feed.get("fallback_active"))
        retry_backoff_sec = float(feed.get("retry_backoff_sec") or 0)
        ig_on_signal = ig_used_for_signal_path()
    except Exception:
        blockers.append("feed_state_unavailable")

    autonomic_recovery = False
    try:
        from system.autonomic_healer import get_transport_recovery_state

        recovery = get_transport_recovery_state()
        autonomic_recovery = bool(
            recovery.get("failover_engaged")
            and (
                recovery.get("synthetic_hydration_active")
                or recovery.get("fallback_transport_tier")
            )
        )
    except Exception:
        autonomic_recovery = False

    if feed_health == "offline" or feed_fresh < 1:
        if not autonomic_recovery:
            blockers.append("feed_starvation")
    elif feed_health != "ok":
        warnings.append(f"feed_{feed_health}")
    if ig_on_signal:
        blockers.append("ig_on_signal_path")
    if not primary_feed and feed_fresh < 1 and not autonomic_recovery:
        blockers.append("no_primary_feed")

    exec_active = False
    stacked = False
    sweep = 0
    armed = 0
    try:
        exec_active, stacked, sweep, armed = _read_execution_light_snapshot()
    except Exception:
        blockers.append("health_light_unavailable")

    if not exec_active:
        blockers.append("execution_inactive")
    if armed <= 0:
        blockers.append("routing_unarmed")
    if not stacked:
        warnings.append("stacked_sweep_idle")

    post_ready_operational = (
        exec_active and armed > 0 and feed_fresh >= 1 and stacked
    )
    if post_ready_operational:
        for stale in ("gates_incomplete", "boot_not_ready", "master_orchestrator_not_primed"):
            while stale in blockers:
                blockers.remove(stale)

    ig_limited = False
    ig_cooldown_sec = 0
    try:
        from system.ig_budget_monitor import ig_budget_snapshot

        ig = ig_budget_snapshot()
        ig_limited = bool(ig.get("rate_limited"))
        ig_cooldown_sec = int(ig.get("cooldown_seconds_remaining") or 0)
    except Exception:
        warnings.append("ig_budget_unknown")
    if ig_limited:
        blockers.append("ig_rate_limited")

    regime_ok = False
    try:
        from runtime.regime_switch_engine import get_regime_switch_snapshot

        reg = get_regime_switch_snapshot()
        regime_ok = bool(reg.get("healthy"))
        if not regime_ok:
            warnings.append("regime_engine_degraded")
    except Exception:
        warnings.append("regime_engine_unavailable")

    risk_ok = True
    try:
        from system.volatility_risk_engine import circuit_breaker_blocks_entry

        blocked, cb_reason = circuit_breaker_blocks_entry()
        if blocked:
            risk_ok = False
            blockers.append(cb_reason or "circuit_breaker_active")
    except Exception:
        warnings.append("risk_engine_unavailable")

    reconcile_ok = True
    try:
        from system.broker_reconciliation_daemon import get_reconciliation_snapshot

        rec = get_reconciliation_snapshot()
        reconcile_ok = bool(rec.get("healthy", True))
        if not reconcile_ok:
            blockers.append("broker_reconciliation_drift")
    except Exception:
        warnings.append("reconciliation_unavailable")

    tuner_ok = True
    try:
        from runtime.parameter_tuner import get_tuner_state_snapshot

        tuner = get_tuner_state_snapshot()
        tuner_ok = bool(tuner.get("healthy", True))
        if not tuner_ok:
            warnings.append("parameter_tuner_degraded")
    except Exception:
        warnings.append("parameter_tuner_unavailable")

    explore_ok = True
    try:
        from runtime.portfolio_exploration_engine import get_exploration_state_snapshot

        explore = get_exploration_state_snapshot()
        explore_ok = bool(explore.get("healthy", True)) or int(explore.get("universe_size") or 0) == 0
        if not explore_ok:
            warnings.append("portfolio_exploration_degraded")
    except Exception:
        warnings.append("portfolio_exploration_unavailable")

    guardian_ok = True
    try:
        from system.chaos_guardian import get_guardian_status_snapshot

        guardian = get_guardian_status_snapshot()
        guardian_ok = bool(guardian.get("healthy", True))
        if not guardian_ok:
            warnings.append("chaos_guardian_degraded")
    except Exception:
        warnings.append("chaos_guardian_unavailable")

    orchestrator_ok = True
    orch_pp: int | None = None
    if not cockpit:
        try:
            from runtime.master_orchestrator import (
                all_warmup_phases_acceptable,
                get_orchestrator_state_snapshot,
                is_orchestrator_armed,
                is_orchestrator_primed,
            )

            if is_orchestrator_armed():
                orch_snap = get_orchestrator_state_snapshot()
                orchestrator_ok = is_orchestrator_primed() and all_warmup_phases_acceptable()
                if not orchestrator_ok:
                    blockers.append("master_orchestrator_not_primed")
                phase_status = orch_snap.get("stage_status") or orch_snap.get("phase_status") or {}
                for phase, status in phase_status.items():
                    st = str(status).upper()
                    if st == "FAILED":
                        blockers.append(f"warmup_{phase}_{st.lower()}")
                        orchestrator_ok = False
                    elif st in ("DEGRADED", "WARMING"):
                        warnings.append(f"warmup_{phase}_degraded")
                stage_tokens = orch_snap.get("stage_tokens") or {}
                for stage, token in stage_tokens.items():
                    if str(token).upper() == "FAILED":
                        blockers.append(f"boot_{stage}_failed")
                        orchestrator_ok = False
                    elif str(token).upper() == "WARMING":
                        warnings.append(f"boot_{stage}_warming")
                if orch_snap.get("degraded_override") and orchestrator_ok:
                    warnings.append("orchestrator_warming_progress")
            else:
                orch = get_orchestrator_state_snapshot()
                orchestrator_ok = bool(orch.get("healthy", True)) or not orch.get("armed")
            if orchestrator_ok:
                orch_pp = (get_orchestrator_state_snapshot().get("scoreboard") or {}).get("total_pp")
        except Exception:
            warnings.append("master_orchestrator_unavailable")

    reporting_ok = True
    reporting_state = "ACTIVE"
    try:
        from system.alert_reporting_matrix import get_reporting_status_snapshot, reporting_healthy

        reporting_ok = reporting_healthy()
        reporting_state = str(get_reporting_status_snapshot().get("subsystem_status", "ACTIVE"))
        if not reporting_ok:
            warnings.append("alert_reporting_degraded")
    except Exception:
        warnings.append("alert_reporting_unavailable")

    watchdog = False
    try:
        from api.agent_health import _watchdog_active

        watchdog = _watchdog_active()
    except Exception:
        pass
    if not watchdog:
        warnings.append("watchdog_inactive")

    try:
        from system.boot.boot_orchestrator import get_boot_status_snapshot

        boot_snap = get_boot_status_snapshot()
        boot_trade_ready = bool(boot_snap.get("trade_ready"))
        boot_blockers = list(boot_snap.get("blockers") or [])
    except Exception:
        boot_trade_ready = False
        boot_blockers = []

    critical_blockers = [b for b in blockers if b not in ("stacked_sweep_idle",)]
    trade_ready = (
        len(critical_blockers) == 0
        and feed_health in ("ok", "degraded")
        and (feed_fresh >= 1 or autonomic_recovery)
        and not ig_on_signal
        and exec_active
        and armed > 0
        and not ig_limited
        and risk_ok
        and reconcile_ok
        and orchestrator_ok
        and (boot_ready or boot_trade_ready)
    )
    ok = trade_ready and feed_health == "ok" and gates_complete

    subsystems = {
        "agent": {"ok": boot_ready, "label": "Agent boot"},
        "feeds": {
            "ok": feed_health == "ok" and feed_fresh >= 1,
            "label": f"Feeds {feed_fresh}/{feed_total}",
            "health": feed_health,
        },
        "execution": {"ok": exec_active and stacked, "label": "Execution loop"},
        "routing": {"ok": armed > 0, "label": f"Routes armed {armed}"},
        "ig": {"ok": not ig_limited, "label": "IG budget"},
        "signal_path": {"ok": not ig_on_signal, "label": "Yahoo-first path"},
        "watchdog": {"ok": watchdog, "label": "Supervision"},
        "regime_engine": {"ok": regime_ok, "label": "Regime switch"},
        "risk_engine": {"ok": risk_ok, "label": "Vol risk / CB"},
        "reconciliation": {"ok": reconcile_ok, "label": "Broker reconcile"},
        "parameter_tuner": {
            "ok": tuner_ok,
            "label": "Param tuner",
            "healthy": tuner_ok,
        },
        "portfolio_exploration": {
            "ok": explore_ok,
            "label": "Portfolio explore",
            "healthy": explore_ok,
        },
        "chaos_guardian": {
            "ok": guardian_ok,
            "label": "Chaos guardian",
            "healthy": guardian_ok,
            "critical": not guardian_ok,
        },
        "master_orchestrator": {
            "ok": orchestrator_ok,
            "label": "Master orch",
            "healthy": orchestrator_ok,
            "pp": orch_pp,
        },
        "alert_reporting": {
            "ok": reporting_ok,
            "label": "Alerts",
            "healthy": reporting_ok,
            "state": reporting_state if reporting_ok else "DEGRADED",
        },
    }

    body: dict[str, Any] = {
        "ok": ok,
        "trade_ready": trade_ready,
        "blockers": critical_blockers,
        "warnings": warnings + boot_blockers,
        "gates": gates,
        "feeds": {
            "health": feed_health,
            "fresh_count": feed_fresh,
            "total_epics": feed_total,
            "primary_feed": primary_feed,
            "fallback_active": fallback_active,
            "retry_backoff_sec": retry_backoff_sec,
            "ig_on_signal_path": ig_on_signal,
        },
        "execution": {
            "loop_active": exec_active,
            "stacked_sweep_alive": stacked,
            "rotation_sweep_count": sweep,
            "routes_armed": armed,
        },
        "ig_budget": {
            "rate_limited": ig_limited,
            "cooldown_seconds_remaining": ig_cooldown_sec,
        },
        "subsystems": subsystems,
        "ready_to_trade": trade_ready,
        "ts": time.time(),
    }
    _CACHE = body
    _CACHE_AT = now
    return dict(body)


def reset_iron_cage_cache_for_tests() -> None:
    global _CACHE, _CACHE_AT
    _CACHE = None
    _CACHE_AT = 0.0
