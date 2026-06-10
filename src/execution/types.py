"""Shared execution types."""

from __future__ import annotations

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
        actual_size = float(raw.get("actual_size") or 0)
        stop_points = float(raw.get("stop_points") or 0)
        limit_points = float(raw.get("limit_points") or 0)
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
