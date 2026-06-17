"""Intelligence layer config helpers — no boot side effects."""

from __future__ import annotations

from typing import Any


def intelligence_layer_config(cfg: Any | None) -> dict[str, Any]:
    if cfg is None:
        return {}
    block = cfg.get("intelligence_layer")
    return block if isinstance(block, dict) else {}


def intelligence_enabled(cfg: Any | None) -> bool:
    return bool(intelligence_layer_config(cfg).get("enabled", False))


def autopilot_config(cfg: Any | None) -> dict[str, Any]:
    block = intelligence_layer_config(cfg)
    nested = block.get("autopilot_scaling")
    return nested if isinstance(nested, dict) else {}


def autopilot_scaling_enabled(cfg: Any | None) -> bool:
    if not intelligence_enabled(cfg):
        return False
    ap = autopilot_config(cfg)
    return bool(ap.get("enabled", True))


def cockpit_config(cfg: Any | None) -> dict[str, Any]:
    block = intelligence_layer_config(cfg)
    nested = block.get("cockpit")
    return nested if isinstance(nested, dict) else {}


def target_engine_config(cfg: Any | None) -> dict[str, Any]:
    block = intelligence_layer_config(cfg)
    nested = block.get("target_engine")
    return nested if isinstance(nested, dict) else {}
