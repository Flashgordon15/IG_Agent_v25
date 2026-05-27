"""
Realised P&L helpers — aligned with IG close semantics (read-only).
"""

from __future__ import annotations

BREAKEVEN_EPSILON = 0.05


def direction_multiplier(side: str) -> float:
    return 1.0 if str(side).upper() == "BUY" else -1.0


def realised_pnl_points(side: str, entry: float, exit_price: float) -> float:
    """P&L in index points (per unit); size applied separately for currency display."""
    return (exit_price - entry) * direction_multiplier(side)


def classify_result(pnl_points: float) -> str:
    if abs(pnl_points) < BREAKEVEN_EPSILON:
        return "BREAKEVEN"
    return "WIN" if pnl_points > 0 else "LOSS"


def exit_price_from_ig_close(
    side: str,
    entry: float,
    size: float,
    *,
    level: float,
    upl: float,
) -> float:
    """Prefer IG level at close; fall back to entry ± upl/size."""
    if level and level > 0:
        return float(level)
    if size > 0 and abs(upl) > 1e-9:
        pts = upl / size
        if str(side).upper() == "BUY":
            return entry + pts
        return entry - pts
    return float(entry)


def close_from_ig_position(
    side: str,
    entry: float,
    size: float,
    *,
    level: float = 0.0,
    upl: float = 0.0,
) -> tuple[float, float, str]:
    """
    Compute exit price, realised P&L (points per unit), and WIN/LOSS/BREAKEVEN.
    """
    exit_px = exit_price_from_ig_close(side, entry, size, level=level, upl=upl)
    pnl = realised_pnl_points(side, entry, exit_px)
    return exit_px, pnl, classify_result(pnl)
