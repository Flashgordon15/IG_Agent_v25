"""Air-gapped broker UPL hard floor — bypasses local entry/mark math.

Reads absolute Unrealized P&L from the IG payload. When a *valid* UPL
(or quote-derived PnL) is ≤ floor GBP, emits a flatten action.

Null / missing UPL must NOT spam fail-safe flattens. Prefer quote-derived
PnL; if still unavailable, log distinctly and skip close.
"""

from __future__ import annotations

from typing import Any

from system.engine_log import log_engine


DEFAULT_FLOOR_GBP = -100.0


def _floor_cfg(cfg: Any | None) -> dict[str, Any]:
    block: Any = {}
    if cfg is not None and hasattr(cfg, "get"):
        block = cfg.get("broker_upl_hard_floor") or {}
    elif cfg is not None:
        block = getattr(cfg, "broker_upl_hard_floor", None) or {}
    if not isinstance(block, dict):
        block = {}
    return {
        "enabled": bool(block.get("enabled", True)),
        "floor_gbp": float(block.get("floor_gbp", DEFAULT_FLOOR_GBP) or DEFAULT_FLOOR_GBP),
        # Default OFF — null UPL alone must not flatten storms.
        "null_upl_fail_safe": bool(block.get("null_upl_fail_safe", False)),
        "exception_fail_safe": bool(block.get("exception_fail_safe", True)),
        "allow_quote_derived_pnl": bool(block.get("allow_quote_derived_pnl", True)),
    }


def raw_broker_upl_gbp_from_ig_item(item: dict[str, Any]) -> float | None:
    """Absolute UPL in GBP from broker payload — no entry_level required."""
    from trading.open_position_view import (
        extract_broker_profit_and_loss,
        pnl_currency_amount_to_gbp,
    )

    pos = item.get("position") or {}
    mkt = item.get("market") or {}
    upl, ccy = extract_broker_profit_and_loss(pos)
    if upl is None:
        upl, ccy = extract_broker_profit_and_loss(item)
    if upl is None:
        # Flat numeric on normalized snapshot rows / coalesce echo
        for key in ("pnl_gbp", "upl_gbp", "unrealized_pnl_gbp", "_pnl_gbp"):
            raw = item.get(key)
            if raw is None and isinstance(pos, dict):
                raw = pos.get(key)
            if raw is not None:
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    pass
        return None
    ccy_s = str(ccy or mkt.get("currency") or "GBP").upper()
    return float(pnl_currency_amount_to_gbp(float(upl), ccy_s))


def quote_derived_pnl_gbp_from_ig_item(item: dict[str, Any]) -> float | None:
    """Derive GBP PnL from entry + IG bid/offer (or hub quote) when UPL missing."""
    try:
        from execution.position_pnl_gbp import pnl_gbp_from_ig_item

        gbp = pnl_gbp_from_ig_item(item)
        if gbp is not None:
            return float(gbp)
    except Exception:
        pass
    pos = item.get("position") or {}
    mkt = item.get("market") or {}
    epic = str(mkt.get("epic") or item.get("epic") or "").strip()
    if not epic:
        return None
    try:
        entry = float(pos.get("level") or pos.get("openLevel") or item.get("entry") or 0)
        size = float(pos.get("size") or item.get("size") or 0)
        direction = str(pos.get("direction") or item.get("direction") or "BUY").upper()
    except (TypeError, ValueError):
        return None
    if entry <= 0 or size <= 0:
        return None
    bid = float(mkt.get("bid") or item.get("bid") or 0)
    offer = float(mkt.get("offer") or mkt.get("ask") or item.get("offer") or 0)
    if bid <= 0 or offer <= 0:
        try:
            from system.market_data_hub import get_market_data_hub

            snap = get_market_data_hub().get_snapshot(epic)
            if snap is not None and float(snap.bid or 0) > 0 and float(snap.offer or 0) > 0:
                bid, offer = float(snap.bid), float(snap.offer)
        except Exception:
            return None
    if bid <= 0 or offer <= 0:
        return None
    try:
        from execution.position_pnl_gbp import pnl_gbp_for_open_row

        return pnl_gbp_for_open_row(
            epic=epic,
            direction=direction,
            entry_level=entry,
            size=size,
            bid=bid,
            offer=offer,
            currency=str(mkt.get("currency") or "GBP"),
        )
    except Exception:
        return None


def resolve_actionable_upl_gbp(
    item: dict[str, Any],
    *,
    cfg: Any | None = None,
) -> tuple[float | None, str]:
    """Return (upl_gbp, source) — never invent a floor breach from missing data."""
    opts = _floor_cfg(cfg)
    raw = raw_broker_upl_gbp_from_ig_item(item)
    if raw is not None:
        return float(raw), "broker_upl"
    if opts.get("allow_quote_derived_pnl", True):
        derived = quote_derived_pnl_gbp_from_ig_item(item)
        if derived is not None:
            return float(derived), "quote_derived"
    return None, "unavailable"


def evaluate_hard_floor_for_item(
    item: dict[str, Any],
    *,
    cfg: Any | None = None,
) -> tuple[bool, str, float | None]:
    """
    Return (should_flatten, reason, upl_gbp).

    Missing UPL: try quote-derived PnL. Still missing → do **not** flatten
    unless ``null_upl_fail_safe`` is explicitly True (legacy; default False).
    """
    opts = _floor_cfg(cfg)
    if not opts["enabled"]:
        return False, "hard_floor_off", None

    floor = float(opts["floor_gbp"])
    pos = item.get("position") or item
    deal_id = str(
        (pos.get("dealId") if isinstance(pos, dict) else None)
        or item.get("deal_id")
        or item.get("dealId")
        or ""
    ).strip()

    try:
        upl, source = resolve_actionable_upl_gbp(item, cfg=cfg)
        if upl is None:
            if opts["null_upl_fail_safe"] and deal_id:
                log_engine(
                    f"broker_upl_null_fail_safe deal={deal_id[:12]} "
                    f"— legacy flatten (null_upl_fail_safe=true)"
                )
                return True, "broker_upl_null_fail_safe", None
            log_engine(
                f"broker_upl_null_no_flatten deal={deal_id[:12] or '?'} "
                f"— no broker UPL and no quote-derived PnL; skip close"
            )
            return False, "broker_upl_null_no_flatten", None
        if float(upl) <= floor:
            return (
                True,
                f"broker_upl_hard_floor {float(upl):.2f} <= {floor:.2f} ({source})",
                float(upl),
            )
        return False, f"within_floor:{source}", float(upl)
    except Exception as exc:
        if opts["exception_fail_safe"] and deal_id:
            log_engine(
                f"broker_upl_gate_exception deal={deal_id[:12]} "
                f"{type(exc).__name__}: {exc}"
            )
            return True, f"broker_upl_gate_exception:{type(exc).__name__}", None
        return False, f"broker_upl_gate_error:{type(exc).__name__}", None


def hard_floor_actions_from_ig_items(
    items: list[dict[str, Any]],
    *,
    cfg: Any | None = None,
) -> list[Any]:
    """Build ManageAction flatten list from raw IG /positions rows."""
    from execution.open_position_rules import ManageAction

    actions: list[ManageAction] = []
    seen: set[str] = set()
    for item in items or []:
        pos = item.get("position") or {}
        mkt = item.get("market") or {}
        deal_id = str(pos.get("dealId") or pos.get("dealID") or item.get("deal_id") or "").strip()
        epic = str(mkt.get("epic") or item.get("epic") or "").strip()
        if not deal_id or deal_id in seen:
            continue
        should, reason, upl = evaluate_hard_floor_for_item(item, cfg=cfg)
        if not should:
            continue
        seen.add(deal_id)
        actions.append(
            ManageAction(
                deal_id=deal_id,
                epic=epic,
                pnl_gbp=float(upl) if upl is not None else float(_floor_cfg(cfg)["floor_gbp"]),
                action="flatten",
                reason=reason,
            )
        )
    return actions


def scan_and_request_hard_floor_flattens(
    rest: Any,
    cfg: Any | None = None,
) -> dict[str, Any]:
    """Poll broker book and route hard-floor breaches through exit gate."""
    from execution.exit_execution_gate import request_flatten

    report: dict[str, Any] = {
        "checked": 0,
        "triggered": 0,
        "skipped_null_upl": 0,
        "results": [],
    }
    try:
        # Never priority-bypass GET /positions — order_priority is for mutates only.
        items = list(rest.open_positions(budget_priority=False) or [])
    except Exception as exc:
        return {
            "checked": 0,
            "triggered": 0,
            "skipped_null_upl": 0,
            "error": f"open_positions:{type(exc).__name__}:{exc}",
            "fail_safe": True,
        }

    for item in items:
        report["checked"] += 1
        pos = item.get("position") or {}
        mkt = item.get("market") or {}
        deal_id = str(pos.get("dealId") or pos.get("dealID") or "").strip()
        epic = str(mkt.get("epic") or "").strip()
        direction = str(pos.get("direction") or "BUY").upper()
        size = float(pos.get("size") or 0)
        should, reason, upl = evaluate_hard_floor_for_item(item, cfg=cfg)
        if reason == "broker_upl_null_no_flatten":
            report["skipped_null_upl"] += 1
            continue
        if not should:
            continue
        report["triggered"] += 1
        result = request_flatten(
            rest=rest,
            deal_id=deal_id,
            epic=epic,
            direction=direction,
            size=size,
            reason=reason,
            pnl_gbp=upl,
            cfg=cfg,
            source="broker_upl_hard_floor",
        )
        report["results"].append(result)
    return report
