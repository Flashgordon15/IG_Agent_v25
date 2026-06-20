"""Profile B → protective mode knobs (config_v29.protective_learning)."""

from __future__ import annotations

from typing import Any

# Temporary test override — set False to restore production protective floor (62%).
USE_TEMPORARY_TEST_GATE = False
TEMPORARY_TEST_CONFIDENCE_FLOOR = 50.0
_PRODUCTION_SIGNAL_THRESHOLD_FLOOR = 62.0
_PRODUCTION_RSI_BUY_MAX = 85.0
TEMPORARY_TEST_RSI_BUY_MAX = 98.0
TEMPORARY_TEST_PROBE_RISK_CAP_GBP = 200.0

# RSI Exhaustion Reversion — production extreme-market short trigger (test gate off).
EXHAUSTION_RSI_ARM_THRESHOLD = 90.0
EXHAUSTION_RSI_TRIGGER_THRESHOLD = 88.0
EXHAUSTION_EDGE_SCORE_BOOST = 15.0

_test_mode_logged = False
_test_rsi_relaxation_logged = False
_test_execution_bypass_logged = False

_block_mtime: float = -1.0
_block_cache: dict[str, Any] = {}


def _config_mtime() -> float:
    try:
        from system.config_loader import _primary_config_path

        path = _primary_config_path()
        return float(path.stat().st_mtime) if path.is_file() else 0.0
    except Exception:
        return 0.0


def _block() -> dict[str, Any]:
    """Protective-learning overlay — reloads when primary config mtime changes."""
    global _block_mtime, _block_cache
    mtime = _config_mtime()
    if mtime != _block_mtime:
        try:
            from system.config_loader import get_config

            raw = get_config().get("protective_learning") or {}
            _block_cache = raw if isinstance(raw, dict) else {}
        except Exception:
            _block_cache = {}
        _block_mtime = mtime
    return _block_cache


def reset_protective_learning_cache_for_tests() -> None:
    global _block_mtime, _block_cache
    _block_mtime = -1.0
    _block_cache = {}


def protective_learning_enabled() -> bool:
    return bool(_block().get("enabled"))


def signal_threshold_floor() -> float | None:
    if not protective_learning_enabled():
        return None
    if USE_TEMPORARY_TEST_GATE:
        confidence_floor = TEMPORARY_TEST_CONFIDENCE_FLOOR
    else:
        try:
            confidence_floor = float(
                _block().get("signal_threshold_floor") or _PRODUCTION_SIGNAL_THRESHOLD_FLOOR
            )
        except (TypeError, ValueError):
            confidence_floor = _PRODUCTION_SIGNAL_THRESHOLD_FLOOR
    return confidence_floor if confidence_floor > 0 else None


def apply_temporary_test_confidence_floor(threshold: float) -> float:
    """Cap the live entry bar at 50% while temporary test mode is active."""
    if USE_TEMPORARY_TEST_GATE:
        return min(float(threshold), TEMPORARY_TEST_CONFIDENCE_FLOOR)
    return float(threshold)


def apply_temporary_test_rsi_buy_max(rsi_buy_max: float) -> float:
    """Raise RSI overbought ceiling to 98 while temporary test gate is active."""
    if USE_TEMPORARY_TEST_GATE:
        return TEMPORARY_TEST_RSI_BUY_MAX
    return float(rsi_buy_max)


def log_temporary_test_rsi_relaxation_once() -> None:
    """Log once per process when demo test mode relaxes the RSI overbought guard."""
    global _test_rsi_relaxation_logged
    if not USE_TEMPORARY_TEST_GATE or _test_rsi_relaxation_logged:
        return
    _test_rsi_relaxation_logged = True
    try:
        from system.engine_log import log_engine

        log_engine(
            "🧪 TEST MODE: Relaxing RSI overbought ceiling to 98 to force test trade verification."
        )
    except Exception:
        pass


def ensure_test_mode_rsi_relaxation_armed() -> None:
    """Arm RSI relaxation for demo verification runs (no-op when test gate off)."""
    if not USE_TEMPORARY_TEST_GATE:
        return
    log_temporary_test_rsi_relaxation_once()


def log_temporary_test_execution_bypass_once() -> None:
    """Log once per process when demo test mode lifts execution barriers."""
    global _test_execution_bypass_logged
    if not USE_TEMPORARY_TEST_GATE or _test_execution_bypass_logged:
        return
    _test_execution_bypass_logged = True
    try:
        from system.engine_log import log_engine

        log_engine(
            "🧪 TEST MODE: Bypassing consecutive loss circuit breaker and "
            "expanding risk caps for test fill validation."
        )
    except Exception:
        pass


def apply_temporary_test_risk_cap_gbp(cap_gbp: float) -> float:
    """Raise probe/full risk ceiling to £200 while temporary test gate is active."""
    if USE_TEMPORARY_TEST_GATE:
        return max(float(cap_gbp), TEMPORARY_TEST_PROBE_RISK_CAP_GBP)
    return float(cap_gbp)


def clear_circuit_breaker_for_test_run(store: Any | None = None) -> None:
    """Force consecutive-loss count to zero and clear the 60-minute pause."""
    try:
        from system.agent_execution_mode import (
            demo_operational_floors_active,
            demo_sandbox_unblock_active,
        )

        allowed = (
            USE_TEMPORARY_TEST_GATE
            or demo_operational_floors_active()
            or demo_sandbox_unblock_active()
        )
    except Exception:
        allowed = USE_TEMPORARY_TEST_GATE
    if not allowed:
        return
    if store is None:
        try:
            from data.learning_store import LearningStore

            store = LearningStore()
        except Exception:
            store = None
    if store is None:
        return
    try:
        store.clear_circuit_breaker_state()
    except Exception:
        pass


def ensure_test_mode_execution_bypass_armed(store: Any | None = None) -> None:
    """Arm circuit-breaker bypass and expanded risk caps for demo test runs."""
    if not USE_TEMPORARY_TEST_GATE:
        return
    log_temporary_test_execution_bypass_once()
    clear_circuit_breaker_for_test_run(store)


def temporary_test_gate_active() -> bool:
    return USE_TEMPORARY_TEST_GATE


def exhaustion_reversion_enabled() -> bool:
    """Arm exhaustion monitor only in production (not temporary test gate)."""
    return not USE_TEMPORARY_TEST_GATE


def exhaustion_rsi_arm_threshold() -> float:
    return EXHAUSTION_RSI_ARM_THRESHOLD


def exhaustion_rsi_trigger_threshold() -> float:
    return EXHAUSTION_RSI_TRIGGER_THRESHOLD


def exhaustion_edge_score_boost() -> float:
    """+15 pts applied to sell score so structural reversals clear the 62% floor."""
    return EXHAUSTION_EDGE_SCORE_BOOST


def production_rsi_buy_max() -> float:
    return _PRODUCTION_RSI_BUY_MAX


def log_exhaustion_reversion_trigger() -> None:
    """Emit when the 1m exhaustion reversion SELL path fires."""
    try:
        from system.engine_log import log_engine

        log_engine(
            "🎯 EXHAUSTION TRIGGER: Market extreme captured. "
            "Reversion SELL signal dispatched to router."
        )
    except Exception:
        pass


def cockpit_controls_unlocked_for_test() -> bool:
    """When True, Flight Deck operator controls bypass manual_stop / init locks."""
    return USE_TEMPORARY_TEST_GATE


_autonomous_engine_boot_armed = False


def ensure_autonomous_engine_on_boot() -> None:
    """One-shot boot: clear manual_stop and resolve execution mode (never force SHADOW)."""
    global _autonomous_engine_boot_armed
    if _autonomous_engine_boot_armed:
        return
    _autonomous_engine_boot_armed = True
    try:
        from system.shutdown_cleanup import clear_manual_stop

        clear_manual_stop()
    except Exception:
        pass
    if USE_TEMPORARY_TEST_GATE:
        return
    try:
        from system.agent_execution_mode import (
            ensure_execution_plane_armed_on_boot,
        )

        ensure_execution_plane_armed_on_boot()
    except Exception:
        pass


def build_production_autonomous_boot_controls() -> dict[str, Any]:
    """First Flight Deck frame — autonomous engine unlocked and ON (purple slider)."""
    ensure_autonomous_engine_on_boot()
    return {
        "manual_stop": False,
        "disabled": False,
        "controls_locked": False,
        "shadow_toggle_enabled": True,
        "init_complete": True,
        "test_mode_unlock": False,
        "autonomous_engine_on": True,
    }


def clear_operational_locks_for_test_run() -> None:
    """Reset supervisor holds so cockpit toggles are clickable during test runs."""
    if not USE_TEMPORARY_TEST_GATE:
        return
    try:
        from system.shutdown_cleanup import clear_manual_stop

        clear_manual_stop()
    except Exception:
        pass


_test_mode_runtime_activated = False


def activate_test_mode_runtime() -> None:
    """One-shot test-run activation — unlock cockpit and arm scalping telemetry."""
    global _test_mode_runtime_activated
    if not USE_TEMPORARY_TEST_GATE:
        return
    clear_operational_locks_for_test_run()
    ensure_test_mode_rsi_relaxation_armed()
    ensure_test_mode_execution_bypass_armed()
    if _test_mode_runtime_activated:
        return
    _test_mode_runtime_activated = True
    try:
        from system.engine_log import log_engine

        log_engine(
            "🧪 TEST MODE: cockpit unlocked, scalping engine ACTIVE, "
            "tick-velocity override OFF."
        )
    except Exception:
        pass


def apply_test_mode_scalping_telemetry(
    scalping: dict[str, Any] | None,
) -> dict[str, Any]:
    """Force ACTIVE scalping state and live tick counters (override off) for test runs."""
    st = dict(scalping or {})
    if not USE_TEMPORARY_TEST_GATE:
        return st
    if str(st.get("engine_state") or "STANDBY").upper() in ("", "STANDBY"):
        st["engine_state"] = "ACTIVE"
    tv = dict(st.get("tick_velocity") or {})
    tv["override_active"] = False
    st["tick_velocity"] = tv
    st["test_mode_active"] = True
    return st


def build_test_mode_cockpit_controls() -> dict[str, Any]:
    """Authoritative unlocked control payload for Flight Deck operator toggles."""
    return {
        "manual_stop": False,
        "disabled": False,
        "controls_locked": False,
        "shadow_toggle_enabled": True,
        "init_complete": True,
        "test_mode_unlock": True,
    }


def log_temporary_test_gate_once() -> None:
    """Emit a single engine.log line per process when test floor is active."""
    global _test_mode_logged
    if not USE_TEMPORARY_TEST_GATE or _test_mode_logged:
        return
    _test_mode_logged = True
    try:
        from system.engine_log import log_engine

        log_engine(
            "🧪 TEST MODE ACTIVE: Evaluating signal against temporary 50.0% floor."
        )
    except Exception:
        pass


def fitness_min_floor() -> float | None:
    if not protective_learning_enabled():
        return None
    try:
        v = float(_block().get("fitness_min_floor") or 0)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def snapshot() -> dict[str, Any]:
    block = _block()
    return {
        "enabled": protective_learning_enabled(),
        "signal_threshold_floor": signal_threshold_floor(),
        "fitness_min_floor": fitness_min_floor(),
        "note": str(block.get("_note") or ""),
    }
