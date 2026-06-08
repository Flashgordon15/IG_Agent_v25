"""Per-epic position cap with HEALTHY-state laddering."""

from __future__ import annotations

from typing import Any

from system.config import Config


def base_max_per_epic(cfg: Config) -> int:
    """Configured per-epic cap before dynamic unlock."""
    if cfg.one_position_per_epic:
        return 1
    return max(1, int(cfg.max_positions_per_epic))


def dynamic_max_per_epic(
    *,
    epic: str,
    base_cap: int,
    open_count: int,
    points_state: str,
    tracker: Any,
) -> tuple[int, str]:
    """Scale cap above base_cap when points are HEALTHY and open book is green.

    Tiers (all require points state = HEALTHY):
      base_cap + 1: all open positions on this epic have pnl_gbp > 0
      base_cap + 2: same AND oldest open position is >= 20 minutes old
    """
    if points_state != "HEALTHY":
        return base_cap, f"base ({points_state})"
    if open_count == 0:
        return base_cap, "base"

    snap = tracker.snapshot()
    epic_pos = [p for p in snap.get("positions", []) if p.get("epic") == epic]
    if not epic_pos:
        return base_cap, "base"

    pnl_values = [p.get("pnl_gbp") for p in epic_pos]
    all_profitable = all(v is not None and float(v) > 0 for v in pnl_values)
    if not all_profitable:
        return base_cap, "not all positions profitable"

    open_mins_vals = [float(p.get("open_mins") or 0) for p in epic_pos]
    oldest_mins = max(open_mins_vals)
    if oldest_mins >= 20:
        return base_cap + 2, f"all profitable, oldest {oldest_mins:.0f}m"
    return base_cap + 1, f"all profitable, oldest {oldest_mins:.0f}m"


def effective_max_per_epic(
    *,
    cfg: Config,
    epic: str,
    open_count: int,
    points_engine: Any | None,
    tracker: Any | None,
) -> tuple[int, str]:
    """Resolve gate + execution cap for an epic."""
    base_cap = base_max_per_epic(cfg)
    if points_engine is None or tracker is None:
        return base_cap, "base (no tracker)"
    try:
        state = str(points_engine.get_state())
    except Exception:
        state = "CAUTION"
    return dynamic_max_per_epic(
        epic=epic,
        base_cap=base_cap,
        open_count=open_count,
        points_state=state,
        tracker=tracker,
    )
