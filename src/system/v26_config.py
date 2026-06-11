"""Read v26 overlay config (ml_veto, milestones) without merging into v25 Config."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from system.paths import project_root


@lru_cache(maxsize=1)
def load_v26_overlay() -> dict[str, Any]:
    path = project_root() / "config" / "config_v26.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def reset_v26_overlay_cache_for_tests() -> None:
    load_v26_overlay.cache_clear()
    get_effective_overlay.cache_clear()


@lru_cache(maxsize=1)
def get_effective_overlay() -> dict[str, Any]:
    """Single v26 overlay read — gate_relaxations + capital envelope + ml blocks."""
    raw = load_v26_overlay()
    gate_relax = raw.get("gate_relaxations") or {}
    try:
        from system.learning_demo_policy import v26_gate_relaxations_suppressed

        v26_active = bool(gate_relax.get("enabled")) and not v26_gate_relaxations_suppressed()
    except Exception:
        v26_active = bool(gate_relax.get("enabled"))
    try:
        from system.config_loader import get_config

        soak = get_config().get("demo_soak_mode") or {}
    except Exception:
        soak = {}
    return {
        **raw,
        "gate_relaxations": gate_relax,
        "v26_gate_relaxations_active": v26_active,
        "demo_soak_mode": soak if isinstance(soak, dict) else {},
    }


def ml_veto_settings() -> dict[str, Any]:
    block = load_v26_overlay().get("ml_veto") or {}
    return {
        "enabled": bool(block.get("enabled", False)),
        "mode": str(block.get("mode") or "veto"),
        "min_probability": float(block.get("min_probability") or 0.58),
        "min_probability_high_conf": float(
            block.get("min_probability_high_conf") or 0.55
        ),
        "min_labelled_rows": int(block.get("min_labelled_rows") or 500),
        "per_epic": dict(block.get("per_epic") or {}),
        "use_s4_models": bool(block.get("use_s4_models", True)),
    }


def s4_settings() -> dict[str, Any]:
    block = load_v26_overlay().get("s4_ml_meta") or {}
    return {
        "enabled": bool(block.get("enabled", False)),
        "min_decided_rows": int(block.get("min_decided_rows") or 30),
        "min_val_wr": float(block.get("min_val_wr") or 0.52),
    }


def epic_ml_veto_enabled(epic: str) -> bool:
    cfg = ml_veto_settings()
    if not cfg.get("enabled"):
        return False
    per = cfg.get("per_epic") or {}
    if per:
        # Whitelist: when per_epic is set, only listed epics use ml_veto.
        if epic not in per:
            return False
        return bool(per[epic].get("enabled", True))
    return True


def epic_min_probability(epic: str) -> float:
    cfg = ml_veto_settings()
    per = cfg.get("per_epic") or {}
    if epic in per and per[epic].get("min_probability") is not None:
        return float(per[epic]["min_probability"])
    return float(cfg.get("min_probability") or 0.58)


def calendar_settings() -> dict[str, Any]:
    block = load_v26_overlay().get("regime") or {}
    return {
        "enabled": bool(block.get("calendar_enabled", False)),
        "calendar_file": str(block.get("calendar_file") or "config/calendar.json"),
    }


def calendar_block_minutes() -> tuple[int, int]:
    """High-impact macro veto window (minutes before/after event)."""
    block = load_v26_overlay().get("regime") or {}
    try:
        before = int(block.get("calendar_block_minutes_before") or 0)
    except (TypeError, ValueError):
        before = 0
    try:
        after = int(block.get("calendar_block_minutes_after") or 0)
    except (TypeError, ValueError):
        after = 0
    return before, after


def pilot_settings() -> dict[str, Any]:
    block = load_v26_overlay().get("pilot") or {}
    return {
        "primary_epic": str(block.get("primary_epic") or ""),
        "target_wr": float(block.get("target_wr") or 0.60),
        "target_rrr": float(block.get("target_rrr") or 2.5),
    }


def s1_settings() -> dict[str, Any]:
    block = load_v26_overlay().get("s1_rules") or {}
    return {
        "independent_threshold": float(block.get("independent_threshold") or 72.0),
        "independent_enabled": bool(block.get("independent_enabled", True)),
    }


def reset_v26_config_cache_for_tests() -> None:
    load_v26_overlay.cache_clear()
