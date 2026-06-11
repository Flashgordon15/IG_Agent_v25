"""Default v26 shadow strategy stack (Stage 1 + Stage 2)."""

from __future__ import annotations

from strategies.base import StrategyPlugin
from strategies.s1_rules_v25 import S1RulesV25
from strategies.s2_momentum import S2Momentum
from strategies.s3_session_fx import S3SessionFx


def default_shadow_strategies() -> list[StrategyPlugin]:
    return [S1RulesV25(), S2Momentum(), S3SessionFx()]
