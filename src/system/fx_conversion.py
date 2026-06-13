"""
FX conversion to account GBP — IG commercial markup on cross-currency P&L.

IG applies ~0.5% above the underlying market rate on currency conversion
(non-GBP P&L → GBP account balance).
"""

from __future__ import annotations

from typing import Final

IG_COMMERCIAL_FX_MARKUP: Final[float] = 0.005
_GBPUSD_EPIC = "CS.D.GBPUSD.CFD.IP"
_EURGBP_EPIC = "CS.D.EURGBP.CFD.IP"
_USD_GBP_PEG = 0.78
_EUR_GBP_PEG = 0.86


def _hub_mid(epic: str) -> float | None:
    try:
        from system.market_data_hub import get_market_data_hub

        snap = get_market_data_hub().get_snapshot(epic)
        if snap is not None and float(snap.bid) > 0:
            return float(snap.bid)
    except Exception:
        pass
    return None


def spot_usd_per_gbp() -> float:
    """GBP/USD bid — how many USD per 1 GBP."""
    mid = _hub_mid(_GBPUSD_EPIC)
    if mid is not None and mid > 0:
        return mid
    return 1.0 / _USD_GBP_PEG


def spot_gbp_per_usd() -> float:
    """USD → GBP spot (before IG commercial markup)."""
    usd_per_gbp = spot_usd_per_gbp()
    if usd_per_gbp <= 0:
        return _USD_GBP_PEG
    return 1.0 / usd_per_gbp


def spot_gbp_per_eur() -> float:
    """EUR → GBP spot (before IG commercial markup)."""
    mid = _hub_mid(_EURGBP_EPIC)
    if mid is not None and mid > 0:
        return mid
    return _EUR_GBP_PEG


def apply_ig_fx_markup(gbp_before_fee: float) -> float:
    """Deduct IG commercial conversion fee from favourable conversion."""
    if gbp_before_fee >= 0:
        return float(gbp_before_fee) * (1.0 - IG_COMMERCIAL_FX_MARKUP)
    return float(gbp_before_fee) * (1.0 + IG_COMMERCIAL_FX_MARKUP)


def convert_to_account_gbp(amount: float, currency: str) -> float:
    """Convert broker-reported P&L in position currency to account GBP (net of FX fee)."""
    ccy = str(currency or "GBP").upper()
    amt = float(amount)
    if ccy == "GBP":
        return amt
    if ccy == "USD":
        return apply_ig_fx_markup(amt * spot_gbp_per_usd())
    if ccy == "EUR":
        return apply_ig_fx_markup(amt * spot_gbp_per_eur())
    return amt
