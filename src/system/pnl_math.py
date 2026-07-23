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


# IG FX CFD: ~$10 per pip per unit of deal size on USD-quoted majors (100k/lot).
_FX_USD_PER_PIP_PER_UNIT = 10.0


def fx_upl_per_ig_point(
    epic: str, size: float, *, currency: str = "USD"
) -> float | None:
    """Quote-currency P&L per IG pip for FX CFD (size × $10/pip on USD majors)."""
    if pip_size_for_epic(epic) is None:
        return None
    try:
        sz = max(0.0, float(size))
    except (TypeError, ValueError):
        return None
    if sz <= 0:
        return None
    ccy = str(currency or "USD").upper()
    if ccy == "USD":
        return sz * _FX_USD_PER_PIP_PER_UNIT
    return sz * _FX_USD_PER_PIP_PER_UNIT


def direction_multiplier(side: str) -> float:
    return 1.0 if str(side).upper() == "BUY" else -1.0


def realised_pnl_points(side: str, entry: float, exit_price: float) -> float:
    """P&L in index points (per unit); size applied separately for currency display."""
    return (exit_price - entry) * direction_multiplier(side)


def classify_result(pnl_points: float) -> str:
    if abs(pnl_points) < BREAKEVEN_EPSILON:
        return "BREAKEVEN"
    return "WIN" if pnl_points > 0 else "LOSS"


def classify_result_gbp(pnl_gbp: float, *, epsilon_gbp: float = 0.01) -> str:
    """Force WIN/LOSS/BREAKEVEN from true cash differential (never CANCELLED)."""
    if abs(float(pnl_gbp)) < float(epsilon_gbp):
        return "BREAKEVEN"
    return "WIN" if float(pnl_gbp) > 0 else "LOSS"


def settle_gbp_from_ig(
    *,
    profit_and_loss: float | None = None,
    ig_pnl_currency: float | None = None,
    pnl_points: float | None = None,
    contract_size: float | None = None,
    point_value: float = 1.0,
) -> float | None:
    """Resolve true gross GBP from IG settlement packet or points×size×point_value."""
    if profit_and_loss is not None:
        try:
            return float(profit_and_loss)
        except (TypeError, ValueError):
            pass
    if ig_pnl_currency is not None:
        try:
            return float(ig_pnl_currency)
        except (TypeError, ValueError):
            pass
    if pnl_points is None:
        return None
    size = float(contract_size or 0.0)
    pv = float(point_value or 1.0)
    if size <= 0:
        return None
    return float(pnl_points) * size * pv


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


FX_PIP_SIZE_MAJORS = 0.0001


def pip_value_gbp_per_unit(epic: str) -> float:
    """GBP value of one IG pip for one unit of deal size (FX CFD majors)."""
    per_pip_usd = fx_upl_per_ig_point(epic, 1.0)
    if per_pip_usd is None:
        return 1.0
    try:
        from trading.open_position_view import pnl_currency_amount_to_gbp

        return float(pnl_currency_amount_to_gbp(float(per_pip_usd), "USD"))
    except Exception:
        return float(per_pip_usd)


def floating_pnl_gbp_from_prices(
    *,
    epic: str,
    side: str,
    entry: float,
    mark: float,
    size: float,
    spread_price: float = 0.0,
) -> float | None:
    """
    FX-aware floating P&L in GBP.

    FX majors: Gross P&L = (Exit - Entry) / 0.0001 × Size × Pip_Value_GBP
    (SELL inverts via direction multiplier). Spread cost subtracted after scaling.
    """
    try:
        entry_f = float(entry)
        mark_f = float(mark)
        size_f = abs(float(size))
    except (TypeError, ValueError):
        return None
    if entry_f <= 0 or mark_f <= 0 or size_f <= 0:
        return None

    pip = pip_size_for_epic(epic)
    if pip is not None and pip > 0:
        direction = direction_multiplier(side)
        gross_pips = direction * (mark_f - entry_f) / float(pip)
        if spread_price > 0:
            gross_pips -= float(spread_price) / float(pip)
        pip_value_gbp = pip_value_gbp_per_unit(epic)
        return round(gross_pips * size_f * pip_value_gbp, 2)

    raw_pts = realised_pnl_points(side, entry_f, mark_f)
    return round(float(raw_pts) * size_f, 2)
