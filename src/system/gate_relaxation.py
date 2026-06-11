"""Gate relaxations — v26 replay-gated overrides + v29 demo_soak_mode."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from system.paths import project_root


@lru_cache(maxsize=1)
def _relaxation_block() -> dict[str, Any]:
    try:
        from system.v26_config import get_effective_overlay

        block = get_effective_overlay().get("gate_relaxations") or {}
        return block if isinstance(block, dict) else {}
    except Exception:
        path = project_root() / "config" / "config_v26.json"
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            block = raw.get("gate_relaxations") or {}
            return block if isinstance(block, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}


def _soak_block() -> dict[str, Any]:
    """Live config read — demo_soak_mode in config_v29 (merged Config)."""
    try:
        from system.config_loader import get_config

        raw = get_config().get("demo_soak_mode") or {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def reset_gate_relaxation_cache_for_tests() -> None:
    _relaxation_block.cache_clear()


def demo_soak_enabled() -> bool:
    return bool(_soak_block().get("enabled"))


def _v26_relaxation_active() -> bool:
    """v26 gate_relaxations — suppressed when Profile B learning_demo owns policy."""
    try:
        from system.learning_demo_policy import v26_gate_relaxations_suppressed

        if v26_gate_relaxations_suppressed():
            return False
    except Exception:
        pass
    return bool(_relaxation_block().get("enabled"))


def relaxation_enabled() -> bool:
    return _v26_relaxation_active() or demo_soak_enabled()


def rotation_filter_bypassed() -> bool:
    """True when top-3 rotation should not block entries (demo soak)."""
    soak = _soak_block()
    if demo_soak_enabled() and bool(soak.get("disable_rotation_filter", True)):
        return True
    return False


def soak_ml_veto_bypassed() -> bool:
    """Skip ML veto during demo soak so probe flow reaches execution for labelling."""
    soak = _soak_block()
    if not demo_soak_enabled():
        return False
    return bool(soak.get("bypass_ml_veto", True))


def soak_spread_to_atr_max(default: float) -> float:
    """Raise spread/ATR circuit threshold during demo soak (more probe flow)."""
    if not demo_soak_enabled():
        return default
    soak = _soak_block()
    try:
        override = float(soak.get("spread_to_atr_circuit_max") or 0)
    except (TypeError, ValueError):
        override = 0.0
    if override > 0:
        return override
    return default


def _epic_relaxation_allowed(epic: str, *, relax_all: bool, allowed: list[Any]) -> bool:
    if relax_all:
        return True
    if not allowed:
        return True
    return epic in allowed


def effective_trade_confidence_threshold(
    base_threshold: float,
    *,
    points_state: str,
    instrument_threshold: float | None = None,
    epic: str = "",
) -> float:
    """Cap points WARNING bar during demo soak / gate_relaxations."""
    soak = _soak_block()
    if demo_soak_enabled():
        if points_state != "WARNING":
            return base_threshold
        floor = float(instrument_threshold or 0)
        if soak.get("warning_use_instrument_threshold") and floor > 0:
            return min(base_threshold, floor)
        try:
            cap = float(soak.get("warning_confidence_cap") or 0)
        except (TypeError, ValueError):
            cap = 0.0
        if cap <= 0:
            return base_threshold
        if floor > 0:
            return min(base_threshold, max(cap, floor))
        return min(base_threshold, cap)

    block = _relaxation_block()
    if not _v26_relaxation_active():
        return base_threshold
    if points_state != "WARNING":
        return base_threshold
    allowed = block.get("epics") or []
    if allowed and epic and epic not in allowed:
        return base_threshold
    floor = float(instrument_threshold or 0)
    if block.get("warning_use_instrument_threshold") and floor > 0:
        return min(base_threshold, floor)
    try:
        cap = float(block.get("warning_confidence_cap") or 0)
    except (TypeError, ValueError):
        cap = 0.0
    if cap <= 0:
        return base_threshold
    if floor > 0:
        return min(base_threshold, max(cap, floor))
    return min(base_threshold, cap)


def effective_fitness_min(epic: str, *, points_state: str) -> float:
    """Return environment fitness floor — lower during demo soak for probe flow."""
    from trading.environment_scorer import GATE_PASS_MIN

    soak = _soak_block()
    if demo_soak_enabled():
        if soak.get("require_points_healthy") and points_state != "HEALTHY":
            return GATE_PASS_MIN
        relax_all = bool(soak.get("relax_all_epics", True))
        allowed = list(soak.get("epics") or [])
        if not _epic_relaxation_allowed(epic, relax_all=relax_all, allowed=allowed):
            return GATE_PASS_MIN
        try:
            floor = float(soak.get("fitness_min") or 50)
        except (TypeError, ValueError):
            floor = 50.0
        return max(45.0, min(GATE_PASS_MIN, floor))

    block = _relaxation_block()
    if not _v26_relaxation_active():
        return GATE_PASS_MIN

    if block.get("require_points_healthy") and points_state != "HEALTHY":
        return GATE_PASS_MIN

    allowed = block.get("epics") or []
    if allowed and epic not in allowed:
        return GATE_PASS_MIN

    try:
        floor = float(block.get("fitness_min") or GATE_PASS_MIN)
    except (TypeError, ValueError):
        return GATE_PASS_MIN

    return max(50.0, min(GATE_PASS_MIN, floor))


def relaxation_snapshot() -> dict[str, Any]:
    block = _relaxation_block()
    soak = _soak_block()
    v26_active = _v26_relaxation_active()
    snap: dict[str, Any] = {
        "enabled": relaxation_enabled(),
        "v26_gate_relaxations_active": v26_active,
        "demo_soak_mode": bool(soak.get("enabled")),
        "fitness_min": soak.get("fitness_min") if demo_soak_enabled() else block.get("fitness_min"),
        "warning_confidence_cap": (
            soak.get("warning_confidence_cap")
            if demo_soak_enabled()
            else block.get("warning_confidence_cap")
        ),
        "relax_all_epics": bool(soak.get("relax_all_epics", True)) if demo_soak_enabled() else False,
        "disable_rotation_filter": rotation_filter_bypassed(),
        "bypass_ml_veto": soak_ml_veto_bypassed(),
        "spread_to_atr_circuit_max": soak.get("spread_to_atr_circuit_max"),
        "epics": list(block.get("epics") or []),
        "require_points_healthy": bool(
            soak.get("require_points_healthy", False)
            if demo_soak_enabled()
            else block.get("require_points_healthy", True)
        ),
        "note": str(
            soak.get("_note") or block.get("_note") or ""
        ),
    }
    try:
        from system.learning_demo_policy import (
            learning_demo_enabled,
            learning_demo_policy_id,
            learning_demo_profile,
        )

        snap["learning_demo_profile"] = (
            learning_demo_profile() if learning_demo_enabled() else ""
        )
        snap["policy_id"] = (
            learning_demo_policy_id() if learning_demo_enabled() else ""
        )
    except Exception:
        pass
    return snap
