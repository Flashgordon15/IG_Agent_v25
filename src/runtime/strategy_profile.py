"""
Strategy profile metadata — non-invasive ownership tagging for pipeline observability.

Does not influence execution, sizing, or dispatch behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

# Extended hold threshold for SWING vs MOMENTUM (seconds)
SWING_HOLD_MIN_SEC = 3600.0


class StrategyProfile(str, Enum):
    SCALP = "SCALP"
    MOMENTUM = "MOMENTUM"
    SWING = "SWING"
    ROTATION = "ROTATION"
    UNKNOWN = "UNKNOWN"


class StrategySource(str, Enum):
    PATH_A = "PATH_A"
    MICRO = "MICRO"
    PATH_B_HANDOFF = "PATH_B_HANDOFF"
    NONE = "NONE"


@dataclass(frozen=True)
class StrategyDerivationHints:
    """Read-only context collected during pipeline health aggregation."""

    has_production_orders: bool = False
    micro_deal_pattern: bool = False
    lifecycle_full_chain: bool = False
    in_active_stack: bool = False
    z_score_pierce_active: bool = False
    has_any_activity: bool = False


def lifecycle_has_full_gate_chain(lifecycle_rows: list[dict[str, Any]]) -> bool:
    """True when lifecycle bus shows SIGNAL → VALIDATION → EXECUTION_REQUEST ok."""
    for lifecycle in lifecycle_rows:
        stages = lifecycle.get("stages") or {}
        signal_ok = (stages.get("signal") or {}).get("status") == "ok"
        validation_ok = (stages.get("validation") or {}).get("status") == "ok"
        exec_ok = (stages.get("execution_request") or {}).get("status") == "ok"
        if signal_ok and validation_ok and exec_ok:
            return True
    return False


def production_order_rows_indicate_micro(order_rows: list[dict[str, Any]]) -> bool:
    for row in order_rows:
        ref = str(row.get("deal_reference") or row.get("deal_id") or "")
        if ref.startswith("MICRO-"):
            return True
        payload = row.get("broker_payload")
        if isinstance(payload, dict):
            place = payload.get("place") or {}
            if isinstance(place, dict) and "MICRO" in str(place.get("dealReference") or ""):
                return True
    return False


def epic_z_pierce_active(epic: str) -> bool:
    """Read stacked dual-core Z-score — metadata only."""
    try:
        from runtime.dual_core_execution import PIERCE_LOWER_Z, PIERCE_UPPER_Z, get_stacked_snapshots

        snap = get_stacked_snapshots().get(epic)
        if snap is None:
            return False
        z = float(snap.volatility_z_score)
        return z <= float(PIERCE_LOWER_Z) or z >= float(PIERCE_UPPER_Z)
    except Exception:
        return False


def active_stack_epics() -> set[str]:
    try:
        from runtime.dual_core_execution import get_active_stack_epics

        return set(get_active_stack_epics())
    except Exception:
        return set()


def _parse_ts_age_sec(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        from datetime import datetime, timezone

        normalized = str(ts).replace("Z", "+00:00")
        if "T" not in normalized and " " in normalized:
            normalized = normalized.replace(" ", "T")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except (TypeError, ValueError):
        return None


def _record_has_activity(record: Any) -> bool:
    return bool(
        getattr(record, "signal_ingested", False)
        or getattr(record, "order_dispatched", False)
        or getattr(record, "order_confirmed", False)
        or getattr(record, "live_tracking", False)
        or getattr(record, "closed", False)
        or getattr(record, "reconciled", False)
    )


def derive_strategy_ownership(
    record: Any,
    hints: StrategyDerivationHints | None = None,
) -> tuple[StrategyProfile, StrategySource]:
    """
    Heuristic strategy profile + source tagging from aggregated pipeline health.

    Does not modify execution — classification only.
    """
    hints = hints or StrategyDerivationHints()
    has_activity = hints.has_any_activity or _record_has_activity(record)

    if not has_activity and not hints.in_active_stack:
        return StrategyProfile.UNKNOWN, StrategySource.NONE

    # 1 — MICRO / SCALP
    scalp_signals = (
        hints.has_production_orders
        and (
            hints.micro_deal_pattern
            or (not record.signal_ingested and record.order_confirmed)
            or (not record.signal_ingested and record.order_dispatched)
        )
    ) or (
        record.order_confirmed
        and not record.signal_ingested
        and hints.has_production_orders
    )
    if scalp_signals:
        return StrategyProfile.SCALP, StrategySource.MICRO

    # 2 — PATH A / MOMENTUM or SWING
    appetite = str(getattr(getattr(record, "ml_appetite", None), "appetite", "") or "").upper()
    trailing = getattr(record, "trailing_guards", None)
    trailing_active = bool(getattr(trailing, "active", False))

    path_a_signals = (
        hints.lifecycle_full_chain
        or record.signal_ingested
        or appetite in ("WEAK", "STRONG")
        or trailing_active
    )
    if path_a_signals:
        hold_ts = record.live_tracking_timestamp or record.order_confirmed_timestamp
        hold_age = _parse_ts_age_sec(hold_ts)
        if record.live_tracking and hold_age is not None and hold_age >= SWING_HOLD_MIN_SEC:
            return StrategyProfile.SWING, StrategySource.PATH_A
        return StrategyProfile.MOMENTUM, StrategySource.PATH_A

    # 3 — PATH B handoff / ROTATION
    rotation_signals = hints.in_active_stack or hints.z_score_pierce_active
    if rotation_signals:
        if record.order_dispatched or record.order_confirmed:
            return StrategyProfile.SCALP, StrategySource.MICRO
        return StrategyProfile.ROTATION, StrategySource.PATH_B_HANDOFF

    if hints.in_active_stack:
        return StrategyProfile.ROTATION, StrategySource.PATH_B_HANDOFF

    return StrategyProfile.UNKNOWN, StrategySource.NONE


def build_derivation_hints(
    *,
    epic: str,
    record: Any,
    lifecycle_rows: list[dict[str, Any]] | None = None,
    order_rows: list[dict[str, Any]] | None = None,
) -> StrategyDerivationHints:
    lifecycle_rows = lifecycle_rows or []
    order_rows = order_rows or []
    stack = active_stack_epics()
    return StrategyDerivationHints(
        has_production_orders=bool(order_rows),
        micro_deal_pattern=production_order_rows_indicate_micro(order_rows),
        lifecycle_full_chain=lifecycle_has_full_gate_chain(lifecycle_rows),
        in_active_stack=epic in stack,
        z_score_pierce_active=epic_z_pierce_active(epic),
        has_any_activity=_record_has_activity(record),
    )


def strategy_profile_from_row(epic_row: dict[str, Any]) -> StrategyProfile:
    raw = str(epic_row.get("active_strategy_profile") or StrategyProfile.UNKNOWN.value).upper()
    try:
        return StrategyProfile(raw)
    except ValueError:
        return StrategyProfile.UNKNOWN


def is_scalp_profile(epic_row: dict[str, Any]) -> bool:
    return strategy_profile_from_row(epic_row) is StrategyProfile.SCALP


def is_rotation_profile(epic_row: dict[str, Any]) -> bool:
    return strategy_profile_from_row(epic_row) is StrategyProfile.ROTATION


def is_path_a_profile(epic_row: dict[str, Any]) -> bool:
    return strategy_profile_from_row(epic_row) in (
        StrategyProfile.MOMENTUM,
        StrategyProfile.SWING,
    )


def effective_trailing_guards_active(epic_row: dict[str, Any]) -> bool:
    """Governance helper — SCALP treats live virtual-stop positions as guarded."""
    trailing = epic_row.get("trailing_guards") or {}
    if trailing.get("active"):
        return True
    if is_scalp_profile(epic_row) and epic_row.get("live_tracking"):
        return True
    return False
