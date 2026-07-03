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


def loss_gbp_at_stop(
    epic: str,
    *,
    size: float,
    stop_pts: float,
) -> float:
    """GBP loss if stop_pts IG points are hit (spreadbet contract specs when available)."""
    key = str(epic or "").strip()
    sz = max(0.01, abs(float(size)))
    pts = max(0.0, float(stop_pts))
    spec = INSTRUMENT_PNL_SPEC.get(key)
    if spec:
        from trading.open_position_view import pnl_currency_amount_to_gbp

        notional = pts * sz * float(spec.get("point_value") or 1.0)
        return float(pnl_currency_amount_to_gbp(notional, str(spec.get("currency") or "USD")))
    return pts * sz * _point_value_gbp(key)


def clamp_size_for_stop_risk(
    epic: str,
    size: float,
    stop_pts: float,
    cfg: Any | None,
) -> float:
    """Downsize deal so broker-mandatory stop width respects risk_per_trade_gbp."""
    profile = _load_profile(cfg)
    loss = loss_gbp_at_stop(epic, size=size, stop_pts=stop_pts)
    if loss <= profile.risk_per_trade_gbp or loss <= 0:
        return float(size)
    pv_loss_per_unit = loss / max(0.01, float(size))
    if pv_loss_per_unit <= 0:
        return float(size)
    capped = profile.risk_per_trade_gbp / pv_loss_per_unit
    return max(0.01, min(float(size), capped))


def resolve_virtual_ceiling_pts(
    *,
    epic: str,
    broker_stop_pts: float,
    profile: MicroRiskProfile | None = None,
) -> float:
    """Tight internal ceiling — always inside broker stop, capped by micro_risk profile."""
    prof = profile or MicroRiskProfile(
        risk_per_trade_gbp=5.0,
        target_r_multiple=1.5,
        min_profit_target_pts=1.0,
        max_loss_cap_pts=4.0,
        virtual_stop_ceiling_pts=4.0,
    )
    broker = max(0.5, float(broker_stop_pts))
    ceiling = min(float(prof.virtual_stop_ceiling_pts), float(prof.max_loss_cap_pts))
    ceiling = min(ceiling, broker * 0.85)
    return max(0.5, ceiling)


# Non-GBP spreadbet contract specs (mirrors open_position_view).
INSTRUMENT_PNL_SPEC: dict[str, dict[str, float | str]] = {
    "IX.D.DOW.IFM.IP": {"point_value": 2.0, "currency": "USD"},
    "IX.D.SPTRD.IFE.IP": {"point_value": 1.0, "currency": "USD"},
    "CS.D.CFPGOLD.CFP.IP": {"point_value": 1.0, "currency": "USD"},
    "IX.D.DAX.IFM.IP": {"point_value": 1.0, "currency": "EUR"},
    "IX.D.NIKKEI.IFM.IP": {"point_value": 1.0, "currency": "JPY"},
}


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
    size_f = max(0.01, abs(float(size)))
    per_pt_gbp = loss_gbp_at_stop(epic, size=size_f, stop_pts=1.0)
    risk_pts = (
        profile.risk_per_trade_gbp / per_pt_gbp
        if per_pt_gbp > 0
        else profile.max_loss_cap_pts
    )
    sl_pts = min(profile.max_loss_cap_pts, max(0.5, risk_pts))
    tp_pts = max(profile.min_profit_target_pts, sl_pts * profile.target_r_multiple)
    # Volatility widen: high |z| → slightly wider targets (not fixed £20)
    if volatility_z is not None:
        zabs = min(3.0, abs(float(volatility_z)))
        widen = 1.0 + 0.08 * zabs
        sl_pts = min(profile.max_loss_cap_pts * widen, sl_pts * widen)
        tp_pts = max(profile.min_profit_target_pts, tp_pts * widen)
    return round(tp_pts, 3), round(sl_pts, 3), profile
