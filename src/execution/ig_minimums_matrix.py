"""
IG minimum deal matrix — tradeability + sizing by epic and market session state.

Used by audit scripts and tests to mimic broker rules including time-of-day
market_status (TRADEABLE vs EDITS_ONLY vs CLOSED).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

TRADEABLE_STATUSES = frozenset({"TRADEABLE", "OPEN"})
SESSION_BLOCK_STATUSES = frozenset({"EDITS_ONLY", "CLOSED", "SUSPENDED", "OFFLINE"})


@dataclass(frozen=True)
class MinimumsVerdict:
    epic: str
    wire_epic: str
    market_status: str
    ig_min_deal: float
    hard_min_deal: float
    effective_min_deal: float
    canary_lot: float
    guard_size: float
    transmit_allowed: bool
    transmit_reason: str
    trade_possible: bool
    block_reason: str
    session_note: str


def session_note_for_status(status: str) -> str:
    """Human note tying IG market_status to time-of-day behaviour."""
    key = str(status or "").upper()
    if key in TRADEABLE_STATUSES:
        return "session_open_new_deals_ok"
    if key == "EDITS_ONLY":
        return "session_edits_only_no_new_deals_typical_rollover_or_maintenance"
    if key == "CLOSED":
        return "session_closed_no_deals"
    if key == "SUSPENDED":
        return "session_suspended_broker_halt"
    if not key:
        return "session_status_unknown_fail_closed"
    return f"session_status_{key.lower()}"


def trade_possible(
    *,
    market_status: str,
    transmit_allowed: bool,
    ig_min_deal: float,
    guard_size: float,
    constraints_ok: bool,
) -> tuple[bool, str]:
    """
    Whether a new deal is possible right now.

    Time-of-day is encoded in market_status from IG REST.
    """
    status = str(market_status or "").upper()
    if not constraints_ok:
        return False, "constraints_unavailable"
    if status not in TRADEABLE_STATUSES:
        return False, f"market_not_tradeable:{status or 'UNKNOWN'}"
    if not transmit_allowed:
        return False, "transmit_guard_blocked"
    if guard_size <= 0:
        return False, "invalid_guard_size"
    if ig_min_deal > 0 and guard_size < ig_min_deal:
        return False, f"below_ig_min:{guard_size}<{ig_min_deal}"
    return True, ""


def evaluate_epic_minimums(
    epic: str,
    *,
    cfg: Any | None,
    rest_client: Any | None,
    wire_epic: str | None = None,
    direction: str = "BUY",
    probe_size: float = 0.1,
) -> MinimumsVerdict:
    """Live or mocked evaluation of one epic against IG minimums + internal guards."""
    from execution.broker_epic_resolver import resolve_account_product, resolve_order_epic
    from execution.order_transmit_guard import guard_order_transmit
    from execution.size_floors import effective_min_deal_size, hard_min_deal_size
    from runtime.dual_core_execution import canary_lot_size

    key = str(epic or "").strip()
    product = resolve_account_product(rest=rest_client, cfg=cfg) if rest_client else "CFD"
    wire = str(wire_epic or resolve_order_epic(key, account_product=product)).strip()

    market_status = ""
    ig_min = 0.0
    constraints_ok = False

    if rest_client is not None and wire:
        try:
            data = rest_client.fetch_market_constraints(wire, budget_priority=True)
            market_status = str((data or {}).get("market_status") or "").upper()
            ig_min = max(0.0, float((data or {}).get("min_deal_size") or 0.0))
            constraints_ok = True
        except Exception:
            constraints_ok = False

    hard = hard_min_deal_size(key, cfg=cfg)
    effective = effective_min_deal_size(key, cfg=cfg, rest_min=ig_min)
    canary = float(canary_lot_size(key, cfg))

    allowed, guard_size, transmit_reason = guard_order_transmit(
        epic=key,
        direction=direction,
        size=probe_size,
        rest_client=rest_client,
        cfg=cfg,
        check_traffic_slot=False,
    )

    possible, block = trade_possible(
        market_status=market_status,
        transmit_allowed=allowed,
        ig_min_deal=effective,
        guard_size=guard_size,
        constraints_ok=constraints_ok,
    )

    return MinimumsVerdict(
        epic=key,
        wire_epic=wire,
        market_status=market_status,
        ig_min_deal=ig_min,
        hard_min_deal=hard,
        effective_min_deal=effective,
        canary_lot=canary,
        guard_size=guard_size,
        transmit_allowed=allowed,
        transmit_reason=transmit_reason,
        trade_possible=possible,
        block_reason=block,
        session_note=session_note_for_status(market_status),
    )
