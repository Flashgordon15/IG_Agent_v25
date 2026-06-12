"""
Realised P&L helpers — aligned with IG close semantics (read-only).
"""

from __future__ import annotations

BREAKEVEN_EPSILON = 0.05

# FX CFD epics: IG stop_distance_points are pip-style (not raw price units).
_FX_PIP_2DP = ("USDJPY", "EURJPY", "GBPJPY")
_FX_PIP_4DP = (
    "EURUSD",
    "GBPUSD",
    "AUDUSD",
    "EURGBP",
    "USDCAD",
    "NZDUSD",
    "USDCHF",
)


def pip_size_for_epic(epic: str) -> float | None:
    """Return one IG pip in price units, or None for non-FX instruments."""
    key = str(epic or "").upper()
    if not key.startswith("CS.D.") or "CFD" not in key:
        return None
    if any(
        token in key for token in ("CFPGOLD", "CFPSILVER", "CFPPLAT", "CRUDE", "OIL")
    ):
        return None
    if any(token in key for token in _FX_PIP_2DP):
        return 0.01
    if any(token in key for token in _FX_PIP_4DP):
        return 0.0001
    return None


def price_delta_to_ig_points(epic: str, price_delta: float) -> float:
    """Convert a raw price move into IG dashboard points (pips for FX)."""
    pip = pip_size_for_epic(epic)
    if pip is None or pip <= 0:
        return float(price_delta)
    return float(price_delta) / pip


def ig_points_to_price_delta(epic: str, ig_points: float) -> float:
    """Convert IG points back to a price move (inverse of price_delta_to_ig_points)."""
    pip = pip_size_for_epic(epic)
    if pip is None or pip <= 0:
        return float(ig_points)
    return float(ig_points) * pip


def display_pnl_pts_precision(epic: str) -> int:
    return 2 if pip_size_for_epic(epic) is not None else 1


def round_pnl_pts(pts: float, epic: str) -> float:
    return round(float(pts), display_pnl_pts_precision(epic))


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
