"""Post-fill virtual stop + dynamic limit — all execution paths (not micro-only)."""

from __future__ import annotations

from typing import Any


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
    """Delegate to unified position risk stack (GBP + virtual + dynamic)."""
    from execution.position_risk_stack import arm_position_risk_stack

    rest = None
    try:
        from runtime.trade_manager import get_dual_core_coordinator

        coord = get_dual_core_coordinator()
        rest = getattr(coord, "_rest", None) if coord else None
    except Exception:
        pass

    return arm_position_risk_stack(
        epic=epic,
        direction=direction,
        size=size,
        entry_level=entry_level,
        deal_id=deal_id,
        stop_distance_pts=stop_distance_pts,
        limit_distance_pts=limit_distance_pts,
        cfg=cfg,
        rest_client=rest,
    )
