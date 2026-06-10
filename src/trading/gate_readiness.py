"""
Aggregate trade readiness from the 7 evaluation gates.

100% means every gate has passed — that is the actual requirement to arm execution.
Partial credit on continuous gates (environment fitness, signal confidence) and
cold-start bar progress reflects how close the engine is, not a separate trade target.
"""

from __future__ import annotations

from typing import Any

from trading.environment_scorer import GATE_PASS_MIN

TRADE_GATE_COUNT = 7
COLD_START_BARS_REQUIRED = 6


def _gate_name(gate: Any) -> str:
    if isinstance(gate, dict):
        return str(gate.get("name") or "")
    return str(getattr(gate, "name", "") or "")


def _gate_passed(gate: Any) -> bool:
    if isinstance(gate, dict):
        return bool(gate.get("pass"))
    return bool(getattr(gate, "passed", False))


def _gate_value(gate: Any) -> Any:
    if isinstance(gate, dict):
        return gate.get("value")
    return getattr(gate, "value", None)


def gate_contribution(
    gate: Any,
    *,
    fitness_min: float = GATE_PASS_MIN,
) -> float:
    """Per-gate progress in [0, 1]. Equal weight when aggregated across all gates."""
    if _gate_passed(gate):
        return 1.0

    name = _gate_name(gate)
    value = _gate_value(gate)

    if name == "environment_fitness" and isinstance(value, dict):
        score = value.get("score")
        if score is not None and fitness_min > 0:
            try:
                return min(max(float(score) / float(fitness_min), 0.0), 1.0)
            except (TypeError, ValueError):
                return 0.0

    if name == "signal_confidence" and isinstance(value, dict):
        conf = value.get("confidence")
        threshold = value.get("threshold")
        try:
            t = float(threshold)
            c = float(conf)
        except (TypeError, ValueError):
            return 0.0
        if t > 0:
            return min(max(c / t, 0.0), 1.0)
        return 0.0

    if name == "cold_start_gap" and isinstance(value, dict):
        if value.get("gap"):
            return 0.0
        if value.get("cold"):
            try:
                bars = int(value.get("bars") or 0)
            except (TypeError, ValueError):
                bars = 0
            return min(max(bars / float(COLD_START_BARS_REQUIRED), 0.0), 1.0)

    return 0.0


def compute_trade_readiness(
    gates: list[Any] | None,
    *,
    gate_count: int | None = None,
    fitness_min: float | None = None,
) -> dict[str, int | str]:
    """
    Average gate contributions × 100.

    remaining_pct is the gap until 100% (all gates passing), not a separate target.
    """
    if fitness_min is None:
        from trading.strictness_resolver import resolve_strictness

        fitness_min = resolve_strictness().fitness_floor
    n = gate_count if gate_count is not None else TRADE_GATE_COUNT
    if n <= 0:
        n = TRADE_GATE_COUNT

    items = list(gates or [])
    if not items:
        return {
            "pct": 0,
            "remaining_pct": 100,
            "label": "0% — 100% remaining before trade",
        }

    total = sum(gate_contribution(g, fitness_min=fitness_min) for g in items)
    ratio = total / float(n)
    pct = max(0, min(100, int(round(ratio * 100))))
    remaining = max(0, 100 - pct)
    label = f"{pct}% — {remaining}% remaining before trade"
    return {"pct": pct, "remaining_pct": remaining, "label": label}


def format_health_badge_text(badge: str, readiness: dict[str, int | str]) -> str:
    """Dashboard master health line, e.g. BLOCKED 71% — 29% remaining before trade."""
    if badge == "READY":
        return "READY 100%"
    pct = int(readiness.get("pct", 0))
    remaining = int(readiness.get("remaining_pct", max(0, 100 - pct)))
    return f"{badge} {pct}% — {remaining}% remaining before trade"
