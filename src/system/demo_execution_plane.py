"""Demo throughput execution plane — relax strategy/hard/unified guards for Core B soak."""

from __future__ import annotations

from typing import Any


def _throughput_block(cfg: Any | None = None) -> dict[str, Any]:
    if cfg is not None:
        try:
            raw = cfg.get("demo_throughput_mode") or {}
            return raw if isinstance(raw, dict) else {}
        except (AttributeError, TypeError):
            pass
    try:
        from system.config_loader import get_config

        raw = get_config().get("demo_throughput_mode") or {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def demo_throughput_active(cfg: Any | None = None) -> bool:
    return bool(_throughput_block(cfg).get("enabled"))


def execution_guards_relaxed(*, epic: str = "", cfg: Any | None = None) -> bool:
    """
    When demo throughput is armed, Core B micro / Path B handoff bypass
    strategy controller, hard enforcement, soft enforcement, and unified route blocks.
    """
    if not demo_throughput_active(cfg):
        return False
    block = _throughput_block(cfg)
    if not bool(block.get("bypass_execution_guards", True)):
        return False
    allowed = block.get("epics") or []
    if allowed and epic and epic not in allowed:
        return False
    return True


def demo_pierce_z_threshold(cfg: Any | None = None, default: float = 2.0) -> float:
    """Lower Z pierce bar during demo throughput (default ±2.0)."""
    block = _throughput_block(cfg)
    try:
        raw = block.get("pierce_z_threshold")
        if raw is not None:
            return max(0.5, float(raw))
    except (TypeError, ValueError):
        pass
    return default
