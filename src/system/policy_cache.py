"""Invalidate cached policy / overlay reads after config reload."""

from __future__ import annotations


def invalidate_policy_caches() -> None:
    try:
        from system.learning_demo_policy import (
            reset_effective_policy_snapshot_cache,
            reset_learning_demo_policy_cache_for_tests,
        )

        reset_learning_demo_policy_cache_for_tests()
        reset_effective_policy_snapshot_cache()
    except Exception:
        pass
    try:
        from system.gate_relaxation import reset_gate_relaxation_cache_for_tests

        reset_gate_relaxation_cache_for_tests()
    except Exception:
        pass
    try:
        from system.v26_config import reset_v26_overlay_cache_for_tests

        reset_v26_overlay_cache_for_tests()
    except Exception:
        pass
    try:
        from system.protective_learning import reset_protective_learning_cache_for_tests

        reset_protective_learning_cache_for_tests()
    except Exception:
        pass
    try:
        from runtime.market_orchestrator import MarketOrchestrator

        MarketOrchestrator.hot_reload_config()
    except Exception:
        pass
    try:
        from system.paths import data_dir
        from system.portfolio_envelope import rehydrate

        flush_flag = data_dir() / "state" / "portfolio_risk_flush.flag"
        if flush_flag.is_file():
            rehydrate(concurrent_risk_gbp=0.0, daily_deployed_gbp=0.0)
            flush_flag.unlink(missing_ok=True)
            from system.engine_log import log_engine

            log_engine(
                "sector override: portfolio concurrent risk flushed to zero baseline"
            )
    except Exception:
        pass
