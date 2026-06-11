"""Sovereign £ risk checks — shared by risk_validation gate and pre-broker submit."""

from __future__ import annotations

from typing import Any

STAGE1_DEFAULT_RISK_CAP_GBP = 150.0


def point_value_gbp_for_config(cfg: Any) -> float:
    try:
        return float(cfg.get("ig_point_value_gbp", 1.0))
    except (TypeError, ValueError, AttributeError):
        return 1.0


def resolve_risk_cap_gbp(cfg: Any) -> float:
    """Per-instrument or global risk_cap_gbp with legacy default."""
    try:
        cap_raw = cfg.get("risk_cap_gbp")
        if cap_raw is not None:
            return float(cap_raw)
    except (TypeError, ValueError, AttributeError):
        pass
    return STAGE1_DEFAULT_RISK_CAP_GBP


def risk_gbp(size: float, stop_pts: float, point_value_gbp: float) -> float:
    return max(0.0, float(stop_pts)) * max(0.0, float(size)) * max(0.0, float(point_value_gbp))


def effective_risk_cap_gbp(
    cfg: Any,
    *,
    confidence: float,
    risk_band_label: str = "",
) -> float:
    """Match gate risk_validation cap logic (full cap vs probe band target)."""
    cap = resolve_risk_cap_gbp(cfg)
    if str(risk_band_label or "").lower() != "probe":
        return cap
    try:
        from system.risk_bands import bands_enabled, probe_risk_target_gbp

        if bands_enabled():
            return float(probe_risk_target_gbp(float(confidence)) * 1.05)
    except Exception:
        pass
    return 80.0


def check_risk_cap(
    *,
    size: float,
    stop_pts: float,
    cfg: Any,
    confidence: float = 0.0,
    risk_band_label: str = "",
) -> tuple[bool, float, float]:
    """
    Return (ok, risk_gbp, cap_gbp).
    """
    pv = point_value_gbp_for_config(cfg)
    cap = effective_risk_cap_gbp(
        cfg, confidence=confidence, risk_band_label=risk_band_label
    )
    gbp = risk_gbp(size, stop_pts, pv)
    return gbp <= cap, gbp, cap


def integrity_gate_sourced_required() -> bool:
    """Profile B learning demo — require gate-approved economics on submit."""
    try:
        from system.learning_demo_policy import learning_demo_integrity_enabled

        return learning_demo_integrity_enabled()
    except Exception:
        return False
