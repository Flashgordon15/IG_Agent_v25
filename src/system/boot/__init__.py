"""BootState gate runners — lazy exports to avoid pulling G2–G5 at import time."""

from __future__ import annotations

from typing import Any

_LAZY_EXPORTS = {
    "BootContext": ("system.boot.context", "BootContext"),
    "Gate1FatalError": ("system.boot.exceptions", "Gate1FatalError"),
    "Gate1Runner": ("system.boot.gate1_runner", "Gate1Runner"),
    "Gate2Runner": ("system.boot.gate2_runner", "Gate2Runner"),
    "Gate3Runner": ("system.boot.gate3_runner", "Gate3Runner"),
    "Gate4Runner": ("system.boot.gate4_runner", "Gate4Runner"),
    "Gate5Runner": ("system.boot.gate5_runner", "Gate5Runner"),
    "create_boot_coordinator": ("system.boot.coordinator_factory", "create_boot_coordinator"),
    "run_gate1_preflight": ("system.boot.gate1_preflight", "run_gate1_preflight"),
}

__all__ = list(_LAZY_EXPORTS.keys())


def __getattr__(name: str) -> Any:
    if name not in _LAZY_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_path, attr = _LAZY_EXPORTS[name]
    import importlib

    module = importlib.import_module(module_path)
    return getattr(module, attr)
