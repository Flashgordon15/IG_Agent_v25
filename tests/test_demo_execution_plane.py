"""Tests for demo throughput execution plane bypasses."""

from __future__ import annotations

from unittest.mock import patch

from runtime.hard_enforcement import is_path_hard_allowed, reset_hard_enforcement_for_tests
from runtime.strategy_controller import ExecutionPath, check_execution_permission, reset_strategy_controller_for_tests
from runtime.strategy_enforcement import is_path_soft_allowed, reset_strategy_enforcement_for_tests
from runtime.unified_execution import UnifiedExecutionPath, _path_allowed_by_route, reset_unified_execution_for_tests
from system.demo_execution_plane import (
    demo_micro_scalper_max_open,
    demo_throughput_active,
    demo_unlimited_daily_trades,
    demo_unlimited_open_positions,
    execution_guards_relaxed,
)


def setup_function() -> None:
    reset_strategy_controller_for_tests()
    reset_hard_enforcement_for_tests()
    reset_strategy_enforcement_for_tests()
    reset_unified_execution_for_tests()


def test_unlimited_open_positions_when_demo_throughput_configured():
    cfg = {
        "demo_throughput_mode": {
            "enabled": True,
            "unlimited_open_positions": True,
            "unlimited_daily_trades": True,
            "max_daily_trades": 0,
        }
    }
    assert demo_unlimited_open_positions(cfg) is True
    assert demo_unlimited_daily_trades(cfg) is True
    assert demo_micro_scalper_max_open(cfg) is None


def test_micro_scalper_default_cap_one_without_unlimited():
    cfg = {"demo_throughput_mode": {"enabled": True}}
    assert demo_micro_scalper_max_open(cfg) == 1


def test_execution_guards_relaxed_when_demo_throughput():
    cfg = {"demo_throughput_mode": {"enabled": True, "bypass_execution_guards": True}}
    assert demo_throughput_active(cfg) is True
    assert execution_guards_relaxed(epic="IX.D.DOW.IFM.IP", cfg=cfg) is True


def test_strategy_controller_allows_micro_under_demo_throughput():
    cfg = {"demo_throughput_mode": {"enabled": True}}
    with patch("system.config_loader.get_config", return_value=cfg):
        result = check_execution_permission("IX.D.DOW.IFM.IP", ExecutionPath.MICRO)
    assert result.allowed is True


def test_hard_enforcement_bypassed_under_demo_throughput():
    from runtime.hard_enforcement import set_hard_enforcement_decisions_for_tests

    set_hard_enforcement_decisions_for_tests(
        [
            {
                "epic": "IX.D.DOW.IFM.IP",
                "active": True,
                "hard_block_paths": ["MICRO", "PATH_B_HANDOFF"],
                "hard_allow_paths": ["PATH_A"],
                "enforcement_reason": "MOMENTUM",
            }
        ]
    )
    cfg = {"demo_throughput_mode": {"enabled": True}}
    with patch("system.config_loader.get_config", return_value=cfg):
        ok, _ = is_path_hard_allowed("IX.D.DOW.IFM.IP", ExecutionPath.MICRO)
    assert ok is True


def test_unified_route_bypass_under_demo_throughput(monkeypatch):
    from runtime.unified_execution import set_unified_execution_routes_for_tests

    monkeypatch.delenv("IG_AGENT_PYTEST", raising=False)
    set_unified_execution_routes_for_tests(
        [
            {
                "epic": "IX.D.DOW.IFM.IP",
                "execution_path": "PATH_A",
                "route_reason": "MOMENTUM",
            }
        ]
    )
    cfg = {"demo_throughput_mode": {"enabled": True}}
    with patch("system.config_loader.get_config", return_value=cfg):
        ok, _ = _path_allowed_by_route("IX.D.DOW.IFM.IP", UnifiedExecutionPath.MICRO)
    assert ok is True
