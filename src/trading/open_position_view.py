"""
Dashboard open-position rows — IG sync fields + per-tick quote unrealized P&L.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from data.models import Quote
from system.pnl_math import pip_size_for_epic, realised_pnl_points, round_pnl_pts

# Non-GBP CFD specs — point_value is in position currency per index point (per contract).
INSTRUMENT_PNL_SPEC: dict[str, dict[str, float | str]] = {
    "IX.D.DOW.IFM.IP": {"point_value": 2.0, "currency": "USD"},
    "CS.D.CFPGOLD.CFP.IP": {"point_value": 1.0, "currency": "USD"},
}

_GBPUSD_EPIC = "CS.D.GBPUSD.CFD.IP"
_USD_GBP_PEG = 0.78


def normalize_epic(epic: str) -> str:
    """Canonical IG epic string for slice keys and position rows."""
    return str(epic or "").strip()


def epic_market_label(epic: str) -> str:
    """Configured short market name for dashboard labels."""
    ep = normalize_epic(epic)
    if not ep:
        return "Market"
    try:
        from system.config_loader import get_config
        from trading.instrument_registry import InstrumentRegistry

        inst = InstrumentRegistry(get_config().as_dict()).get_by_epic(ep)
        if inst:
            name = str(inst.get("name") or "").strip()
            if name:
                return name
    except Exception:
        pass
    try:
        from system.market_display import format_market_display_name

        return format_market_display_name(epic=ep)
    except Exception:
        return ep


def row_belongs_to_epic(row: dict[str, Any], epic: str) -> bool:
    """True when a trade row belongs on this market's dashboard slice."""
    target = normalize_epic(epic)
    row_epic = normalize_epic(str(row.get("epic") or ""))
    if row_epic:
        return row_epic == target
    market = str(row.get("market") or "").strip()
    if market:
        return market == epic_market_label(target)
    return False


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


def _quote_mark_trustworthy(entry: float | None, mark: float | None, epic: str) -> bool:
    """Reject streaming marks on a wildly different price scale than entry."""
    if entry is None or mark is None:
        return False
    try:
        entry_f = float(entry)
        mark_f = float(mark)
    except (TypeError, ValueError):
        return False
    if entry_f <= 0 or mark_f <= 0:
        return False
    if pip_size_for_epic(epic) is not None:
        return abs(mark_f - entry_f) < 1.0
    if entry_f > 1000:
        return abs(mark_f - entry_f) / entry_f < 0.25
    return abs(mark_f - entry_f) < max(500.0, entry_f * 0.5)


def unrealized_from_quote(
    side: str,
    entry: float,
    size: float,
    quote: Quote,
    *,
    epic: str = "",
    point_value: float = 1.0,
    currency: str = "GBP",
    point_value_gbp: float | None = None,
) -> tuple[float, float, float]:
    """Return (mark, pnl_pts per unit, pnl_gbp)."""
    mark = _mark_price(side, quote)
    if mark is None:
        return 0.0, 0.0, 0.0
    raw_delta = realised_pnl_points(side, entry, mark)
    pts = raw_delta
    if epic:
        from system.pnl_math import price_delta_to_ig_points

        pts = price_delta_to_ig_points(epic, raw_delta)
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


def tick_has_open_positions_for_epic(tick: dict[str, Any], epic: str) -> bool:
    """True when the dashboard tick holds at least one open row for epic."""
    epic_key = normalize_epic(epic)
    if not epic_key:
        return False
    positions = tick.get("positions")
    if isinstance(positions, list):
        for pos in positions:
            if isinstance(pos, dict) and row_belongs_to_epic(pos, epic_key):
                return True
    markets = tick.get("markets")
    if isinstance(markets, dict):
        mslice = markets.get(epic_key)
        if isinstance(mslice, dict):
            slice_positions = mslice.get("positions")
            if isinstance(slice_positions, list) and slice_positions:
                return True
    return False


def apply_position_view_refresh(
    tick: dict[str, Any],
    epic: str,
    bid: float,
    offer: float,
    *,
    point_value_gbp: float = 1.0,
) -> bool:
    """Recompute open-position marks/P&L in-memory from a streaming quote."""
    epic_key = normalize_epic(epic)
    if not epic_key or bid <= 0 or offer <= 0:
        return False
    if not tick_has_open_positions_for_epic(tick, epic_key):
        return False
    quote = Quote(datetime.now(timezone.utc), bid, offer)
    updated = False

    markets = tick.get("markets")
    if isinstance(markets, dict):
        mslice = markets.get(epic_key)
        if isinstance(mslice, dict):
            slice_positions = mslice.get("positions")
            if isinstance(slice_positions, list) and slice_positions:
                mslice["positions"] = enrich_positions_with_quote(
                    slice_positions,
                    quote,
                    point_value_gbp=point_value_gbp,
                    epic=epic_key,
                )
                updated = True

    positions = tick.get("positions")
    if isinstance(positions, list) and positions:
        tick["positions"] = enrich_positions_with_quote(
            positions,
            quote,
            point_value_gbp=point_value_gbp,
            epic=epic_key,
        )
        updated = True
    return updated


def force_position_view_refresh(
    epic: str,
    bid: float,
    offer: float,
    *,
    tick_age_s: float | None = None,
    stream_status: str = "LIVE",
) -> bool:
    """Push an immediate dashboard position refresh from a live quote tick."""
    from api.snapshot_store import force_position_view_refresh as _store_refresh

    return _store_refresh(
        epic,
        bid,
        offer,
        tick_age_s=tick_age_s,
        stream_status=stream_status,
    )


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
            epic=epic_str,
            point_value=pv,
            currency=ccy,
            point_value_gbp=point_value_gbp,
        )
        if not _quote_mark_trustworthy(entry, mark, epic_str):
            out.append(row)
            continue
        if mark:
            row["current"] = mark
        row["pnl_pts"] = round_pnl_pts(pts, epic_str)
        row["point_value"] = pv
        row["currency"] = ccy
        # Streaming quote wins over stale IG sync UPL so open P&L tracks live prices.
        row["pnl_gbp"] = round(gbp, 2)
        if gbp != 0.0:
            if ccy == "GBP":
                row["pnl_currency"] = float(gbp)
            else:
                row["pnl_currency"] = round(
                    float(gbp) / max(usd_to_gbp_rate(), 1e-9), 2
                )
        out.append(row)
    return out


def sum_open_unrealized_gbp(tick: dict[str, Any]) -> float:
    """Sum unrealized £ P&L from top-level and per-market position rows."""
    total = 0.0
    seen_deals: set[str] = set()

    def _add(pos: dict[str, Any]) -> None:
        nonlocal total
        deal = str(pos.get("deal_id") or "")
        if deal and deal in seen_deals:
            return
        if deal:
            seen_deals.add(deal)
        try:
            total += float(pos.get("pnl_gbp") or 0.0)
        except (TypeError, ValueError):
            pass

    positions = tick.get("positions")
    if isinstance(positions, list):
        for pos in positions:
            if isinstance(pos, dict):
                _add(pos)

    markets = tick.get("markets")
    if isinstance(markets, dict):
        for mslice in markets.values():
            if not isinstance(mslice, dict):
                continue
            for pos in mslice.get("positions") or []:
                if isinstance(pos, dict):
                    _add(pos)
    return round(total, 2)


def apply_display_daily_pnl(tick: dict[str, Any]) -> None:
    """Dashboard Today P&L = realized (closed) + open unrealized."""
    realized_raw = tick.get("realized_daily_pnl_gbp")
    if realized_raw is None:
        daily_raw = tick.get("daily_pnl_gbp")
        if daily_raw is not None and tick.get("open_unrealized_gbp") is not None:
            try:
                realized_raw = float(daily_raw) - float(tick["open_unrealized_gbp"])
            except (TypeError, ValueError):
                realized_raw = daily_raw
        else:
            realized_raw = daily_raw
    try:
        realized = float(realized_raw or 0.0)
    except (TypeError, ValueError):
        realized = 0.0
    tick["realized_daily_pnl_gbp"] = round(realized, 2)
    open_upl = sum_open_unrealized_gbp(tick)
    tick["open_unrealized_gbp"] = open_upl
    tick["daily_pnl_gbp"] = round(realized + open_upl, 2)


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
