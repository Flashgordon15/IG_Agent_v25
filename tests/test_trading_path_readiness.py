"""Trading-path readiness — badge must not greenwash dead entries."""

from __future__ import annotations

from runtime.trading_path_readiness import compute_trading_path_readiness


def _clear_new_gates(monkeypatch) -> None:
    monkeypatch.setattr(
        "system.rest_api_budget.entries_blocked_by_rest_pressure",
        lambda: (False, ""),
    )
    monkeypatch.setattr(
        "runtime.broker_snapshot.open_count_from_snapshot",
        lambda max_age_sec=300.0: 0,
    )
    monkeypatch.setattr(
        "runtime.broker_snapshot.read_snapshot",
        lambda max_age_sec=None: {"count": 0, "positions": []},
    )
    import json
    import time
    from pathlib import Path

    td = Path("/tmp/ig_agent_path_ready_ts")
    td.mkdir(parents=True, exist_ok=True)
    (td / "trade_support_status.json").write_text(
        json.dumps({"ts": time.time(), "broker_open": 0})
    )
    monkeypatch.setattr("system.paths.data_dir", lambda: td)


def test_trading_path_down_when_desk_idle_insufficient_bars(monkeypatch) -> None:
    _clear_new_gates(monkeypatch)
    monkeypatch.setattr(
        "runtime.feed_health_watchdog.entries_blocked_by_feed_health",
        lambda: False,
    )
    monkeypatch.setattr(
        "api.health_light.get_health_light_response",
        lambda: {
            "execution_loop_active": True,
            "stacked_sweep_alive": True,
            "routing_state": {"armed": 7},
            "data_feeds": {"hub": {"fresh_count": 7}},
        },
    )
    monkeypatch.setattr(
        "api.health_light.iron_cage_from_health_light_snapshot",
        lambda snap=None: {"trade_ready": True},
    )
    monkeypatch.setattr(
        "runtime.deploy_hold.is_deploy_hold_active",
        lambda: False,
    )
    monkeypatch.setattr(
        "system.paths.state_dir",
        lambda: __import__("pathlib").Path("/tmp/ig_agent_path_ready_test_missing"),
    )
    monkeypatch.setattr(
        "runtime.strategy_controller.check_execution_permission",
        lambda epic, path: type("P", (), {"allowed": True, "reason": ""})(),
    )
    monkeypatch.setattr(
        "system.config_loader.get_config",
        lambda reload=False: {
            "dual_core": {"exclude_from_hot_path": ["IX.D.NIKKEI.IFM.IP"]},
            "max_open_positions": 6,
            "max_positions_per_epic": 2,
        },
    )
    out = compute_trading_path_readiness(
        desk_idle={
            "code": "insufficient_bars",
            "label": "DOW warming — insufficient bars",
        }
    )
    assert out["trading_path_live"] is False
    assert out["primary_blocker"]["code"] == "desk_idle_insufficient_bars"
    assert "DESK TRADING DOWN" in out["badge"]


def test_trading_path_live_when_clear(monkeypatch) -> None:
    _clear_new_gates(monkeypatch)
    monkeypatch.setattr(
        "runtime.feed_health_watchdog.entries_blocked_by_feed_health",
        lambda: False,
    )
    monkeypatch.setattr(
        "api.health_light.get_health_light_response",
        lambda: {
            "execution_loop_active": True,
            "stacked_sweep_alive": True,
            "routing_state": {"armed": 7},
            "data_feeds": {"hub": {"fresh_count": 7}},
        },
    )
    monkeypatch.setattr(
        "api.health_light.iron_cage_from_health_light_snapshot",
        lambda snap=None: {"trade_ready": True},
    )
    monkeypatch.setattr(
        "system.regime_state.get_regime_state_snapshot",
        lambda: {"markets": []},
    )
    monkeypatch.setattr(
        "runtime.deploy_hold.is_deploy_hold_active",
        lambda: False,
    )
    monkeypatch.setattr(
        "system.paths.state_dir",
        lambda: __import__("pathlib").Path("/tmp/ig_agent_path_ready_test_missing"),
    )
    monkeypatch.setattr(
        "runtime.strategy_controller.check_execution_permission",
        lambda epic, path: type("P", (), {"allowed": True, "reason": ""})(),
    )
    monkeypatch.setattr(
        "system.config_loader.get_config",
        lambda reload=False: {
            "dual_core": {"exclude_from_hot_path": ["IX.D.NIKKEI.IFM.IP"]},
            "max_open_positions": 6,
            "max_positions_per_epic": 2,
        },
    )
    out = compute_trading_path_readiness(desk_idle=None)
    assert out["trading_path_live"] is True
    assert out["primary_blocker"] is None
    assert "TRADING PATH LIVE" in out["badge"]


def test_path_down_on_cap_breach(monkeypatch) -> None:
    _clear_new_gates(monkeypatch)
    monkeypatch.setattr(
        "runtime.broker_snapshot.open_count_from_snapshot",
        lambda max_age_sec=300.0: 31,
    )
    monkeypatch.setattr(
        "runtime.feed_health_watchdog.entries_blocked_by_feed_health",
        lambda: False,
    )
    monkeypatch.setattr(
        "api.health_light.iron_cage_from_health_light_snapshot",
        lambda snap=None: {"trade_ready": True},
    )
    monkeypatch.setattr(
        "api.health_light.get_health_light_response",
        lambda: {},
    )
    monkeypatch.setattr(
        "runtime.deploy_hold.is_deploy_hold_active",
        lambda: False,
    )
    monkeypatch.setattr(
        "system.paths.state_dir",
        lambda: __import__("pathlib").Path("/tmp/ig_agent_path_ready_test_missing"),
    )
    monkeypatch.setattr(
        "runtime.strategy_controller.check_execution_permission",
        lambda epic, path: type("P", (), {"allowed": True, "reason": ""})(),
    )
    monkeypatch.setattr(
        "system.config_loader.get_config",
        lambda reload=False: {
            "dual_core": {"exclude_from_hot_path": []},
            "max_open_positions": 6,
            "max_positions_per_epic": 2,
        },
    )
    out = compute_trading_path_readiness(desk_idle=None)
    assert out["trading_path_live"] is False
    codes = {b["code"] for b in out["blockers"]}
    assert "cap_breach" in codes


def test_path_down_on_trade_support_stale(monkeypatch) -> None:
    _clear_new_gates(monkeypatch)
    import json
    from pathlib import Path

    td = Path("/tmp/ig_agent_path_ready_ts_stale")
    td.mkdir(parents=True, exist_ok=True)
    (td / "trade_support_status.json").write_text(
        json.dumps({"ts": 1.0, "broker_open": 3})
    )
    monkeypatch.setattr("system.paths.data_dir", lambda: td)
    monkeypatch.setattr(
        "runtime.feed_health_watchdog.entries_blocked_by_feed_health",
        lambda: False,
    )
    monkeypatch.setattr(
        "api.health_light.iron_cage_from_health_light_snapshot",
        lambda snap=None: {"trade_ready": True},
    )
    monkeypatch.setattr(
        "api.health_light.get_health_light_response",
        lambda: {},
    )
    monkeypatch.setattr(
        "runtime.deploy_hold.is_deploy_hold_active",
        lambda: False,
    )
    monkeypatch.setattr(
        "system.paths.state_dir",
        lambda: Path("/tmp/ig_agent_path_ready_test_missing"),
    )
    monkeypatch.setattr(
        "runtime.strategy_controller.check_execution_permission",
        lambda epic, path: type("P", (), {"allowed": True, "reason": ""})(),
    )
    monkeypatch.setattr(
        "system.config_loader.get_config",
        lambda reload=False: {
            "dual_core": {"exclude_from_hot_path": []},
            "max_open_positions": 6,
            "max_positions_per_epic": 2,
        },
    )
    out = compute_trading_path_readiness(desk_idle=None)
    assert out["trading_path_live"] is False
    assert any(b["code"] == "trade_support_stale" for b in out["blockers"])


def test_sniper_alone_does_not_false_red_path(monkeypatch) -> None:
    """Sniper P is ops telemetry — dual_core micro can still be live."""
    _clear_new_gates(monkeypatch)
    monkeypatch.setattr(
        "runtime.feed_health_watchdog.entries_blocked_by_feed_health",
        lambda: False,
    )
    monkeypatch.setattr(
        "api.health_light.get_health_light_response",
        lambda: {
            "execution_loop_active": True,
            "stacked_sweep_alive": True,
            "routing_state": {"armed": 7},
            "data_feeds": {"hub": {"fresh_count": 7}},
        },
    )
    monkeypatch.setattr(
        "api.health_light.iron_cage_from_health_light_snapshot",
        lambda snap=None: {"trade_ready": True},
    )
    monkeypatch.setattr(
        "system.regime_state.get_regime_state_snapshot",
        lambda: {"markets": []},
    )
    monkeypatch.setattr(
        "runtime.deploy_hold.is_deploy_hold_active",
        lambda: False,
    )
    monkeypatch.setattr(
        "system.paths.state_dir",
        lambda: __import__("pathlib").Path("/tmp/ig_agent_path_ready_test_missing"),
    )
    monkeypatch.setattr(
        "runtime.strategy_controller.check_execution_permission",
        lambda epic, path: type("P", (), {"allowed": True, "reason": ""})(),
    )
    monkeypatch.setattr(
        "system.config_loader.get_config",
        lambda reload=False: {
            "dual_core": {"exclude_from_hot_path": ["IX.D.NIKKEI.IFM.IP"]},
            "max_open_positions": 6,
            "max_positions_per_epic": 2,
        },
    )
    out = compute_trading_path_readiness(desk_idle=None)
    assert out["trading_path_live"] is True


def test_path_down_on_offline_for_dev(monkeypatch) -> None:
    _clear_new_gates(monkeypatch)
    import json
    from pathlib import Path

    td = Path("/tmp/ig_agent_path_ready_offline")
    td.mkdir(parents=True, exist_ok=True)
    (td / "offline_for_dev.json").write_text(
        json.dumps({"active": True, "reason": "unit_test"})
    )
    monkeypatch.setattr("system.paths.state_dir", lambda: td)
    monkeypatch.setattr(
        "runtime.feed_health_watchdog.entries_blocked_by_feed_health",
        lambda: False,
    )
    monkeypatch.setattr(
        "api.health_light.iron_cage_from_health_light_snapshot",
        lambda snap=None: {"trade_ready": True},
    )
    monkeypatch.setattr(
        "api.health_light.get_health_light_response",
        lambda: {},
    )
    monkeypatch.setattr(
        "runtime.deploy_hold.is_deploy_hold_active",
        lambda: False,
    )
    monkeypatch.setattr(
        "runtime.strategy_controller.check_execution_permission",
        lambda epic, path: type("P", (), {"allowed": True, "reason": ""})(),
    )
    monkeypatch.setattr(
        "system.config_loader.get_config",
        lambda reload=False: {
            "dual_core": {"exclude_from_hot_path": []},
            "max_open_positions": 6,
            "max_positions_per_epic": 2,
        },
    )
    out = compute_trading_path_readiness(desk_idle=None)
    assert out["trading_path_live"] is False
    assert any(b["code"] == "offline_for_dev" for b in out["blockers"])
