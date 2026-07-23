"""Execute flatten actions from open-position assessment via exit gate."""

from __future__ import annotations

import time
from typing import Any

from execution.exit_execution_gate import request_flatten, request_flatten_from_action
from execution.open_position_rules import ManageReport

# Spaced closes under REST pressure — ≤~2–3 DELETE/min.
_DEFAULT_CLOSE_GAP_SEC = 22.0


def _book_from_snapshot_or_rest(rest: Any) -> dict[str, dict[str, Any]]:
    """Prefer shared snapshot under coalesce; fall back to REST when allowed."""
    by_deal: dict[str, dict[str, Any]] = {}
    try:
        from runtime.broker_snapshot import ig_items_from_snapshot

        for item in ig_items_from_snapshot(max_age_sec=None) or []:
            p = item.get("position") or {}
            did = str(p.get("dealId") or p.get("dealID") or "").strip()
            if did:
                by_deal[did] = item
        if by_deal:
            return by_deal
    except Exception:
        pass
    try:
        for item in rest.open_positions(budget_priority=True) or []:
            p = item.get("position") or {}
            did = str(p.get("dealId") or p.get("dealID") or "").strip()
            if did:
                by_deal[did] = item
    except Exception:
        by_deal = {}
    return by_deal


def _book_from_report(report: ManageReport) -> dict[str, dict[str, Any]]:
    """Build a minimal IG-shaped book from assessed rows (no REST)."""
    by_deal: dict[str, dict[str, Any]] = {}
    for row in getattr(report, "positions", None) or []:
        if not isinstance(row, dict):
            continue
        did = str(row.get("deal_id") or "").strip()
        epic = str(row.get("epic") or "").strip()
        if not did:
            continue
        by_deal[did] = {
            "position": {
                "dealId": did,
                "direction": str(row.get("direction") or "BUY").upper(),
                "size": float(row.get("size") or 0),
                "level": float(row.get("entry") or 0),
            },
            "market": {"epic": epic},
        }
    for act in report.actions:
        did = str(act.deal_id or "").strip()
        if not did or did in by_deal:
            continue
        by_deal[did] = {
            "position": {
                "dealId": did,
                "direction": "BUY",
                "size": 0.0,
                "level": 0.0,
            },
            "market": {"epic": str(act.epic or "")},
        }
    return by_deal


def execute_actions(
    rest: Any,
    report: ManageReport,
    cfg: Any | None = None,
    *,
    close_gap_sec: float = _DEFAULT_CLOSE_GAP_SEC,
    max_closes: int | None = None,
) -> None:
    """Flatten breach positions through the single exit execution gate."""
    by_deal = _book_from_snapshot_or_rest(rest)
    if not by_deal:
        by_deal = _book_from_report(report)

    done = 0
    for act in report.actions:
        if act.action != "flatten":
            continue
        if max_closes is not None and done >= max_closes:
            act.error = "deferred_rest_budget"
            continue
        if act.deal_id in by_deal:
            result = request_flatten_from_action(
                rest, act, cfg=cfg, book=by_deal, source="open_position_actions"
            )
        else:
            result = request_flatten(
                rest=rest,
                deal_id=act.deal_id,
                epic=act.epic,
                direction="BUY",
                size=0.0,
                reason=act.reason,
                pnl_gbp=act.pnl_gbp,
                cfg=cfg,
                source="open_position_actions",
            )
        if result.get("ok") or result.get("already_flat"):
            act.ok = True
            by_deal.pop(act.deal_id, None)
            done += 1
        elif result.get("skipped"):
            act.error = "is_executing"
        else:
            act.error = str(result.get("error") or "flatten_failed")
        time.sleep(max(0.5, float(close_gap_sec)))


def execute_actions_bulk(rest: Any, report: ManageReport, cfg: Any) -> None:
    """Spaced per-deal flatten under REST pressure (no net-close storm).

    Bulk net-close was unreliable on this desk (no confirm + direction bugs).
    Cap-breach unwind uses confirmed DELETE closes spaced for the IG budget.
    """
    flatten = [a for a in report.actions if a.action == "flatten"]
    if not flatten:
        return
    try:
        from system.rest_api_budget import positions_poll_deferred

        under_pressure = bool(positions_poll_deferred())
    except Exception:
        under_pressure = True
    max_closes = 3 if under_pressure else None
    gap = 22.0 if under_pressure else 1.0
    execute_actions(rest, report, cfg=cfg, close_gap_sec=gap, max_closes=max_closes)
