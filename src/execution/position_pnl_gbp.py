"""Broker-authoritative open-position P&L in GBP (UPL or IG bid/offer marks)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from data.models import Quote
from trading.open_position_view import (
    extract_broker_profit_and_loss,
    instrument_pnl_spec,
    pnl_currency_amount_to_gbp,
    unrealized_from_quote,
)
from trading.open_position_view import _quote_mark_trustworthy


def pnl_gbp_for_open_row(
    *,
    epic: str,
    direction: str,
    entry_level: float,
    size: float,
    upl: float | None = None,
    bid: float = 0.0,
    offer: float = 0.0,
    currency: str = "",
) -> float | None:
    """
    Resolve unrealized P&L in GBP for one open contract.

    Prefers broker UPL when present; otherwise computes from IG bid/offer marks
    (never Yahoo hub — scale mismatch breaks Gold/indices).

    Untrusted when entry_level <= 0 — never treat broker UPL alone as actionable
    soft-loss PnL (ghost −£122 marks with entry=0).
    """
    if entry_level <= 0 or size <= 0:
        return None

    if upl is not None and abs(float(upl)) >= 0.001:
        spec = instrument_pnl_spec(epic)
        ccy = str(currency or spec.get("currency") or "GBP").upper()
        return float(pnl_currency_amount_to_gbp(float(upl), ccy))

    if bid <= 0 or offer <= 0:
        return None

    mark = float(bid if str(direction or "").upper() == "BUY" else offer)
    if not _quote_mark_trustworthy(float(entry_level), mark, epic):
        return None

    spec = instrument_pnl_spec(epic)
    ccy = str(currency or spec.get("currency") or "GBP").upper()
    quote = Quote(time=datetime.now(timezone.utc), bid=float(bid), offer=float(offer))
    _, _, gbp = unrealized_from_quote(
        str(direction or "BUY"),
        float(entry_level),
        float(size),
        quote,
        epic=epic,
        currency=ccy,
    )
    return float(gbp)


def pnl_gbp_from_ig_item(item: dict[str, Any]) -> float | None:
    """Extract GBP P&L from a raw GET /positions row."""
    pos = item.get("position") or {}
    mkt = item.get("market") or {}
    epic = str(mkt.get("epic") or pos.get("epic") or "").strip()
    if not epic:
        return None
    upl, ccy = extract_broker_profit_and_loss(pos)
    if upl is None:
        upl, ccy = extract_broker_profit_and_loss(item)
    try:
        entry = float(pos.get("level") or pos.get("openLevel") or 0)
        size = float(pos.get("size") or 0)
    except (TypeError, ValueError):
        return None
    direction = str(pos.get("direction") or "BUY").upper()
    bid = float(mkt.get("bid") or 0)
    offer = float(mkt.get("offer") or 0)
    return pnl_gbp_for_open_row(
        epic=epic,
        direction=direction,
        entry_level=entry,
        size=size,
        upl=float(upl) if upl is not None else None,
        bid=bid,
        offer=offer,
        currency=str(ccy or mkt.get("currency") or ""),
    )
