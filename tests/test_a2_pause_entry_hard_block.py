"""APP#1 — learning-loop Step 2: paused CFD must not submit new orders."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def cfd_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("IG_ENGINE_ORIGIN", "QUANT_SNIPER")
    monkeypatch.setenv("IG_ACCOUNT_ID", "Z6BAH4")
    monkeypatch.setenv("IG_DATA_ROOT", str(tmp_path))
    (tmp_path / "state_cfd").mkdir(parents=True, exist_ok=True)
    from api.agent_control import reset_agent_control_for_tests

    reset_agent_control_for_tests()
    yield tmp_path
    reset_agent_control_for_tests()


@pytest.fixture
def sb_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("IG_ENGINE_ORIGIN", "MACRO_SENTINEL")
    monkeypatch.setenv("IG_ACCOUNT_ID", "Z6BAH3")
    monkeypatch.setenv("IG_DATA_ROOT", str(tmp_path))
    (tmp_path / "state_sb").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state_cfd").mkdir(parents=True, exist_ok=True)
    from api.agent_control import reset_agent_control_for_tests

    reset_agent_control_for_tests()
    yield tmp_path
    reset_agent_control_for_tests()


def _write_a2_marker(root: Path, *, active: bool = True, mode: str = "A2_SB_ONLY") -> Path:
    path = root / "state_cfd" / "a2_entries_paused.json"
    path.write_text(
        json.dumps(
            {
                "active": active,
                "mode": mode,
                "date": "2026-07-24",
                "reason": "learning_loop_step2_cfd_a2_paused",
            }
        ),
        encoding="utf-8",
    )
    return path


def test_process_pause_blocks_new_entries(cfd_env):
    from api import agent_control

    with agent_control._lock:
        agent_control._paused = True
    blocked, reason = agent_control.new_entries_hard_blocked()
    assert blocked is True
    assert reason == "api_trading_paused"


def test_a2_marker_blocks_cfd_even_when_process_unpaused(cfd_env):
    from api import agent_control

    _write_a2_marker(cfd_env, active=True)
    with agent_control._lock:
        agent_control._paused = False
    blocked, reason = agent_control.new_entries_hard_blocked()
    assert blocked is True
    assert reason == "a2_entries_paused"


def test_a2_marker_does_not_block_sb_when_unpaused(sb_env):
    from api import agent_control

    _write_a2_marker(sb_env, active=True)
    with agent_control._lock:
        agent_control._paused = False
    blocked, reason = agent_control.new_entries_hard_blocked()
    assert blocked is False
    assert reason == ""


def test_engage_pause_from_a2_marker_on_cfd(cfd_env):
    from api import agent_control

    _write_a2_marker(cfd_env, active=True)
    with agent_control._lock:
        agent_control._paused = False
    out = agent_control.engage_pause_from_a2_marker_if_needed()
    assert out["action"] == "paused_from_a2_marker"
    assert agent_control.is_paused() is True


def test_is_api_trading_paused_includes_a2_marker(cfd_env):
    from api import agent_control
    from runtime.dual_core_execution import is_api_trading_paused

    _write_a2_marker(cfd_env, active=True)
    with agent_control._lock:
        agent_control._paused = False
    assert is_api_trading_paused() is True


def test_place_market_order_rejects_when_a2_paused(cfd_env):
    from api import agent_control
    from ig_api.exceptions import IGOrderError
    from ig_api.rest_client import IGRestClient

    _write_a2_marker(cfd_env, active=True)
    with agent_control._lock:
        agent_control._paused = False

    client = IGRestClient.__new__(IGRestClient)
    client.account_id = "Z6BAH4"
    with pytest.raises(IGOrderError) as exc:
        client.place_market_order(
            epic="IX.D.DOW.IFM.IP",
            direction="BUY",
            size=1.0,
            stop_distance=20.0,
        )
    assert "a2_entries_paused" in str(exc.value)


def test_place_market_order_rejects_when_process_paused(cfd_env):
    from api import agent_control
    from ig_api.exceptions import IGOrderError
    from ig_api.rest_client import IGRestClient

    with agent_control._lock:
        agent_control._paused = True

    client = IGRestClient.__new__(IGRestClient)
    client.account_id = "Z6BAH4"
    with pytest.raises(IGOrderError) as exc:
        client.place_market_order(
            epic="IX.D.DOW.IFM.IP",
            direction="SELL",
            size=1.0,
            stop_distance=20.0,
        )
    assert "api_trading_paused" in str(exc.value)


def test_place_otc_market_payload_rejects_when_paused(cfd_env):
    from api import agent_control
    from ig_api.exceptions import IGOrderError
    from ig_api.rest_client import IGRestClient

    with agent_control._lock:
        agent_control._paused = True

    client = IGRestClient.__new__(IGRestClient)
    client.account_id = "Z6BAH4"
    with pytest.raises(IGOrderError) as exc:
        client.place_otc_market_payload(
            {
                "epic": "IX.D.DOW.IFM.IP",
                "direction": "BUY",
                "size": 1,
                "orderType": "MARKET",
                "maxSlippage": 5,
            }
        )
    assert "api_trading_paused" in str(exc.value)


def test_inactive_a2_marker_allows_unpaused_cfd(cfd_env):
    from api import agent_control

    _write_a2_marker(cfd_env, active=False, mode="CLEARED")
    with agent_control._lock:
        agent_control._paused = False
    blocked, reason = agent_control.new_entries_hard_blocked()
    assert blocked is False
    assert reason == ""


def test_pause_check_exception_fail_closed(cfd_env):
    from api import agent_control

    with patch.object(agent_control, "is_paused", side_effect=RuntimeError("boom")):
        blocked, reason = agent_control.new_entries_hard_blocked()
    assert blocked is True
    assert "fail_closed" in reason
