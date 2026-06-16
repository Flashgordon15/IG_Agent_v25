"""Passive risk-lock / config restriction fields for dashboard diagnostics."""

from __future__ import annotations

from typing import Any


def _live_config_restrictions() -> dict[str, Any]:
    try:
        from system.config_loader import get_config

        cfg = get_config()
        max_open = int(cfg.max_open_positions)
        rotation_on = bool(cfg.get("enforce_top3_rotation_filter", True))
    except Exception:
        max_open = 0
        rotation_on = True
    return {
        "max_open_positions": max_open,
        "enforce_top3_rotation_filter": rotation_on,
    }


def enrich_restrictions_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Inject ``config`` and ``system_state.hydration.max_open_positions`` for UI standby banner.
    """
    out = dict(payload)
    restrictions = _live_config_restrictions()
    out["config"] = dict(restrictions)

    sys_snap = dict(out.get("system_state") or {})
    hydration = dict(sys_snap.get("hydration") or {})
    hydration["max_open_positions"] = restrictions["max_open_positions"]
    sys_snap["hydration"] = hydration
    out["system_state"] = sys_snap
    return out
