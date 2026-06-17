"""
Rocket-trigger fast path — institutional sweep / breakout detection (memory-only).

Eligible signals skip redundant validation re-runs but still pass RiskManager.assess.
"""

from __future__ import annotations

from typing import Any

SWEEP_REGIMES = frozenset({"SWEEP_BUY", "SWEEP_SELL"})
MOMENTUM_REGIMES = frozenset({"MOMENTUM_UP", "MOMENTUM_DOWN"})
MIN_CONFIDENCE = 0.85
VELOCITY_TICK_THRESHOLD = 15


def rocket_trigger_eligible(epic: str) -> tuple[bool, str]:
    key = str(epic or "").strip()
    if not key:
        return False, "missing_epic"
    try:
        from intelligence.intelligence_worker import get_intelligence_worker
        from intelligence.velocity_filter import ticks_in_window

        worker = get_intelligence_worker()
        micro = worker.micro_model.classify(key)
        regime = str(getattr(micro, "regime", "NEUTRAL"))
        conf = float(getattr(micro, "confidence", 0.0) or 0.0)
        ticks = ticks_in_window(key)
    except Exception:
        return False, "micro_unavailable"

    if regime in SWEEP_REGIMES and conf >= MIN_CONFIDENCE:
        return True, f"sweep_{regime.lower()}"
    if regime in MOMENTUM_REGIMES and ticks >= VELOCITY_TICK_THRESHOLD:
        return True, f"velocity_breakout_{ticks}ticks"
    if conf >= 0.90 and ticks >= VELOCITY_TICK_THRESHOLD:
        return True, "confidence_velocity_cross"
    return False, "nominal"


def apply_rocket_metadata(signal: Any) -> bool:
    """Tag signal snapshot in-place; returns eligibility."""
    eligible, reason = rocket_trigger_eligible(str(getattr(signal, "epic", "") or ""))
    snap = getattr(signal, "snapshot", None)
    if not isinstance(snap, dict):
        return eligible
    if eligible:
        snap["rocket_trigger"] = True
        snap["rocket_reason"] = reason
    return eligible


def weld_rocket_dispatch_params(
    execution_params: dict[str, Any],
    *,
    epic: str = "",
    micro_confidence: float = 1.0,
    config: Any | None = None,
) -> dict[str, Any]:
    """
    Rocket fast-path + standard path — iron-clad two-decimal lot before REST.

    Delegates to position_ladder broker contract; idempotent on repeated calls.
    """
    from trading.position_ladder import weld_execution_params_lot

    welded = weld_execution_params_lot(
        execution_params,
        epic=epic,
        micro_confidence=micro_confidence,
        config=config,
    )
    if welded.get("rocket_trigger") or execution_params.get("rocket_trigger"):
        welded["rocket_lot_welded"] = True
    return welded


def weld_rest_payload_map(payload: dict[str, Any], *, epic: str = "") -> dict[str, Any]:
    """Final REST payload weld — size field must satisfy two-decimal contract."""
    from trading.position_ladder import apply_broker_lot_contract

    out = dict(payload)
    if "size" in out:
        out["size"] = apply_broker_lot_contract(float(out["size"]), epic)
    return out
