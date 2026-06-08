"""v26 gate relaxations — replay-gated overrides (config_v26 gate_relaxations)."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from system.paths import project_root


@lru_cache(maxsize=1)
def _relaxation_block() -> dict[str, Any]:
    path = project_root() / "config" / "config_v26.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        block = raw.get("gate_relaxations") or {}
        return block if isinstance(block, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def reset_gate_relaxation_cache_for_tests() -> None:
    _relaxation_block.cache_clear()


def relaxation_enabled() -> bool:
    return bool(_relaxation_block().get("enabled"))


def effective_fitness_min(epic: str, *, points_state: str) -> float:
    """Return fitness gate floor for this epic (default 55%)."""
    from trading.environment_scorer import GATE_PASS_MIN

    block = _relaxation_block()
    if not block.get("enabled"):
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
    return {
        "enabled": bool(block.get("enabled")),
        "fitness_min": block.get("fitness_min"),
        "epics": list(block.get("epics") or []),
        "require_points_healthy": bool(block.get("require_points_healthy", True)),
        "note": str(block.get("_note") or ""),
    }
