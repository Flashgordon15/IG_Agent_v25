"""
P&L accounting normalisation — NET broker figures for learning plane rows.
"""

from __future__ import annotations

from typing import Any


def normalize_shadow_net_pnl(row: dict[str, Any]) -> dict[str, Any]:
    """
    Ensure shadow registry rows prefer IG-confirmed NET currency P&L.

    ``ig_pnl_currency`` from IG transaction history is authoritative (spread,
    FX conversion, and funding already embedded). ``pnl_points`` is retained for
    diagnostics but must not override currency P&L when present.
    """
    out = dict(row)
    ig = out.get("ig_pnl_currency")
    if ig is not None:
        try:
            ig_f = float(ig)
        except (TypeError, ValueError):
            ig_f = None
        else:
            out["ig_pnl_currency"] = ig_f
            currency = str(out.get("currency") or "GBP").upper()
            if currency != "GBP":
                from system.fx_conversion import convert_to_account_gbp

                out["ig_pnl_gbp"] = round(convert_to_account_gbp(ig_f, currency), 2)
            else:
                out["ig_pnl_gbp"] = round(ig_f, 2)
            out["pnl_is_net"] = True
            return out
    out["pnl_is_net"] = False
    return out
