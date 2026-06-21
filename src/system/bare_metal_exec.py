"""Bare-metal unified execution hot-path flags — zero I/O on Thread B."""

from __future__ import annotations

import os


def bare_metal_hot_path_active() -> bool:
    """True when Thread B must skip logging, shadow_log, and dashboard publish."""
    if os.environ.get("IG_BARE_METAL_EXEC", "1").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return False
    try:
        from system.ipc.ring_buffer import unified_engine_active

        return unified_engine_active()
    except Exception:
        return False
