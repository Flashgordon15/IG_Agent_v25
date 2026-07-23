"""Resolve IG broker fill price for post-fill risk arming."""

from __future__ import annotations

from typing import Any


def resolve_broker_fill_level(
    confirm: dict[str, Any] | None,
    *,
    hub_mid: float = 0.0,
) -> float:
    """
    Prefer IG confirm ``level`` (spreadbet truth) over Yahoo hub mid.

    Hub mids are on a different scale (~100) than IG fills (~52k / ~3.3k) when
    pricing transport is Yahoo — using hub mid breaks virtual stops and trails.
    """
    raw = {}
    if isinstance(confirm, dict):
        raw = confirm.get("raw") if isinstance(confirm.get("raw"), dict) else confirm
    for key in ("level", "openLevel", "fillLevel"):
        try:
            px = float(raw.get(key) or 0)
        except (TypeError, ValueError):
            px = 0.0
        if px > 0:
            return px
    return float(hub_mid or 0.0)
