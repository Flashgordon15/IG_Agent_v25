"""
High-velocity multi-asset prioritisation — Day 1 Genesis five-asset matrix.

Germany 40 (DAX), Wall Street (US 30), Gold (XAU/USD), Japan 225 (Nikkei),
EUR/USD — fluid QMM routing by microstructure confidence + spread turbulence.
"""

from __future__ import annotations

from typing import Any

# Day 1 Genesis — five-asset execution matrix (EPIC → display name)
FIVE_ASSET_MATRIX: dict[str, str] = {
    "IX.D.DAX.IFM.IP": "Germany 40 (DAX)",
    "IX.D.DOW.IFM.IP": "Wall Street (US 30)",
    "CS.D.CFPGOLD.CFP.IP": "Gold (XAU/USD)",
    "IX.D.NIKKEI.IFM.IP": "Japan 225 (Nikkei)",
    "CS.D.EURUSD.CFD.IP": "EUR/USD",
}

TIER1_EQUITY_MOMENTUM = frozenset(
    {
        "IX.D.DAX.IFM.IP",
        "IX.D.DOW.IFM.IP",
    }
)

PRIORITY_ASSET_MATRIX = frozenset(FIVE_ASSET_MATRIX.keys())

_EPIC_PRIORITY_MULTIPLIER: dict[str, float] = {
    "IX.D.DAX.IFM.IP": 1.40,
    "IX.D.DOW.IFM.IP": 1.40,
    "CS.D.CFPGOLD.CFP.IP": 1.20,
    "IX.D.NIKKEI.IFM.IP": 1.15,
    "CS.D.EURUSD.CFD.IP": 1.18,
}

NON_MATRIX_RANK_PENALTY = 0.05


def epic_priority_multiplier(epic: str) -> float:
    """Static preference boost for target EPICs."""
    return float(_EPIC_PRIORITY_MULTIPLIER.get(str(epic or "").strip(), 1.0))


def is_priority_asset(epic: str) -> bool:
    return str(epic or "").strip() in PRIORITY_ASSET_MATRIX


def intelligence_router_boost(epic: str) -> tuple[float, dict[str, Any]]:
    """
    Score boost from live microstructure confidence and spread turbulence.

    Returns (multiplier, detail dict) for QMM rank fusion.
    """
    key = str(epic or "").strip()
    detail: dict[str, Any] = {"epic": key, "priority_tier": epic_priority_multiplier(key)}
    try:
        from intelligence.pipeline_bridge import get_intelligence_layer

        layer = get_intelligence_layer()
        micro = layer.microstructure_verdict(key)
        spread = layer.spread_verdict(key)
        conf = float(micro.confidence)
        turbulence = bool(spread.blocked) or float(spread.throttle_factor) >= 0.5
        detail.update(
            {
                "micro_confidence": conf,
                "micro_regime": str(micro.regime),
                "spread_z": float(spread.z_score),
                "turbulence": turbulence,
            }
        )
        if turbulence:
            return max(0.35, 1.0 - float(spread.throttle_factor)), detail
        boost = 0.75 + conf * 0.55
        try:
            from intelligence.liquidity_wave import NIKKEI_EPIC, in_tokyo_momentum_window

            if in_tokyo_momentum_window() and key == NIKKEI_EPIC:
                boost *= 1.12
                detail["tokyo_momentum_boost"] = True
        except Exception:
            pass
        boost *= epic_priority_multiplier(key)
        return max(0.5, min(2.5, boost)), detail
    except Exception as exc:
        detail["error"] = f"{type(exc).__name__}"
        return epic_priority_multiplier(key), detail


def fuse_qmm_rank_score(epic: str, base_rank: float) -> float:
    """Distribute execution priority by microstructure + spread + asset tier."""
    key = str(epic or "").strip()
    if key not in PRIORITY_ASSET_MATRIX:
        return max(0.01, float(base_rank) * NON_MATRIX_RANK_PENALTY)
    intel_boost, _detail = intelligence_router_boost(key)
    fused = float(base_rank) * intel_boost
    return max(0.01, fused)


def genesis_matrix_snapshot() -> dict[str, Any]:
    """Router telemetry — five-asset matrix labels."""
    tokyo_active = False
    try:
        from intelligence.liquidity_wave import in_tokyo_momentum_window

        tokyo_active = in_tokyo_momentum_window()
    except Exception:
        pass
    return {
        "tokyo_momentum_active": tokyo_active,
        "assets": [
            {"epic": epic, "label": label, "tier": epic_priority_multiplier(epic)}
            for epic, label in FIVE_ASSET_MATRIX.items()
        ],
    }
