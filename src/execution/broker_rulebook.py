"""
IG broker dealing-rule guardrail — last-mile stop/size compliance before REST dispatch.

Raises nimble strategy floors to live broker minimums and floors fractional lots to
legal increments. Emits [BROKER_RULE_OVERRIDE] lines for benchmark / engine log tails.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from harmonization.iron_clad_risk import (
    mandatory_limit_points_for_epic,
    mandatory_stop_points_for_epic,
)
from system.engine_log import log_engine

STRATEGY_STOP_FLOOR = mandatory_stop_points_for_epic("")
STRATEGY_LIMIT_FLOOR = mandatory_limit_points_for_epic("")


def floor_to_step(value: float, step: float) -> float:
    """Floor *value* to the nearest legal broker increment (never round up)."""
    step_f = max(float(step), 1e-9)
    return math.floor(float(value) / step_f) * step_f


def broker_min_stop_points(constraints: dict[str, Any] | None) -> float:
    if not constraints:
        return 1.0
    try:
        return max(1.0, float(constraints.get("min_stop_distance") or 1.0))
    except (TypeError, ValueError):
        return 1.0


def apply_stop_floor(
    stop_pts: float,
    limit_pts: float,
    *,
    constraints: dict[str, Any] | None,
    epic: str = "",
) -> tuple[float, float, list[str]]:
    """
    Enforce max(strategy_floor, broker_min) on stop and preserve limit ≥ stop.

    Returns (stop, limit, human-readable override notes).
    """
    strategy_stop = mandatory_stop_points_for_epic(epic)
    strategy_limit = mandatory_limit_points_for_epic(epic)
    broker_min = broker_min_stop_points(constraints)
    before_stop = float(stop_pts)
    before_limit = float(limit_pts or 0)
    stop_out = max(before_stop, strategy_stop, broker_min)
    limit_out = max(before_limit, strategy_limit, stop_out)
    notes: list[str] = []
    if abs(stop_out - before_stop) > 1e-9:
        notes.append(
            f"stop {before_stop:.1f}->{stop_out:.1f} (broker_min={broker_min:.1f})"
        )
    if abs(limit_out - before_limit) > 1e-9:
        notes.append(f"limit {before_limit:.1f}->{limit_out:.1f}")
    return stop_out, limit_out, notes


def apply_size_rulebook(
    size: float,
    *,
    constraints: dict[str, Any] | None,
    epic: str,
) -> tuple[float, list[str]]:
    """
    Floor size to broker min_deal / step grid; honour epic operational floors.

    When min_deal_size < 1.0 (index micro-lots), preserves fractional contracts.
    """
    notes: list[str] = []
    before = float(size)
    if before <= 0:
        return before, notes

    min_deal = 0.01
    if constraints:
        try:
            min_deal = max(float(constraints.get("min_deal_size") or 0.01), 0.01)
        except (TypeError, ValueError):
            min_deal = 0.01

    step = min_deal
    if constraints:
        try:
            alt = float(constraints.get("min_step_distance") or 0)
            if alt > 0:
                step = max(min_deal, alt)
        except (TypeError, ValueError):
            pass

    sized = floor_to_step(before, step)
    sized = max(sized, min_deal)

    try:
        from execution.size_floors import operational_size_floor

        op_floor = operational_size_floor(epic)
        if op_floor > 0:
            sized = max(sized, op_floor)
            sized = floor_to_step(sized, step)
            sized = max(sized, min_deal)
    except Exception:
        pass

    if abs(sized - before) > 1e-9:
        notes.append(
            f"size {before:.4f}->{sized:.2f} (min_deal={min_deal:.2f}, step={step:.2f})"
        )
    return sized, notes


def _display_name(epic: str) -> str:
    try:
        from execution.trade_risk import instrument_for_epic
        from system.config_loader import get_config

        inst = instrument_for_epic(epic, get_config().as_dict()) or {}
        name = str(inst.get("name") or "").strip()
        if name:
            return name
    except Exception:
        pass
    return str(epic or "").strip()


def log_broker_overrides(
    epic: str,
    field: str,
    before: float,
    after: float,
    *,
    reason: str,
) -> None:
    label = _display_name(epic)
    log_engine(
        f"[BROKER_RULE_OVERRIDE] Adjusted {label} {field} from {before:.1f} to {after:.1f} "
        f"for API compliance ({reason}) epic={epic}"
    )


@dataclass
class BrokerRulebookResult:
    size: float
    stop_distance: float
    limit_distance: float
    overrides: list[str] = field(default_factory=list)
    constraints: dict[str, Any] = field(default_factory=dict)
    vetoed: bool = False
    veto_reason: str = ""
    risk_budget_gbp: float = 0.0
    baseline_size: float = 0.0
    baseline_stop: float = 0.0

    @property
    def fractional_micro_lot(self) -> bool:
        try:
            return float(self.constraints.get("min_deal_size") or 1.0) < 1.0
        except (TypeError, ValueError):
            return False


def point_value_gbp_for_epic(epic: str) -> float:
    """£ per IG point for monetary risk budget (instrument config)."""
    try:
        from trading.open_position_view import point_value_gbp_for_epic as _pv

        return max(1e-9, float(_pv(str(epic or "").strip(), fallback=1.0)))
    except Exception:
        return 1.0


def monetary_risk_budget_gbp(
    size: float,
    stop_pts: float,
    *,
    epic: str,
) -> float:
    """Strategy intent liability: size × stop × £/point."""
    return max(0.0, float(size) * float(stop_pts) * point_value_gbp_for_epic(epic))


def stop_pts_for_risk_budget(
    risk_gbp: float,
    size: float,
    *,
    epic: str,
) -> float:
    """Inverse sizing — compress stop when broker forces size upward."""
    pv = point_value_gbp_for_epic(epic)
    denom = float(size) * pv
    if risk_gbp <= 0 or denom <= 0:
        return 0.0
    return float(risk_gbp) / denom


def apply_dynamic_risk_balance(
    *,
    epic: str,
    baseline_size: float,
    baseline_stop: float,
    baseline_limit: float,
    size_out: float,
    constraints: dict[str, Any] | None,
) -> tuple[float, float, list[str], bool, str]:
    """
    When broker size floor expands contracts, compress stop to preserve £ risk budget.

    Returns (stop, limit, notes, vetoed, veto_reason).
    """
    notes: list[str] = []
    risk_budget = monetary_risk_budget_gbp(
        baseline_size,
        baseline_stop,
        epic=epic,
    )
    if (
        float(size_out) <= float(baseline_size) + 1e-9
        or risk_budget <= 0
        or float(baseline_stop) <= 0
    ):
        stop_out, limit_out, stop_notes = apply_stop_floor(
            baseline_stop,
            baseline_limit,
            constraints=constraints,
            epic=epic,
        )
        notes.extend(stop_notes)
        return stop_out, limit_out, notes, False, ""

    balanced_stop = stop_pts_for_risk_budget(
        risk_budget,
        size_out,
        epic=epic,
    )
    broker_min = broker_min_stop_points(constraints)
    if balanced_stop + 1e-9 < broker_min:
        reason = (
            f"required_stop={balanced_stop:.4f}pt < broker_min={broker_min:.1f}pt "
            f"after size {baseline_size:.4f}->{size_out:.2f} "
            f"(risk_budget=£{risk_budget:.2f})"
        )
        return (
            baseline_stop,
            baseline_limit,
            notes,
            True,
            reason,
        )

    stop_out = balanced_stop
    limit_out = float(baseline_limit or 0)
    if limit_out > 0 and baseline_stop > 0:
        limit_out = max(stop_out, limit_out * (balanced_stop / baseline_stop))
    else:
        limit_out = max(limit_out, mandatory_limit_points_for_epic(epic), stop_out)

    notes.append(
        f"risk_balance stop {baseline_stop:.2f}->{stop_out:.2f} "
        f"(size {baseline_size:.2f}->{size_out:.2f}, budget=£{risk_budget:.2f})"
    )
    return stop_out, limit_out, notes, False, ""


class BrokerRulebookGuard:
    """Single choke-point for broker dealing rules at order transmission."""

    @staticmethod
    def apply(
        *,
        epic: str,
        size: float,
        stop_distance: float,
        limit_distance: float,
        rest_client: Any | None,
    ) -> BrokerRulebookResult:
        epic_key = str(epic or "").strip()
        constraints: dict[str, Any] = {}
        if rest_client is not None and hasattr(rest_client, "fetch_market_constraints"):
            try:
                raw = rest_client.fetch_market_constraints(
                    epic_key,
                    budget_priority=True,
                    max_age_seconds=60.0,
                )
                if isinstance(raw, dict):
                    constraints = dict(raw)
            except Exception as exc:
                log_engine(
                    f"broker_rulebook: constraints fetch failed epic={epic_key} "
                    f"{type(exc).__name__}: {exc}"
                )

        overrides: list[str] = []
        before_size = float(size)
        before_stop = float(stop_distance)
        before_limit = float(limit_distance or 0)
        risk_budget = monetary_risk_budget_gbp(
            before_size,
            before_stop,
            epic=epic_key,
        )

        size_out, size_notes = apply_size_rulebook(
            before_size,
            constraints=constraints,
            epic=epic_key,
        )
        for note in size_notes:
            overrides.append(note)
            log_broker_overrides(
                epic_key,
                "size",
                before_size,
                size_out,
                reason=note.split("(", 1)[-1].rstrip(")") if "(" in note else note,
            )

        stop_out, limit_out, balance_notes, vetoed, veto_detail = apply_dynamic_risk_balance(
            epic=epic_key,
            baseline_size=before_size,
            baseline_stop=before_stop,
            baseline_limit=before_limit,
            size_out=size_out,
            constraints=constraints,
        )
        for note in balance_notes:
            overrides.append(note)
            if note.startswith("risk_balance stop "):
                log_broker_overrides(
                    epic_key,
                    "stop",
                    before_stop,
                    stop_out,
                    reason=note,
                )
            elif note.startswith("stop ") or note.startswith("limit "):
                if note.startswith("stop "):
                    log_broker_overrides(
                        epic_key,
                        "stop",
                        before_stop,
                        stop_out,
                        reason=note.split("(", 1)[-1].rstrip(")") if "(" in note else note,
                    )
                elif note.startswith("limit "):
                    log_broker_overrides(
                        epic_key,
                        "limit",
                        before_limit,
                        limit_out,
                        reason=note,
                    )

        if vetoed:
            log_engine(
                "[SANDBOX_VETO] Trade rejected due to broker sizing vs risk budget mismatch "
                f"epic={epic_key} {veto_detail}"
            )
            return BrokerRulebookResult(
                size=size_out,
                stop_distance=before_stop,
                limit_distance=before_limit,
                overrides=overrides,
                constraints=constraints,
                vetoed=True,
                veto_reason=veto_detail,
                risk_budget_gbp=risk_budget,
                baseline_size=before_size,
                baseline_stop=before_stop,
            )

        return BrokerRulebookResult(
            size=size_out,
            stop_distance=stop_out,
            limit_distance=limit_out,
            overrides=overrides,
            constraints=constraints,
            risk_budget_gbp=risk_budget,
            baseline_size=before_size,
            baseline_stop=before_stop,
        )
