"""
Trading-plane facade — alpha trail + institutional capital harvesting contract.

Delegates to ``intelligence.alpha_trail`` (authoritative implementation).
"""

from __future__ import annotations

from intelligence.alpha_trail import (
    ANTI_REGRET_PROFIT_PIPS,
    ANTI_REGRET_STOP_OFFSET_PIPS,
    ONE_R_LOCK_MULTIPLIER,
    PARABOLIC_LOCK_FLOOR_PCT,
    PARABOLIC_MILESTONE_PCT,
    TWO_R_PROFIT_MULTIPLIER,
    AlphaOptimisedTrailEngine,
    AlphaTrailPosition,
    apply_capital_harvest_contract,
    reset_capital_harvest_contract_for_tests,
)

__all__ = [
    "ANTI_REGRET_PROFIT_PIPS",
    "ANTI_REGRET_STOP_OFFSET_PIPS",
    "ONE_R_LOCK_MULTIPLIER",
    "PARABOLIC_LOCK_FLOOR_PCT",
    "PARABOLIC_MILESTONE_PCT",
    "TWO_R_PROFIT_MULTIPLIER",
    "AlphaOptimisedTrailEngine",
    "AlphaTrailPosition",
    "apply_capital_harvest_contract",
    "reset_capital_harvest_contract_for_tests",
]
