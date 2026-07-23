"""Plausible mid bands for night-matrix / desk epics.

Prevents poisoned feed races (e.g. Yahoo ~100 DXY-scale values landing on
EURUSD, or index micro-channel ~100 bound as DOW) from corrupting hub quotes,
fulfillment cache, and the Quantum Terminal scanner.
"""

from __future__ import annotations

from typing import Any


def plausible_mid_for_epic(epic: str | None, mid: float) -> bool:
    """Return True when *mid* is in a sane absolute band for *epic*."""
    try:
        m = float(mid)
    except (TypeError, ValueError):
        return False
    if not (m > 0.0) or m != m:  # noqa: PLR0124 — NaN check
        return False
    e = str(epic or "").strip().upper()
    if not e:
        return False
    if "EURUSD" in e or "GBPUSD" in e or e.endswith("=X"):
        return 0.5 < m < 2.5
    if "CFPGOLD" in e or "XAU" in e or e == "GC" or "GOLD" in e:
        return 500.0 <= m <= 20000.0
    if "CRUDE" in e or "BRENT" in e or "OIL" in e or e.startswith("CL"):
        return 20.0 <= m < 500.0
    if "DOW" in e or "DAX" in e or "NIKKEI" in e or "FTSE" in e or "NASDAQ" in e:
        return m >= 1000.0
    if "IFM" in e or e.startswith("IX."):
        return m >= 1000.0
    # Unknown epic — reject the notorious ~100 micro-channel / DXY band
    if 50.0 <= m <= 200.0:
        return False
    return True


def sanitize_quote_levels(
    epic: str | None,
    *,
    bid: float,
    offer: float,
    mid: float | None = None,
) -> tuple[float, float, float] | None:
    """Return (bid, offer, mid) when levels are mutually consistent and plausible."""
    try:
        b = float(bid)
        o = float(offer)
    except (TypeError, ValueError):
        return None
    if b <= 0.0 or o <= 0.0 or o < b:
        return None
    m = float(mid) if mid is not None and mid > 0 else (b + o) * 0.5
    if not plausible_mid_for_epic(epic, m):
        return None
    return b, o, m


def filter_market_quotes(
    quotes: dict[str, Any] | None,
    *,
    prior: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Drop implausible mids; fall back to *prior* good quote for the epic when available."""
    out: dict[str, Any] = {}
    src = quotes if isinstance(quotes, dict) else {}
    prev = prior if isinstance(prior, dict) else {}
    for epic, row in src.items():
        if not isinstance(row, dict):
            continue
        try:
            bid = float(row.get("bid") or 0)
            offer = float(row.get("offer") or 0)
            mid = float(row.get("mid") or row.get("last_price") or 0)
        except (TypeError, ValueError):
            continue
        if mid <= 0 and bid > 0 and offer > bid:
            mid = (bid + offer) / 2.0
        if plausible_mid_for_epic(str(epic), mid):
            out[str(epic)] = dict(row)
            out[str(epic)]["mid"] = mid
            out[str(epic)]["last_price"] = mid
            continue
        fallback = prev.get(str(epic))
        if isinstance(fallback, dict):
            try:
                fmid = float(fallback.get("mid") or fallback.get("last_price") or 0)
            except (TypeError, ValueError):
                fmid = 0.0
            if plausible_mid_for_epic(str(epic), fmid):
                kept = dict(fallback)
                kept["stale_fallback"] = True
                kept["rejected_mid"] = mid
                out[str(epic)] = kept
    return out
