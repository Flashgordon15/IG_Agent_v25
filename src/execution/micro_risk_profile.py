"""
Configurable micro risk — TP/SL from risk_per_trade_gbp, not fixed £20 clusters.

Root cause of ±£20 Gold P&L: fixed 1.5/2.0pt stops × size 10 contracts × ~$1/pt
≈ £15–20 per scalp. This module scales distances from configured GBP risk.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MicroRiskProfile:
    risk_per_trade_gbp: float
    target_r_multiple: float
    min_profit_target_pts: float
    max_loss_cap_pts: float
    virtual_stop_ceiling_pts: float


def _load_profile(cfg: Any | None) -> MicroRiskProfile:
    block: dict[str, Any] = {}
    if cfg is not None:
        try:
            raw = getattr(cfg, "micro_risk", None)
            if isinstance(raw, dict):
                block = raw
            elif hasattr(cfg, "get"):
                block = dict(cfg.get("micro_risk") or {})
        except Exception:
            block = {}
    return MicroRiskProfile(
        risk_per_trade_gbp=float(block.get("risk_per_trade_gbp", 5.0)),
        target_r_multiple=float(block.get("target_r_multiple", 1.5)),
        min_profit_target_pts=float(block.get("min_profit_target_pts", 1.0)),
        max_loss_cap_pts=float(block.get("max_loss_cap_pts", 4.0)),
        virtual_stop_ceiling_pts=float(
            block.get("virtual_stop_ceiling_pts", block.get("max_loss_cap_pts", 4.0))
        ),
    )


def _point_value_gbp(epic: str) -> float:
    try:
        from trading.open_position_view import point_value_gbp_for_epic

        return max(0.01, float(point_value_gbp_for_epic(epic)))
    except Exception:
        return 1.0


def resolve_micro_tp_sl_for_epic(
    epic: str,
    size: float,
    cfg: Any | None,
    *,
    volatility_z: float | None = None,
) -> tuple[float, float, MicroRiskProfile]:
    """
    Compute TP/SL in IG points from GBP risk budget and deal size.

    P&L_gbp ≈ points × size × point_value_gbp
    """
    profile = _load_profile(cfg)
    pv = _point_value_gbp(epic)
    size_f = max(0.01, abs(float(size)))
    denom = size_f * pv
    risk_pts = profile.risk_per_trade_gbp / denom if denom > 0 else profile.max_loss_cap_pts
    sl_pts = min(profile.max_loss_cap_pts, max(0.5, risk_pts))
    tp_pts = max(profile.min_profit_target_pts, sl_pts * profile.target_r_multiple)
    # Volatility widen: high |z| → slightly wider targets (not fixed £20)
    if volatility_z is not None:
        zabs = min(3.0, abs(float(volatility_z)))
        widen = 1.0 + 0.08 * zabs
        sl_pts = min(profile.max_loss_cap_pts * widen, sl_pts * widen)
        tp_pts = max(profile.min_profit_target_pts, tp_pts * widen)
    return round(tp_pts, 3), round(sl_pts, 3), profile
