"""Live reason telemetry tokens for gate diagnostics / fulfillment UI."""

from __future__ import annotations

from typing import Any

EXEC_ROUTE_OPEN = "[EXEC_ROUTE_OPEN]"
REGIME_CORR_CAP = "[REGIME_CORR_CAP]"
REGIME_LULL_VOL = "[REGIME_LULL_VOL]"
REGIME_ALPHA_DIF = "[REGIME_ALPHA_DIF]"


def resolve_gating_reason(
    *,
    epic: str,
    wait_reason: str = "",
    all_passed: bool = False,
    gates: list[dict[str, Any]] | None = None,
) -> str:
    """Map gate wait state to a single executive status code."""
    wr = str(wait_reason or "").strip()
    wr_l = wr.lower()

    if all_passed and not wr:
        return EXEC_ROUTE_OPEN

    if _is_corr_cap(wr_l, gates):
        return REGIME_CORR_CAP
    if _is_lull_vol(wr_l, gates):
        return REGIME_LULL_VOL
    if _is_alpha_drift(wr_l, gates):
        return REGIME_ALPHA_DIF

    if all_passed:
        return EXEC_ROUTE_OPEN

    return REGIME_ALPHA_DIF if wr else EXEC_ROUTE_OPEN


def _is_corr_cap(wait_l: str, gates: list[dict[str, Any]] | None) -> bool:
    if "correlation guard" in wait_l and ("max 5" in wait_l or "/5" in wait_l):
        return True
    for g in gates or []:
        name = str(g.get("name") or "").lower()
        detail = str(g.get("detail") or g.get("why_failed") or "").lower()
        if "correlation" in name or "correlation guard" in detail:
            if "max 5" in detail or "5 buy" in detail or "5 sell" in detail:
                return True
    try:
        from execution.correlation_guard import snapshot

        snap = snapshot()
        cap = int(snap.get("max") or 5)
        if int(snap.get("buy") or 0) >= cap or int(snap.get("sell") or 0) >= cap:
            return True
    except Exception:
        pass
    return False


def _is_lull_vol(wait_l: str, gates: list[dict[str, Any]] | None) -> bool:
    needles = (
        "spread",
        "atr",
        "lull",
        "iron_clad",
        "adaptive_vol",
        "spread_to_atr",
        "volatility",
        "volume_surge",
    )
    if any(n in wait_l for n in needles):
        return True
    for g in gates or []:
        name = str(g.get("name") or "").lower()
        if any(n in name for n in ("spread", "atr", "iron_clad", "volatility")):
            if not g.get("passed", True):
                return True
    return False


def _is_alpha_drift(wait_l: str, gates: list[dict[str, Any]] | None) -> bool:
    needles = (
        "drift",
        "sigma",
        "feature_drift",
        "alpha_matrix",
        "historical cell",
        "training",
        "integrity_abort",
        "order confirmation overdue",
    )
    if any(n in wait_l for n in needles):
        return True
    for g in gates or []:
        name = str(g.get("name") or "").lower()
        if name in ("alpha_matrix", "alpha_matrix_approved", "feature_drift"):
            if not g.get("passed", True):
                return True
    return bool(wait_l)
