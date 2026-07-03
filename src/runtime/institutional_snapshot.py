"""Aggregated institutional ops telemetry for Iron Ledger / ai_diagnostics."""

from __future__ import annotations

import time
from typing import Any


def build_institutional_matrix_snapshot() -> dict[str, Any]:
    """Writer-thread snapshot — safe to call from master ledger publish only."""
    spread_fuses: dict[str, Any] = {}
    kalman: dict[str, Any] = {}
    horizons: dict[str, Any] = {}
    hot_swap: dict[str, Any] = {}
    zero_copy: dict[str, Any] = {}
    alpha_decay: dict[str, Any] = {}
    lead_lag: dict[str, Any] = {}
    asymmetric_risk: dict[str, Any] = {}
    rls: dict[str, Any] = {}
    volume_profile: dict[str, Any] = {}

    try:
        from runtime.portfolio_exploration_engine import get_spread_fuse_snapshot

        spread_fuses = get_spread_fuse_snapshot()
    except Exception:
        pass

    try:
        from runtime.portfolio_exploration_engine import get_regime_kalman_snapshot

        kalman = get_regime_kalman_snapshot()
    except Exception:
        pass

    try:
        from trading.probability_engine import get_multi_horizon_matrix_snapshot

        horizons = get_multi_horizon_matrix_snapshot()
    except Exception:
        pass

    try:
        from system.autonomic_healer import get_hot_swap_snapshot

        hot_swap = get_hot_swap_snapshot()
    except Exception:
        pass

    try:
        from system.market_data_hub import get_zero_copy_pipeline_snapshot

        zero_copy = get_zero_copy_pipeline_snapshot()
    except Exception:
        pass

    try:
        from trading.probability_engine import get_alpha_decay_snapshot

        alpha_decay = get_alpha_decay_snapshot()
    except Exception:
        pass

    try:
        from runtime.master_orchestrator import get_lead_lag_arbitrage_snapshot

        lead_lag = get_lead_lag_arbitrage_snapshot()
    except Exception:
        pass

    try:
        from execution.risk_manager import get_asymmetric_risk_snapshot

        asymmetric_risk = get_asymmetric_risk_snapshot()
    except Exception:
        pass

    volatility_bracket: dict[str, Any] = {}
    try:
        from execution.risk_manager import get_volatility_bracket_snapshot

        volatility_bracket = get_volatility_bracket_snapshot()
    except Exception:
        pass

    try:
        from trading.probability_engine import get_rls_calibrator_snapshot

        rls = get_rls_calibrator_snapshot()
    except Exception:
        pass

    try:
        from runtime.portfolio_exploration_engine import get_volume_profile_snapshot

        volume_profile = get_volume_profile_snapshot()
    except Exception:
        pass

    return {
        "ok": True,
        "ts": time.time(),
        "spread_fuses": spread_fuses,
        "regime_kalman": kalman,
        "multi_horizon": horizons,
        "hot_swap": hot_swap,
        "zero_copy_pipeline": zero_copy,
        "alpha_decay": alpha_decay,
        "lead_lag_arbitrage": lead_lag,
        "asymmetric_risk": asymmetric_risk,
        "volatility_bracket": volatility_bracket,
        "rls_calibrator": rls,
        "volume_profile": volume_profile,
    }
