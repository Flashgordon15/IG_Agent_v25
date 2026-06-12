"""Profile B → protective mode knobs (config_v29.protective_learning)."""

from __future__ import annotations

from functools import lru_cache
from typing import Any


@lru_cache(maxsize=1)
def _block() -> dict[str, Any]:
    try:
        from system.config_loader import get_config

        raw = get_config().get("protective_learning") or {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def reset_protective_learning_cache_for_tests() -> None:
    _block.cache_clear()


def protective_learning_enabled() -> bool:
    return bool(_block().get("enabled"))


def signal_threshold_floor() -> float | None:
    if not protective_learning_enabled():
        return None
    try:
        v = float(_block().get("signal_threshold_floor") or 0)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


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
