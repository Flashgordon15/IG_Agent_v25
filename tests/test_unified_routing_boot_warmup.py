"""Boot-time unified routing cache warm-up (P0) tests."""

from __future__ import annotations

import os
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from api.gui_status import warm_unified_execution_route_cache
from runtime.app_mode import reset_app_mode_for_tests
from runtime.session_lock import lock_path_for_scope, reset_session_lock_state_for_tests, write_session_lock
from runtime.strategy_controller import ExecutionPath
from runtime.unified_execution import (
    reset_unified_execution_for_tests,
    unified_guard_micro_dispatch,
    unified_guard_path_a_execution,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    reset_app_mode_for_tests()
    reset_session_lock_state_for_tests()
    reset_unified_execution_for_tests()
    for key in ("APP_MODE", "IG_ACCOUNT_SCOPE", "IG_DATA_ROOT", "IG_TRIAGE_DB"):
        monkeypatch.delenv(key, raising=False)


def _selector(profile: str = "MOMENTUM") -> dict:
    return {
        "epic": "CS.D.EURUSD.CFD.IP",
        "recommended_profile": profile,
        "selector_confidence": 80,
    }


def _hard(*, allow: list[str], block: list[str]) -> dict:
    return {
        "epic": "CS.D.EURUSD.CFD.IP",
        "active": True,
        "hard_allow_paths": allow,
        "hard_block_paths": block,
    }


def _apply_warm_patches(stack: ExitStack) -> None:
    stack.enter_context(
        patch(
            "api.gui_status.build_trade_pipeline_health",
            return_value=[{"epic": "CS.D.EURUSD.CFD.IP"}],
        )
    )
    stack.enter_context(
        patch(
            "api.gui_status.build_pipeline_governance",
            return_value={
                "pipeline_governance": {"per_epic": []},
                "session_governance": {},
                "gui_alerts": [],
            },
        )
    )
    stack.enter_context(patch("api.gui_status.build_strategy_selector_advice", return_value=[]))
    stack.enter_context(patch("api.gui_status.build_strategy_controller_decisions", return_value=[]))
    stack.enter_context(patch("api.gui_status.build_strategy_transition_advice", return_value=[]))
    stack.enter_context(patch("api.gui_status.build_strategy_enforcement_decisions", return_value=[]))
    stack.enter_context(
        patch(
            "api.gui_status.build_hard_enforcement_decisions",
            return_value=[
                _hard(
                    allow=[ExecutionPath.PATH_A.value],
                    block=[ExecutionPath.MICRO.value],
                )
            ],
        )
    )
    stack.enter_context(
        patch(
            "api.gui_status.build_api_feed_health",
            return_value={"feeds": {"f1": {"status": "OK"}}, "ranking": {"primary": "f1"}},
        )
    )
    stack.enter_context(
        patch("api.gui_status.build_market_rotation_status", return_value={"active_markets": []})
    )
    stack.enter_context(
        patch(
            "api.gui_status.build_session_review_bundle",
            return_value={"session_review": {}, "loosening_advice": {}, "self_reflection": {}},
        )
    )
    stack.enter_context(
        patch(
            "api.gui_status.build_adaptive_thresholds",
            return_value={"threshold_adjustments": {}, "adjustment_flags": []},
        )
    )
    stack.enter_context(
        patch(
            "api.gui_status.build_strategy_performance_bundle",
            return_value={"strategy_performance_memory": {}, "strategy_weighting_advice": {}},
        )
    )
    stack.enter_context(
        patch(
            "api.gui_status.build_regime_detection_bundle",
            return_value={
                "regime_detection": [
                    {
                        "epic": "CS.D.EURUSD.CFD.IP",
                        "regime_classification": "TREND",
                        "regime_confidence": 75,
                    }
                ],
                "regime_strategy_alignment": [],
            },
        )
    )
    stack.enter_context(
        patch(
            "api.gui_status.build_regime_aware_strategy_selector",
            return_value=[_selector("MOMENTUM")],
        )
    )
    stack.enter_context(
        patch(
            "api.gui_status.build_regime_risk_envelope",
            return_value=[
                {"epic": "CS.D.EURUSD.CFD.IP", "risk_profile": "MEDIUM", "risk_confidence": 70}
            ],
        )
    )
    stack.enter_context(
        patch(
            "api.gui_status.build_regime_sizing_advice",
            return_value=[
                {
                    "epic": "CS.D.EURUSD.CFD.IP",
                    "recommended_size_factor": 0.25,
                    "sizing_confidence": 70,
                }
            ],
        )
    )
    stack.enter_context(
        patch(
            "api.gui_status.build_daily_pnl_targeting",
            return_value={"recommended_bias": {"stand_down_bias": 0.1}, "contributing_factors": {}},
        )
    )


def test_cold_cache_fail_open_before_warm():
    assert unified_guard_path_a_execution("CS.D.EURUSD.CFD.IP") is True


def test_warm_unified_execution_route_cache_populates_guards(tmp_path, monkeypatch):
    scope = "ig:WARM1"
    root = tmp_path / "production"
    root.mkdir()
    monkeypatch.setenv("APP_MODE", "DEMO")
    monkeypatch.setenv("IG_ACCOUNT_SCOPE", scope)
    monkeypatch.setenv("IG_DATA_ROOT", str(root))
    write_session_lock(
        lock_path_for_scope(scope, root),
        pid=os.getpid(),
        port=8080,
        account_scope=scope,
    )

    with ExitStack() as stack:
        _apply_warm_patches(stack)
        route_count = warm_unified_execution_route_cache()

    assert route_count == 1
    assert unified_guard_path_a_execution("CS.D.EURUSD.CFD.IP") is True
    assert unified_guard_micro_dispatch("CS.D.EURUSD.CFD.IP") is False


def test_post_ready_services_invokes_route_warmup():
    from system.boot.context import BootContext
    from system.boot import post_ready_services

    with patch.dict(os.environ, {"IG_TEST_HARNESS": "1"}):
        with patch("api.gui_status.warm_unified_execution_route_cache") as warm_api:
            post_ready_services.start_post_ready_services(BootContext(config=None, rest_client=None))
            warm_api.assert_not_called()

    with patch("system.boot.post_ready_services._harness_mode", return_value=False):
        with patch(
            "system.guard.kernel_interceptor.install_kernel_interceptor",
            side_effect=ImportError("skip"),
        ):
            with patch("system.boot.post_ready_services.threading.Thread") as thread_mock:
                post_ready_services.start_post_ready_services(
                    BootContext(config=None, rest_client=None)
                )
    names = [call.kwargs.get("name") for call in thread_mock.call_args_list]
    assert "post-ready-route-warmup" in names


def test_feed_plane_ready_skips_blocking_rotation_bootstrap():
    """Feed plane must not synchronously scan the rotation universe before stacked tracks."""
    from system.boot import post_ready_services

    cfg = MagicMock()
    cfg.get.return_value = {}
    with (
        patch("feeder.yahoo_quote_poller.start_yahoo_quote_poller") as yahoo_mock,
        patch(
            "system.unified_fulfillment_cache.start_fulfillment_cache_refresh"
        ) as cache_mock,
        patch("system.cockpit_feed_guardian_agent.start_agent_feed_guardian") as guardian_mock,
        patch("runtime.dual_core_execution.bootstrap_multi_source_rotation_stack") as bootstrap_mock,
    ):
        post_ready_services._ensure_feed_plane_ready(MagicMock(), cfg)

    yahoo_mock.assert_called_once()
    cache_mock.assert_called_once()
    guardian_mock.assert_called_once()
    bootstrap_mock.assert_not_called()
