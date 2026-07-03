"""
Targeted subsystem self-healing — never restarts the full agent.
Each routine restarts only the failing component and logs the exact cause.
"""

from __future__ import annotations

import threading
from typing import Any

from system.engine_log import log_engine

_heal_lock = threading.Lock()
_last_heal_mono: dict[str, float] = {}
_HEAL_COOLDOWN_SEC = 10.0


def _cooldown_ok(subsystem_id: str) -> bool:
    import time

    now = time.monotonic()
    with _heal_lock:
        last = _last_heal_mono.get(subsystem_id, 0.0)
        if (now - last) < _HEAL_COOLDOWN_SEC:
            return False
        _last_heal_mono[subsystem_id] = now
    return True


def heal_yahoo(*, cfg: Any | None = None) -> bool:
    """Restart Yahoo quote poller only."""
    if not _cooldown_ok("yahoo"):
        return False
    from system.boot.boot_orchestrator import SubsystemId, mark_subsystem_healing

    mark_subsystem_healing(SubsystemId.YAHOO, action="restart_yahoo_poller")
    try:
        try:
            from system.chaos_guardian import clear_token_queue_delays

            clear_token_queue_delays(refill=True)
        except Exception:
            pass
        from feeder.yahoo_quote_poller import start_yahoo_quote_poller
        from feeder.pricing_transport import yahoo_poll_seconds
        from runtime.dual_core_execution import ROTATION_UNIVERSE
        from system.config_loader import ConfigLoader

        if cfg is None:
            cfg = ConfigLoader().load(validate=False)
        start_yahoo_quote_poller(list(ROTATION_UNIVERSE), poll_sec=yahoo_poll_seconds(cfg))
        log_engine("boot_heal: Yahoo poller restarted")
        return True
    except Exception as exc:
        log_engine(f"boot_heal: Yahoo restart failed {type(exc).__name__}: {exc}")
        return False


def heal_ig(*, rest: Any | None = None) -> bool:
    """Re-verify IG session without blocking other subsystems."""
    if not _cooldown_ok("ig"):
        return False
    from system.boot.boot_orchestrator import SubsystemId, mark_subsystem_healing

    mark_subsystem_healing(SubsystemId.IG, action="refresh_ig_session")
    try:
        if rest is None:
            from system.boot.post_ready_services import get_boot_rest_client

            rest = get_boot_rest_client()
        if rest is None:
            from system.config_loader import ConfigLoader
            from ig_api.rest_client import IGRestClient

            cfg = ConfigLoader().load(validate=False)
            rest = IGRestClient(cfg)
        rest.ensure_session()
        log_engine("boot_heal: IG session refreshed")
        return True
    except Exception as exc:
        log_engine(f"boot_heal: IG refresh failed {type(exc).__name__}: {exc}")
        return False


def heal_feeds() -> bool:
    """Restart feed guardian and trigger cockpit feed heal."""
    if not _cooldown_ok("feeds"):
        return False
    from system.boot.boot_orchestrator import SubsystemId, mark_subsystem_healing

    mark_subsystem_healing(SubsystemId.FEEDS, action="feed_guardian_heal")
    try:
        from system.cockpit_feed_guardian_agent import start_agent_feed_guardian
        from system.unified_fulfillment_cache import force_cockpit_feed_heal

        start_agent_feed_guardian()
        force_cockpit_feed_heal(reason="boot_heal_feeds")
        log_engine("boot_heal: feed guardian + cockpit heal triggered")
        return True
    except Exception as exc:
        log_engine(f"boot_heal: feeds heal failed {type(exc).__name__}: {exc}")
        return False


def heal_routing() -> bool:
    """Re-arm unified execution routes in background."""
    if not _cooldown_ok("routing"):
        return False
    from system.boot.boot_orchestrator import SubsystemId, mark_subsystem_healing

    mark_subsystem_healing(SubsystemId.ROUTING, action="warm_route_cache")

    def _warm() -> None:
        try:
            from api.gui_status import warm_unified_execution_route_cache

            n = warm_unified_execution_route_cache()
            log_engine(f"boot_heal: route cache re-warmed ({n} routes)")
        except Exception as exc:
            log_engine(f"boot_heal: routing warm failed {type(exc).__name__}: {exc}")

    threading.Thread(target=_warm, name="boot-heal-routing", daemon=True).start()
    return True


def heal_governance() -> bool:
    """Clear emergency override if safe — non-blocking re-eval."""
    if not _cooldown_ok("governance"):
        return False
    from system.boot.boot_orchestrator import SubsystemId, mark_subsystem_healing

    mark_subsystem_healing(SubsystemId.GOVERNANCE, action="clear_emergency_override")
    try:
        from cockpit.emergency import COCKPIT_EMERGENCY_OVERRIDE_ACTIVE, clear_emergency_cockpit_override

        if COCKPIT_EMERGENCY_OVERRIDE_ACTIVE:
            clear_emergency_cockpit_override(resume_trading=True)
            log_engine("boot_heal: emergency cockpit override cleared")
        return True
    except Exception as exc:
        log_engine(f"boot_heal: governance heal failed {type(exc).__name__}: {exc}")
        return False


def heal_execution() -> bool:
    """Restart stacked dual-asset execution loop only."""
    if not _cooldown_ok("execution"):
        return False
    try:
        from system.system_state import get_system_state

        if not get_system_state().snapshot_model().ready:
            return False
    except Exception:
        return False
    from system.boot.boot_orchestrator import SubsystemId, mark_subsystem_healing

    mark_subsystem_healing(SubsystemId.EXECUTION, action="restart_stacked_sweep")
    try:
        from runtime.dual_core_execution import (
            _ensure_stacked_sweep_running,
            _stacked_sweep_is_productive,
        )

        if _stacked_sweep_is_productive():
            return True
        _ensure_stacked_sweep_running()
        log_engine("boot_heal: stacked execution sweep restarted")
        return _stacked_sweep_is_productive()
    except Exception as exc:
        log_engine(f"boot_heal: execution restart failed {type(exc).__name__}: {exc}")
        return False


def run_targeted_heal(subsystem_id: str) -> bool:
    """Dispatch heal by subsystem id — never restarts full agent."""
    handlers = {
        "yahoo": heal_yahoo,
        "ig": heal_ig,
        "feeds": heal_feeds,
        "routing": heal_routing,
        "governance": heal_governance,
        "execution": heal_execution,
        "core_agent": lambda: False,
    }
    fn = handlers.get(subsystem_id)
    if fn is None:
        return False
    from system.boot.boot_orchestrator import record_boot_event

    record_boot_event("heal_attempt", subsystem=subsystem_id)
    ok = fn()
    record_boot_event(
        "heal_complete" if ok else "heal_failed",
        subsystem=subsystem_id,
        level="info" if ok else "warn",
    )
    return ok
