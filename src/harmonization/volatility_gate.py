"""
Dynamic volatility-scaled threshold — reduces No Trade Paradox in low-vol regimes.
"""

from __future__ import annotations

import math
from typing import Any

# No Trade Paradox — volatility-adjusted confidence band (percent scale).
NO_TRADE_PARADOX_MIN_PCT = 75.0
NO_TRADE_PARADOX_MAX_PCT = 80.0
NO_TRADE_PARADOX_RELAX_STRENGTH = 12.0


def dynamic_confidence_floor(
    *,
    base_threshold: float,
    atr: float,
    atr_baseline: float,
    rsi: float = 50.0,
    min_floor: float = NO_TRADE_PARADOX_MIN_PCT,
    max_floor: float = NO_TRADE_PARADOX_MAX_PCT,
) -> dict[str, Any]:
    """
    Scale signal threshold down when realised vol is below baseline (execution viability).
    Never breaches iron-clad risk — sizing/stops remain fixed at transmission layer.
    """
    base = float(base_threshold)
    atr_val = max(float(atr), 1e-9)
    baseline = max(float(atr_baseline), 1e-9)
    vol_ratio = atr_val / baseline
    vol_ratio = max(0.35, min(vol_ratio, 2.5))

    if vol_ratio < 1.0:
        relax = (1.0 - vol_ratio) * NO_TRADE_PARADOX_RELAX_STRENGTH
    else:
        relax = 0.0

    rsi_center = abs(float(rsi) - 50.0) / 50.0
    rsi_bonus = max(0.0, (0.5 - rsi_center)) * 3.0

    adjusted = base - relax - rsi_bonus
    if base > max_floor:
        # Paradox band: cap restrictive 90%+ floors into 75–80% viability window.
        adjusted = max(min_floor, min(max_floor, adjusted))
    elif base >= min_floor:
        adjusted = max(min_floor, min(base, adjusted))
    else:
        # Base already permissive — vol relax may lower further (never below 40%).
        adjusted = max(40.0, min(base, adjusted))

    return {
        "base_threshold": round(base, 2),
        "adjusted_threshold": round(adjusted, 2),
        "vol_ratio": round(vol_ratio, 4),
        "relax_pts": round(relax, 2),
        "rsi_bonus_pts": round(rsi_bonus, 2),
        "viable": adjusted <= base,
    }


def audit_trade_blockers(gate_diag: dict[str, Any]) -> list[dict[str, str]]:
    """Extract per-epic block reasons from fulfillment gate diagnostics."""
    blockers: list[dict[str, str]] = []
    by_epic = gate_diag.get("by_epic") or {}
    for epic, row in sorted(by_epic.items()):
        wait = str(row.get("wait_reason") or "")
        zone = str(row.get("zone_label") or "")
        if row.get("all_passed"):
            continue
        blockers.append(
            {
                "epic": epic,
                "zone": zone,
                "reason": wait or "unknown",
            }
        )
    return blockers


def no_trade_paradox_threshold(
    threshold_pct: float,
    *,
    atr: float = 0.0,
    atr_baseline: float = 10.0,
    rsi: float = 50.0,
) -> float:
    """
    Cap and relax live entry threshold into 75–80% volatility band.

    Prevents stale 90–95% protective floors from blocking all dispatch.
    """
    tuned = dynamic_confidence_floor(
        base_threshold=float(threshold_pct),
        atr=float(atr or 1.5),
        atr_baseline=max(float(atr_baseline), 1.0),
        rsi=float(rsi or 50.0),
    )
    return float(tuned["adjusted_threshold"])


def dynamic_entry_spread_cap(
    *,
    epic: str,
    normal_spread: float,
    spread_multiplier: float,
    atr: float = 0.0,
) -> float:
    """
    Widen spread cap during live packet loss / wide broker quotes.

    Stop-loss remains fixed at iron-clad 10pt — entry buffer only.
    """
    try:
        from intelligence.matrix_backtuner import DEFAULT_EPIC_STOP

        stop_ref = float(DEFAULT_EPIC_STOP.get(epic, 10.0) or 10.0)
    except Exception:
        stop_ref = 10.0
    atr_ref = max(float(atr or 0), stop_ref * 0.05)
    buffer = max(atr_ref * 0.35, stop_ref * 0.08)
    base_cap = max(float(normal_spread), 0.5) * float(spread_multiplier)
    return base_cap + buffer
