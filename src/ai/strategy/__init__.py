"""Strategy Evolution Suite — research and proposals (§18)."""

from ai.strategy.backtest_simulator import BacktestSimulator, load_strategy_proposals
from ai.strategy.performance_reviewer import (
    FRICTION_WARN_RATIO,
    build_friction_matrix,
    force_shadow_learning_pipeline,
    friction_warning,
    process_shadow_learning_pipeline,
    simulate_shadow_outcome,
)

__all__ = [
    "BacktestSimulator",
    "FRICTION_WARN_RATIO",
    "build_friction_matrix",
    "force_shadow_learning_pipeline",
    "friction_warning",
    "load_strategy_proposals",
    "process_shadow_learning_pipeline",
    "simulate_shadow_outcome",
]
