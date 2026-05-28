"""Margin headroom check before broker submit — fail silently on balance errors."""

from __future__ import annotations

from typing import Any

from system.config import Config
from system.engine_log import log_engine


def apply_margin_preflight(
    cfg: Config,
    execution_params: dict[str, Any],
    account_available: float | None,
) -> dict[str, Any]:
    """
    Ensure available margin covers stop × size × point_value × 2.
    Reduces size or returns params unchanged when balance unknown.
    """
    out = dict(execution_params)
    try:
        stop_pts = float(out.get("risk", cfg.stop_distance_points))
        size = float(out.get("size", cfg.trade_size))
        point_value = float(cfg.get("ig_point_value_gbp", 1.0))
        if stop_pts <= 0 or size <= 0 or point_value <= 0:
            return out
        required = stop_pts * size * point_value * 2.0
        if account_available is None:
            return out
        avail = float(account_available)
        log_engine(f"Margin OK: £{avail:.0f} available")
        if avail >= required:
            return out
        max_size = avail / (stop_pts * point_value * 2.0)
        min_size = float(cfg.adaptive_min_trade_size)
        if max_size < min_size:
            log_engine(
                f"Margin skip: £{avail:.0f} available < £{required:.0f} required"
            )
            out["_margin_skip"] = True
            return out
        reduced = max(min_size, min(size, max_size))
        if reduced < size - 1e-9:
            log_engine(
                f"Margin size reduced {size:.2f} -> {reduced:.2f} "
                f"(£{avail:.0f} available, need £{required:.0f})"
            )
            out["size"] = reduced
        return out
    except Exception:
        return out
