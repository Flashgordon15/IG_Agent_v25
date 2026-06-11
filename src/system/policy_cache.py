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
