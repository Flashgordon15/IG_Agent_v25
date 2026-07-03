"""Phase 4 portfolio synthesis telemetry for Iron Ledger / ai_diagnostics."""

from __future__ import annotations

import time
from typing import Any


def build_portfolio_synthesis_snapshot() -> dict[str, Any]:
    """Writer-thread aggregate — covariance, equilibrium weights, drawdown fuse, heat-map."""
    covariance: dict[str, Any] = {}
    equilibrium: dict[str, Any] = {}
    heatmap: dict[str, Any] = {}
    drawdown_fuse: dict[str, Any] = {}
    guardian: dict[str, Any] = {}
    news_alpha: dict[str, Any] = {}

    try:
        from runtime.portfolio_exploration_engine import get_portfolio_covariance_snapshot

        covariance = get_portfolio_covariance_snapshot()
    except Exception:
        pass

    try:
        from execution.risk_manager import get_equilibrium_risk_snapshot

        equilibrium = get_equilibrium_risk_snapshot()
    except Exception:
        pass

    try:
        from trading.probability_engine import build_cognitive_risk_heatmap

        heatmap = build_cognitive_risk_heatmap()
    except Exception:
        pass

    try:
        from execution.risk_manager import get_equity_curve_trailing_fuse_snapshot

        drawdown_fuse = get_equity_curve_trailing_fuse_snapshot()
    except Exception:
        pass

    try:
        from system.chaos_guardian import get_portfolio_synthesis_guard_snapshot

        guardian = get_portfolio_synthesis_guard_snapshot()
    except Exception:
        pass

    try:
        from trading.probability_engine import get_news_alpha_telemetry_snapshot
        from system.market_data_hub import get_external_api_health_matrix, get_headline_urgency_snapshot

        news_alpha = {
            "telemetry": get_news_alpha_telemetry_snapshot(),
            "headlines": get_headline_urgency_snapshot(),
            "api_ingest": get_external_api_health_matrix(),
        }
    except Exception:
        pass

    return {
        "ok": True,
        "ts": time.time(),
        "covariance": covariance,
        "equilibrium_allocation": equilibrium,
        "cognitive_risk_heatmap": heatmap,
        "drawdown_fuse": drawdown_fuse,
        "chaos_guardian": guardian,
        "news_alpha": news_alpha,
    }
