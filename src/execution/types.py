"""Shared execution types."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from data.models import Quote


class ExecutionMode(str, Enum):
    """TEST = internal simulator; DEMO/LIVE = real IG REST + LiveExecutor."""

    TEST = "TEST"
    DEMO = "DEMO"
    LIVE = "LIVE"

    def uses_simulator(self) -> bool:
        return self == ExecutionMode.TEST

    def uses_broker(self) -> bool:
        return self in (ExecutionMode.DEMO, ExecutionMode.LIVE)


@dataclass
class TradeSignal:
    market: str
    epic: str
    direction: str
    raw_confidence: float
    adjusted_confidence: float
    setup_key: str
    quote: Quote
    snapshot: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    gate_execution_params: dict[str, Any] | None = None


def normalize_gate_execution_params(
    raw: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """
    Immutable, float-cast gate sizing payload for order submission.

    Returns None when required fields are missing or non-numeric.
    """
    if not raw or not isinstance(raw, dict):
        return None
    try:
        from harmonization.iron_clad_risk import (
            MANDATORY_LIMIT_POINTS,
            MANDATORY_STOP_POINTS,
            MAX_ORDER_SIZE,
        )

        actual_size = min(
            float(raw.get("actual_size") or raw.get("size") or 0),
            MAX_ORDER_SIZE,
        )
        stop_points = max(
            float(raw.get("stop_points") or 0),
            MANDATORY_STOP_POINTS,
        )
        limit_points = max(
            float(raw.get("limit_points") or 0),
            MANDATORY_LIMIT_POINTS,
            stop_points,
        )
        risk_gbp_raw = raw.get("risk_gbp")
        risk_gbp = (
            float(risk_gbp_raw)
            if risk_gbp_raw is not None and str(risk_gbp_raw).strip() != ""
            else None
        )
    except (TypeError, ValueError):
        return None
    if actual_size <= 0 or stop_points <= 0:
        return None
    out: dict[str, Any] = {
        "actual_size": actual_size,
        "stop_points": stop_points,
        "limit_points": limit_points,
        "stop_source": raw.get("stop_source"),
        "gate_sourced": True,
    }
    if risk_gbp is not None:
        out["risk_gbp"] = risk_gbp
    for optional in ("risk_band", "risk_cap_gbp", "sizing_confidence"):
        if raw.get(optional) is not None:
            out[optional] = raw.get(optional)
    for optional in (
        "qmm_horizon",
        "qmm_trailing_distance_points",
        "qmm_trailing_trigger_points",
        "qmm_breakeven_trigger_points",
        "qmm_news_flow_sensitive",
        "qmm_horizon_confidence",
        "qmm_horizon_notes",
    ):
        if raw.get(optional) is not None:
            out[optional] = raw.get(optional)
    return out


def force_inject_gate_execution_params(
    *,
    epic: str,
    size: float,
    gate_execution_params: dict[str, Any] | None = None,
    stop_points: float | None = None,
    limit_points: float | None = None,
) -> dict[str, Any]:
    """
    Physical schema injection — every order block carries explicit gate sizing.

    Iron-clad floors: max 1 lot, 10pt stop, 20pt limit. Never returns None.
    """
    from harmonization.iron_clad_risk import (
        MANDATORY_LIMIT_POINTS,
        MANDATORY_STOP_POINTS,
        MAX_ORDER_SIZE,
    )

    raw = dict(gate_execution_params or {})
    lot = min(
        max(float(raw.get("actual_size") or raw.get("size") or size or 0.1), 0.01),
        MAX_ORDER_SIZE,
    )
    stop = max(float(stop_points or raw.get("stop_points") or MANDATORY_STOP_POINTS), MANDATORY_STOP_POINTS)
    limit = max(
        float(limit_points or raw.get("limit_points") or MANDATORY_LIMIT_POINTS),
        MANDATORY_LIMIT_POINTS,
        stop,
    )
    merged: dict[str, Any] = {
        **raw,
        "gate_sourced": True,
        "actual_size": lot,
        "size": lot,
        "final_size": int(lot),
        "stop_points": stop,
        "limit_points": limit,
        "stop_source": str(raw.get("stop_source") or "force_inject_order_builder"),
        "epic": str(epic or raw.get("epic") or ""),
    }
    normalized = normalize_gate_execution_params(merged)
    if normalized is not None:
        return normalized
    return merged


@dataclass(frozen=True)
class FrozenGateExecutionParams:
    """Immutable gate-approved sizing snapshot for cross-thread handoff."""

    actual_size: float
    stop_points: float
    limit_points: float
    gate_sourced: bool = True
    stop_source: Any = None
    risk_gbp: float | None = None
    risk_band: Any = None
    risk_cap_gbp: Any = None
    sizing_confidence: Any = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "actual_size": self.actual_size,
            "stop_points": self.stop_points,
            "limit_points": self.limit_points,
            "gate_sourced": self.gate_sourced,
        }
        if self.stop_source is not None:
            out["stop_source"] = self.stop_source
        if self.risk_gbp is not None:
            out["risk_gbp"] = self.risk_gbp
        if self.risk_band is not None:
            out["risk_band"] = self.risk_band
        if self.risk_cap_gbp is not None:
            out["risk_cap_gbp"] = self.risk_cap_gbp
        if self.sizing_confidence is not None:
            out["sizing_confidence"] = self.sizing_confidence
        return copy.deepcopy(out)


def freeze_gate_execution_params(
    raw: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """
    Normalize, deep-copy, and freeze gate sizing for execution handoff.

    Returns a fresh dict detached from the live GateResult.value payload so
    concurrent loops cannot mutate sizing mid-flight.
    """
    normalized = normalize_gate_execution_params(raw)
    if normalized is None:
        return None
    frozen = FrozenGateExecutionParams(
        actual_size=float(normalized["actual_size"]),
        stop_points=float(normalized["stop_points"]),
        limit_points=float(normalized["limit_points"]),
        gate_sourced=bool(normalized.get("gate_sourced", True)),
        stop_source=normalized.get("stop_source"),
        risk_gbp=(
            float(normalized["risk_gbp"])
            if normalized.get("risk_gbp") is not None
            else None
        ),
        risk_band=normalized.get("risk_band"),
        risk_cap_gbp=normalized.get("risk_cap_gbp"),
        sizing_confidence=normalized.get("sizing_confidence"),
    )
    out = frozen.to_dict()
    for optional in (
        "qmm_horizon",
        "qmm_trailing_distance_points",
        "qmm_trailing_trigger_points",
        "qmm_breakeven_trigger_points",
        "qmm_news_flow_sensitive",
        "qmm_horizon_confidence",
        "qmm_horizon_notes",
    ):
        if normalized.get(optional) is not None:
            out[optional] = normalized.get(optional)
    return out


@dataclass
class ExecutionResult:
    success: bool
    action: str
    deal_reference: str | None = None
    deal_id: str | None = None
    rejection_reason: str | None = None
    execution_params: dict[str, Any] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)
