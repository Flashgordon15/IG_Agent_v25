"""
Dashboard open-position rows — IG sync fields + per-tick quote unrealized P&L.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from data.models import Quote
from system.pnl_math import realised_pnl_points

# Non-GBP CFD specs — point_value is in position currency per index point (per contract).
INSTRUMENT_PNL_SPEC: dict[str, dict[str, float | str]] = {
    "IX.D.DOW.IFM.IP": {"point_value": 2.0, "currency": "USD"},
    "CS.D.CFPGOLD.CFP.IP": {"point_value": 1.0, "currency": "USD"},
}

_GBPUSD_EPIC = "CS.D.GBPUSD.CFD.IP"
_USD_GBP_PEG = 0.78


def instrument_pnl_spec(epic: str) -> dict[str, float | str]:
    spec = INSTRUMENT_PNL_SPEC.get(str(epic or "").strip())
    if spec:
        return {
            "point_value": float(spec.get("point_value") or 1.0),
            "currency": str(spec.get("currency") or "GBP").upper(),
        }
    return {"point_value": 1.0, "currency": "GBP"}


def usd_to_gbp_rate() -> float:
    """USD→GBP from live GBP/USD hub quote, else conservative M0 peg."""
    try:
        from system.market_data_hub import get_market_data_hub

        snap = get_market_data_hub().get_snapshot(_GBPUSD_EPIC)
        if snap is not None and float(snap.bid) > 0:
            return 1.0 / float(snap.bid)
    except Exception:
        pass
    return _USD_GBP_PEG


def pnl_currency_amount_to_gbp(amount: float, currency: str) -> float:
    """Broker UPL is already in position currency (e.g. USD for Wall St Cash)."""
    ccy = str(currency or "GBP").upper()
    if ccy == "USD":
        return float(amount) * usd_to_gbp_rate()
    return float(amount)


def raw_points_pnl_to_gbp(
    raw_points_pnl: float,
    *,
    point_value: float,
    currency: str,
) -> float:
    """Index points × contract multiplier → GBP for dashboard."""
    ccy = str(currency or "GBP").upper()
    notional = float(raw_points_pnl) * float(point_value)
    if ccy == "USD":
        return notional * usd_to_gbp_rate()
    return notional


def _compute_open_mins(pos: dict[str, Any]) -> float | None:
    """Return minutes since entry from open_mins (pre-computed) or opened_at timestamp."""
    if pos.get("open_mins") is not None:
        try:
            return float(pos["open_mins"])
        except (TypeError, ValueError):
            pass
    opened_at = pos.get("opened_at") or pos.get("entry_time")
    if not opened_at:
        return None
    try:
        opened = datetime.fromisoformat(str(opened_at).replace("Z", ""))
        return max(0.0, (datetime.now() - opened).total_seconds() / 60.0)
    except Exception:
        return None


def _side(pos: dict[str, Any]) -> str:
    return str(pos.get("side") or pos.get("direction") or "").upper()


def _entry(pos: dict[str, Any]) -> float | None:
    for key in ("entry", "level", "open_level"):
        v = pos.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def _mark_price(side: str, quote: Quote) -> float | None:
    if side == "BUY":
        return float(quote.bid) if quote.bid else None
    if side == "SELL":
        return float(quote.offer) if quote.offer else None
    return float(quote.mid) if quote.mid else None


def unrealized_from_quote(
    side: str,
    entry: float,
    size: float,
    quote: Quote,
    *,
    point_value: float = 1.0,
    currency: str = "GBP",
    point_value_gbp: float | None = None,
) -> tuple[float, float, float]:
    """Return (mark, pnl_pts per unit, pnl_gbp)."""
    mark = _mark_price(side, quote)
    if mark is None:
        return 0.0, 0.0, 0.0
    pts = realised_pnl_points(side, entry, mark)
    pv = float(point_value)
    if point_value_gbp is not None and str(currency or "GBP").upper() == "GBP":
        pv = float(point_value_gbp)
    raw_pts = pts * max(0.0, float(size))
    gbp = raw_points_pnl_to_gbp(raw_pts, point_value=pv, currency=currency)
    return mark, pts, gbp


def normalize_sync_position(pos: dict[str, Any]) -> dict[str, Any]:
    """Map IgPositionSync snapshot row to dashboard field names."""
    side = _side(pos)
    entry = _entry(pos)
    stop = pos.get("stop")
    if stop is None:
        stop = pos.get("stop_level")
    target = pos.get("target")
    if target is None:
        target = pos.get("limit_level")
    bid = float(pos.get("bid") or 0)
    offer = float(pos.get("offer") or 0)
    current = pos.get("current") or pos.get("mid")
    if current is None and side == "BUY" and bid:
        current = bid
    elif current is None and side == "SELL" and offer:
        current = offer
    epic = str(pos.get("epic") or "")
    spec = instrument_pnl_spec(epic)
    currency = str(pos.get("currency") or spec["currency"]).upper()
    point_value = float(pos.get("point_value") or spec["point_value"])
    pnl_currency = pos.get("pnl_currency")
    if pnl_currency is None:
        pnl_currency = pos.get("upl")
    if pnl_currency is None:
        pnl_currency = pos.get("pnl_gbp")
    pnl_gbp: float | None = None
    if pnl_currency is not None:
        try:
            pnl_gbp = round(
                pnl_currency_amount_to_gbp(float(pnl_currency), currency), 2
            )
        except (TypeError, ValueError):
            pnl_gbp = None
    return {
        "deal_id": pos.get("deal_id") or pos.get("dealId") or "",
        "side": side,
        "entry": entry,
        "current": current,
        "stop": stop,
        "target": target,
        "pnl_currency": float(pnl_currency) if pnl_currency is not None else None,
        "pnl_gbp": pnl_gbp,
        "pnl_pts": pos.get("pnl_pts"),
        "size": float(pos.get("size") or 0),
        "trail_active": bool(pos.get("trail_active", False)),
        "breakeven_hit": bool(pos.get("breakeven_hit", False)),
        "open_mins": _compute_open_mins(pos),
        "epic": epic,
        "point_value": point_value,
        "currency": currency,
    }


def enrich_positions_with_quote(
    positions: list[dict[str, Any]],
    quote: Quote | None,
    *,
    point_value_gbp: float,
    epic: str | None = None,
) -> list[dict[str, Any]]:
    """Refresh mark and unrealized P&L from streaming quote (no REST)."""
    if not positions or quote is None:
        return positions
    out: list[dict[str, Any]] = []
    for raw in positions:
        row = dict(raw)
        if epic and str(row.get("epic") or "") and str(row.get("epic")) != epic:
            out.append(row)
            continue
        side = _side(row)
        entry = _entry(row)
        size = float(row.get("size") or 0)
        epic_str = str(row.get("epic") or epic or "")
        spec = instrument_pnl_spec(epic_str)
        pv = float(row.get("point_value") or spec["point_value"])
        ccy = str(row.get("currency") or spec["currency"]).upper()
        pnl_currency = row.get("pnl_currency")
        if pnl_currency is None and row.get("pnl_gbp") is not None and ccy == "GBP":
            pnl_currency = row.get("pnl_gbp")
        if not side or entry is None or size <= 0:
            out.append(row)
            continue
        mark, pts, gbp = unrealized_from_quote(
            side,
            entry,
            size,
            quote,
            point_value=pv,
            currency=ccy,
            point_value_gbp=point_value_gbp,
        )
        if mark:
            row["current"] = mark
        row["pnl_pts"] = round(pts, 1)
        row["point_value"] = pv
        row["currency"] = ccy
        # Prefer broker UPL in position currency; quote math when UPL missing/zero.
        try:
            ig_amt = float(pnl_currency) if pnl_currency is not None else 0.0
        except (TypeError, ValueError):
            ig_amt = 0.0
        if ig_amt != 0.0:
            row["pnl_currency"] = ig_amt
            row["pnl_gbp"] = round(pnl_currency_amount_to_gbp(ig_amt, ccy), 2)
        else:
            row["pnl_gbp"] = round(gbp, 2)
        out.append(row)
    return out


def positions_from_store_rows(
    rows: list[Any],
    quote: Quote | None,
    *,
    point_value_gbp: float,
) -> list[dict[str, Any]]:
    """Fallback when IG sync list is empty but LearningStore has OPEN rows."""
    out: list[dict[str, Any]] = []
    for tr in rows:
        keys = tr.keys() if hasattr(tr, "keys") else ()
        side = str(tr["side"]).upper() if "side" in keys else ""
        entry = float(tr["entry"])
        size = float(tr["size"] or 0)
        deal_id = ""
        if "ig_deal_id" in keys and tr["ig_deal_id"]:
            deal_id = str(tr["ig_deal_id"])
        elif "deal_id" in keys and tr["deal_id"]:
            deal_id = str(tr["deal_id"])
        epic = str(tr.get("epic") or "")
        spec = instrument_pnl_spec(epic)
        row: dict[str, Any] = {
            "deal_id": deal_id,
            "side": side,
            "entry": entry,
            "current": None,
            "stop": float(tr["stop"]) if tr.get("stop") is not None else None,
            "target": float(tr["target"]) if tr.get("target") is not None else None,
            "pnl_gbp": None,
            "pnl_currency": None,
            "pnl_pts": None,
            "size": size,
            "trail_active": False,
            "breakeven_hit": False,
            "open_mins": None,
            "epic": epic,
            "point_value": float(spec["point_value"]),
            "currency": str(spec["currency"]),
        }
        upl = tr.get("unrealized_pnl") if hasattr(tr, "get") else None
        if upl is not None:
            try:
                row["pnl_gbp"] = float(upl)
            except (TypeError, ValueError):
                pass
        out.append(row)
    return enrich_positions_with_quote(
        out, quote, point_value_gbp=point_value_gbp, epic=None
    )
