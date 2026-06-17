"""Sovereign £ risk checks — shared by risk_validation gate and pre-broker submit."""

from __future__ import annotations

from typing import Any

STAGE1_DEFAULT_RISK_CAP_GBP = 150.0
EXHAUSTION_ATR_STOP_MULTIPLIER = 2.5


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


def snapshot_atr_pts(snapshot: dict[str, Any] | None) -> float:
    """Active ATR from signal snapshot last bar."""
    if not isinstance(snapshot, dict):
        return 0.0
    last = snapshot.get("last")
    if last is None:
        return 0.0
    try:
        return float(last.get("atr", 0) if hasattr(last, "get") else last["atr"])
    except (TypeError, ValueError, KeyError):
        return 0.0


def is_exhaustion_reversion_sell(
    snapshot: dict[str, Any] | None,
    *,
    setup_key: str = "",
    direction: str = "",
) -> bool:
    """True when the entry is the RSI exhaustion structural reversion short."""
    dir_u = str(direction or "").upper()
    if dir_u and dir_u not in ("", "SELL"):
        return False
    if "exhaustion_reversion" in str(setup_key or ""):
        return True
    snap = snapshot if isinstance(snapshot, dict) else {}
    return bool(snap.get("exhaustion_triggered")) and dir_u != "BUY"


def log_atr_protect_active(*, epic: str = "", setup_key: str = "") -> None:
    try:
        from system.engine_log import log_engine

        suffix = f" epic={epic}" if epic else ""
        if setup_key:
            suffix = f"{suffix} setup={setup_key}"
        log_engine(
            "🛡️ ATR PROTECT: Volatility expansion active. "
            "Stop widened to 2.5x ATR, position size downscaled to isolate cash risk."
            f"{suffix}"
        )
    except Exception:
        pass


def apply_atr_protect_envelope(
    *,
    stop_pts: float,
    size: float,
    atr_pts: float,
    point_value_gbp: float,
    snapshot: dict[str, Any] | None = None,
    setup_key: str = "",
    direction: str = "",
    epic: str = "",
) -> tuple[float, float, dict[str, Any]]:
    """
    Stop-loss envelope — exhaustion reversion SELL widens stop to 2.5× ATR and
    downscales size so sovereign £ risk stays identical to the static stop path.
    """
    inactive: dict[str, Any] = {"atr_protect_active": False}
    if not is_exhaustion_reversion_sell(
        snapshot, setup_key=setup_key, direction=direction
    ):
        return float(stop_pts), float(size), inactive

    atr = max(0.0, float(atr_pts))
    if atr <= 0:
        atr = snapshot_atr_pts(snapshot)
    if atr <= 0:
        return float(stop_pts), float(size), inactive

    static_stop = max(1.0, float(stop_pts))
    widened_stop = max(1.0, atr * EXHAUSTION_ATR_STOP_MULTIPLIER)
    base_risk = risk_gbp(float(size), static_stop, float(point_value_gbp))
    scale = static_stop / widened_stop if widened_stop > 0 else 1.0
    scaled_size = max(0.0, float(size) * scale)

    meta: dict[str, Any] = {
        "atr_protect_active": True,
        "atr_protect_stop_before": round(static_stop, 2),
        "atr_protect_stop_after": round(widened_stop, 2),
        "atr_protect_size_before": round(float(size), 4),
        "atr_protect_size_after": round(scaled_size, 4),
        "atr_protect_base_risk_gbp": round(base_risk, 2),
        "atr_protect_atr": round(atr, 2),
        "atr_protect_multiplier": EXHAUSTION_ATR_STOP_MULTIPLIER,
    }
    log_atr_protect_active(epic=epic, setup_key=setup_key)
    return widened_stop, scaled_size, meta


def effective_risk_cap_gbp(
    cfg: Any,
    *,
    confidence: float,
    risk_band_label: str = "",
) -> float:
    """Match gate risk_validation cap logic (full cap vs probe band target)."""
    cap = resolve_risk_cap_gbp(cfg)
    if str(risk_band_label or "").lower() != "probe":
        result = cap
    else:
        try:
            from system.risk_bands import bands_enabled, probe_risk_target_gbp

            if bands_enabled():
                result = float(probe_risk_target_gbp(float(confidence)) * 1.05)
            else:
                result = 80.0
        except Exception:
            result = 80.0
    try:
        from system.protective_learning import apply_temporary_test_risk_cap_gbp

        return apply_temporary_test_risk_cap_gbp(result)
    except Exception:
        return result


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
