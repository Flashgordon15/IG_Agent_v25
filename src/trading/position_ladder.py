"""Per-epic position cap with HEALTHY-state laddering."""

from __future__ import annotations

import math
from typing import Any

from system.config import Config


def truncate_to_broker_lot(size_value: float) -> float:
    """IG broker contract — strict two-decimal lot truncation (floor, never round up)."""
    return math.floor(float(size_value) * 100) / 100.0


def is_valid_broker_lot(size_value: float) -> bool:
    """True when *size_value* has at most two decimal places (e.g. 1.12 ok, 1.125 not)."""
    scaled = truncate_to_broker_lot(size_value)
    return abs(float(size_value) - scaled) < 1e-9


def apply_broker_lot_contract(size: float, epic: str = "") -> float:
    """Operational epic floor, then two-decimal truncation — final pre-dispatch weld."""
    from execution.size_floors import operational_size_floor

    floor = operational_size_floor(epic)
    raw = max(float(size), floor) if floor > 0 else float(size)
    return truncate_to_broker_lot(raw)


def finalize_dispatch_lot_size(
    size_value: float,
    *,
    epic: str = "",
    micro_confidence: float = 1.0,
    config: Any | None = None,
    apply_overnight_scale: bool = True,
) -> tuple[float, str]:
    """
    Scale (optional overnight band) then weld to broker two-decimal contract.

    Example: 1.5 × 0.75 risk band = 1.125 → 1.12.
    """
    size = float(size_value)
    reason = "raw"
    if apply_overnight_scale and epic:
        mult, mult_reason = overnight_dispatch_size_scale(
            micro_confidence, epic=epic, config=config
        )
        if mult < 1.0:
            size = size * mult
            reason = mult_reason
    welded = apply_broker_lot_contract(size, epic)
    if welded != size:
        reason = f"{reason}; broker_lot {size:.4f}->{welded:.2f}"
    return max(0.01, welded), reason


def weld_execution_params_lot(
    execution_params: dict[str, Any],
    *,
    epic: str = "",
    micro_confidence: float = 1.0,
    config: Any | None = None,
) -> dict[str, Any]:
    """Mutate execution_params / REST maps — enforce two-decimal lot before dispatch."""
    out = dict(execution_params)
    raw = float(out.get("size") or 0.0)
    if raw <= 0:
        return out
    welded, note = finalize_dispatch_lot_size(
        raw,
        epic=epic,
        micro_confidence=micro_confidence,
        config=config,
        apply_overnight_scale=False,
    )
    out["size"] = welded
    if out.get("gate_approved_size") is not None:
        out["gate_approved_size"] = truncate_to_broker_lot(
            float(out["gate_approved_size"])
        )
    notes = str(out.get("notes") or "")
    if "broker_lot" in note and note not in notes:
        out["notes"] = f"{notes}, {note}" if notes else note
    out["broker_lot_welded"] = True
    return out


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
    ladder_cap, ladder_reason = dynamic_max_per_epic(
        epic=epic,
        base_cap=base_cap,
        open_count=open_count,
        points_state=state,
        tracker=tracker,
    )
    try:
        from intelligence.autopilot_scaling import effective_autopilot_max_per_epic
        from intelligence.policy import autopilot_scaling_enabled

        if autopilot_scaling_enabled(cfg):
            merged, reason, _rating = effective_autopilot_max_per_epic(
                cfg=cfg,
                epic=epic,
                base_cap=base_cap,
                ladder_cap=ladder_cap,
                ladder_reason=ladder_reason,
            )
            return merged, reason
    except Exception:
        pass
    try:
        from intelligence.target_engine import apply_target_position_cap

        return apply_target_position_cap(ladder_cap, base_cap, ladder_reason, cfg=cfg)
    except Exception:
        pass
    return ladder_cap, ladder_reason


def overnight_dispatch_size_scale(
    micro_confidence: float,
    *,
    epic: str,
    config: Any | None = None,
) -> tuple[float, str]:
    """Pre-dispatch lot scaler — delegates to liquidity_wave overnight bands."""
    try:
        from intelligence.liquidity_wave import overnight_volatility_size_multiplier

        return overnight_volatility_size_multiplier(
            micro_confidence, epic=epic, config=config
        )
    except Exception:
        return 1.0, "unavailable"
