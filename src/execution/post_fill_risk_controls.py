"""Post-fill virtual stop + dynamic limit — all execution paths (not micro-only)."""

from __future__ import annotations

from typing import Any

from system.engine_log import log_engine


def arm_post_fill_risk_controls(
    *,
    epic: str,
    direction: str,
    size: float,
    entry_level: float,
    deal_id: str,
    stop_distance_pts: float,
    limit_distance_pts: float | None = None,
    cfg: Any | None = None,
) -> dict[str, Any]:
    """
    Arm internal virtual stop (tight) + dynamic limit after broker fill.

    Broker stop may be widened to IG minimum (10–12pt on indices); virtual ceiling
    enforces the configured GBP risk budget before the wide broker stop is hit.
    """
    key = str(epic or "").strip()
    deal = str(deal_id or "").strip()
    if not key or not deal or float(entry_level or 0) <= 0:
        return {"ok": False, "reason": "missing_epic_deal_or_entry"}

    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            cfg = None

    from execution.micro_risk_profile import (
        resolve_micro_tp_sl_for_epic,
        resolve_virtual_ceiling_pts,
    )

    _, _, profile = resolve_micro_tp_sl_for_epic(key, float(size), cfg)
    ceiling = resolve_virtual_ceiling_pts(
        epic=key,
        broker_stop_pts=float(stop_distance_pts),
        profile=profile,
    )

    try:
        from runtime.virtual_stop_loss import register_virtual_stop

        register_virtual_stop(
            epic=key,
            direction=str(direction or "BUY"),
            entry_level=float(entry_level),
            size=float(size),
            deal_id=deal,
            ceiling_pts=ceiling,
        )
    except Exception as exc:
        log_engine(
            f"post_fill_risk: virtual stop arm failed epic={key} deal={deal}: "
            f"{type(exc).__name__}: {exc}"
        )
        return {"ok": False, "reason": f"virtual_stop:{type(exc).__name__}"}

    limit_pts = float(limit_distance_pts or 0.0)
    if limit_pts <= 0:
        limit_pts = float(stop_distance_pts) * 1.5

    try:
        from runtime.dynamic_limit_engine import register_dynamic_limit

        register_dynamic_limit(
            deal_id=deal,
            epic=key,
            direction=str(direction or "BUY"),
            entry_level=float(entry_level),
            limit_pts=limit_pts,
        )
    except Exception:
        pass

    try:
        from runtime.trade_lifecycle import transition, LifecycleState

        transition(
            deal,
            LifecycleState.TRAILING_STOP_ACTIVE,
            message="Virtual stop armed (post-fill)",
            extra={"entry_level": entry_level, "ceiling_pts": ceiling},
        )
    except Exception:
        pass

    log_engine(
        f"post_fill_risk: armed epic={key} deal={deal} "
        f"broker_stop={float(stop_distance_pts):.2f}pt virtual_ceiling={ceiling:.2f}pt"
    )
    return {
        "ok": True,
        "virtual_ceiling_pts": ceiling,
        "broker_stop_pts": float(stop_distance_pts),
    }
