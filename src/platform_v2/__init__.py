"""Platform V2 — adaptive volatility, compound escalation, feature drift."""

from __future__ import annotations

import os
from typing import Any


def platform_v2_settings() -> dict[str, Any]:
    """Merged platform_v2 block from config (v30 overlay)."""
    try:
        from system.config_loader import ConfigLoader

        raw = ConfigLoader().load() or {}
        block = raw.get("platform_v2")
        if isinstance(block, dict):
            return dict(block)
    except Exception:
        pass
    return {}


def platform_v2_enabled() -> bool:
    # Keep iron-clad baselines stable in pytest; V2 tests patch this on.
    if os.environ.get("IG_AGENT_PYTEST", "").strip() == "1":
        return False
    return bool(platform_v2_settings().get("enabled", False))


__all__ = [
    "platform_v2_enabled",
    "platform_v2_settings",
]
