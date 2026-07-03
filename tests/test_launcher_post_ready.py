"""Stage 6 post-ready acceptance — launcher + desktop splash."""

from __future__ import annotations

from cockpit.launcher_post_ready import (
    launcher_stage_visual_state,
    post_ready_execution_acceptable,
)


def test_post_ready_strict_execution_loop():
    ok, reason = post_ready_execution_acceptable(
        health_light={
            "stacked_sweep_alive": True,
            "execution_loop_active": True,
            "routing_state": {"armed": 3},
        },
        boot_tier="green",
    )
    assert ok is True
    assert reason == "execution_loop_active"


def test_post_ready_trade_ready_boot_status():
    ok, reason = post_ready_execution_acceptable(
        health_light={"agent_online": True},
        boot_status={"trade_ready": True},
        boot_tier="amber",
    )
    assert ok is True
    assert reason == "trade_ready"


def test_post_ready_routes_armed():
    ok, reason = post_ready_execution_acceptable(
        health_light={"routing_state": {"armed": 2}},
        boot_tier="amber",
    )
    assert ok is True
    assert "routes_armed" in reason


def test_post_ready_amber_api_live_after_g5():
    ok, reason = post_ready_execution_acceptable(
        health_light={
            "agent_online": True,
            "stacked_sweep_alive": False,
            "execution_loop_active": False,
            "routing_state": {"armed": 0},
        },
        boot_status={"phase": "G5"},
        boot_tier="amber",
    )
    assert ok is True
    assert reason == "amber_post_g5"


def test_post_ready_amber_zero_metrics_still_passes():
    """Desktop G5 amber — all-zero health_light must not block Stage 6 for 90s."""
    ok, reason = post_ready_execution_acceptable(
        health_light={
            "agent_online": True,
            "stacked_sweep_alive": False,
            "execution_loop_active": False,
            "routing_state": {"armed": 0, "degraded": False},
        },
        boot_status={},
        boot_tier="amber",
    )
    assert ok is True
    assert reason == "amber_api_live"


def test_post_ready_pending_on_red_tier():
    ok, reason = post_ready_execution_acceptable(
        health_light={"agent_online": True},
        boot_tier="red",
    )
    assert ok is False
    assert reason == "pending"


def test_launcher_stage_visual_complete_on_status():
    assert (
        launcher_stage_visual_state(
            stage="post_ready",
            detail="Execution plane amber_api_live",
            status="Stage 6 complete",
            boot_tier="amber",
        )
        == "complete"
    )


def test_launcher_stage_visual_complete_on_detail():
    assert (
        launcher_stage_visual_state(
            stage="post_ready",
            detail="Stage 6 complete — Execution plane amber_api_live",
            boot_tier="amber",
        )
        == "complete"
    )


def test_launcher_stage_visual_warming_during_poll():
    assert (
        launcher_stage_visual_state(
            stage="post_ready",
            detail="Confirming trade-ready routing…",
            boot_tier="amber",
        )
        == "warming"
    )
