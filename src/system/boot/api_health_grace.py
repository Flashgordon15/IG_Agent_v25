"""Post-bind health grace window — defer heavy hydrators until /health is stable."""

from __future__ import annotations

import os
import time

_boot_bound_at: float = 0.0


def mark_api_bound() -> None:
    """Call once when :8080 bind completes."""
    global _boot_bound_at
    if _boot_bound_at <= 0.0:
        _boot_bound_at = time.monotonic()


def health_grace_sec() -> float:
    try:
        return max(
            0.0,
            float(os.environ.get("IG_API_HEALTH_GRACE_SEC", "15")),
        )
    except (TypeError, ValueError):
        return 15.0


def heavy_services_defer_sec() -> float:
    try:
        return max(
            health_grace_sec(),
            float(os.environ.get("IG_API_HEAVY_SERVICES_DEFER_SEC", "20")),
        )
    except (TypeError, ValueError):
        return 20.0


def health_grace_active() -> bool:
    if _boot_bound_at <= 0.0:
        return True
    return (time.monotonic() - _boot_bound_at) < health_grace_sec()


def reset_api_health_grace_for_tests() -> None:
    global _boot_bound_at
    _boot_bound_at = 0.0
