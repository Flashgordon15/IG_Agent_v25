"""
Full-system stress tests for v31 strategy layers — synthetic sessions, consistency checks.

Advisory-only stack exercise; no execution path changes.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest

from runtime.pipeline_governance import build_pipeline_governance
from runtime.session_review import build_loosening_advice, build_self_reflection, build_session_review_bundle
from runtime.strategy_controller import build_strategy_controller_decisions, reset_strategy_controller_for_tests
from runtime.strategy_enforcement import build_strategy_enforcement_decisions, reset_strategy_enforcement_for_tests
from runtime.strategy_selector import build_strategy_selector_advice
from runtime.strategy_transition import build_strategy_transition_advice


def _ago_iso(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _epic_row(
    epic: str,
    *,
    profile: str = "UNKNOWN",
    source: str = "NONE",
    **extra: Any,
) -> dict[str, Any]:
    base = {
        "epic": epic,
        "market_name": epic,
        "pipeline_state": "IDLE",
        "active_strategy_profile": profile,
        "strategy_source": source,
        "signal_ingested": False,
        "order_prepared": False,
        "order_dispatched": False,
        "live_tracking": False,
        "ml_appetite": {"appetite": "NONE", "probability": 0.0, "reason": ""},
        "trailing_guards": {"active": False},
    }
    base.update(extra)
    return base


def _feed_ok() -> dict[str, Any]:
    return {
        "feeds": {
            "feed1": {"status": "OK", "latency_ms": 900.0, "last_update_timestamp": _ago_iso(3)},
            "feed2": {"status": "OK", "latency_ms": 1500.0, "last_update_timestamp": _ago_iso(5)},
        },
        "ranking": {"primary": "feed1"},
    }


def _feed_mixed() -> dict[str, Any]:
    return {
        "feeds": {
            "feed1": {"status": "DEGRADED", "latency_ms": 18000.0, "last_update_timestamp": _ago_iso(300)},
            "feed2": {"status": "OK", "latency_ms": 1200.0, "last_update_timestamp": _ago_iso(4)},
        },
        "ranking": {"primary": "feed2"},
    }


def _feed_degraded() -> dict[str, Any]:
    return {
        "feeds": {
            "feed1": {"status": "DEGRADED", "latency_ms": 25000.0},
            "feed2": {"status": "DEGRADED", "latency_ms": 30000.0},
        },
        "ranking": {"primary": "feed1"},
    }


def _rotation_idle() -> dict[str, Any]:
    return {"active_markets": [], "candidate_markets": [], "rotation_state": "IDLE"}


def _run_full_stack(
    *,
    pipeline_rows: list[dict[str, Any]],
    api_feed_health: dict[str, Any],
    session_governance: dict[str, Any],
    market_rotation: dict[str, Any],
    session_uptime_sec: float = 3600.0,
    volatility_z: float = 1.0,
    z_pierce: bool = False,
    trade_data: dict[str, Any] | None = None,
    drawdown: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reset_strategy_controller_for_tests()
    reset_strategy_enforcement_for_tests()

    governance = build_pipeline_governance(
        trade_pipeline_health=pipeline_rows,
        api_feed_health=api_feed_health,
        market_rotation_status=market_rotation,
    )
    pipeline_gov = governance.get("pipeline_governance") or {}

    stack = ExitStack()
    stack.enter_context(patch("runtime.strategy_selector._volatility_z", return_value=volatility_z))
    stack.enter_context(patch("runtime.strategy_transition._volatility_z", return_value=volatility_z))
    stack.enter_context(patch("runtime.session_review._volatility_z", return_value=volatility_z))
    stack.enter_context(patch("runtime.strategy_selector.epic_z_pierce_active", return_value=z_pierce))
    stack.enter_context(patch("runtime.strategy_transition.epic_z_pierce_active", return_value=z_pierce))
    if trade_data is not None:
        stack.enter_context(patch("runtime.session_review._fetch_session_trades", return_value=trade_data))
    if drawdown is not None:
        stack.enter_context(patch("runtime.session_review._drawdown_summary", return_value=drawdown))

    with stack:
        selector = build_strategy_selector_advice(
            trade_pipeline_health=pipeline_rows,
            pipeline_governance=pipeline_gov,
            api_feed_health=api_feed_health,
            market_rotation_status=market_rotation,
            session_governance=session_governance,
        )
        transition = build_strategy_transition_advice(
            trade_pipeline_health=pipeline_rows,
            pipeline_governance=pipeline_gov,
            api_feed_health=api_feed_health,
            market_rotation_status=market_rotation,
            session_governance=session_governance,
            strategy_selector_advice=selector,
        )
        controller = build_strategy_controller_decisions(
            trade_pipeline_health=pipeline_rows,
            pipeline_governance=pipeline_gov,
            strategy_selector_advice=selector,
        )
        enforcement = build_strategy_enforcement_decisions(
            trade_pipeline_health=pipeline_rows,
            pipeline_governance=pipeline_gov,
            api_feed_health=api_feed_health,
            strategy_controller_decisions=controller,
            strategy_transition_advice=transition,
            strategy_selector_advice=selector,
        )
        bundle = build_session_review_bundle(
            trade_pipeline_health=pipeline_rows,
            pipeline_governance=pipeline_gov,
            session_governance=session_governance,
            api_feed_health=api_feed_health,
            market_rotation_status=market_rotation,
            strategy_selector_advice=selector,
            strategy_transition_advice=transition,
            strategy_controller_decisions=controller,
            strategy_enforcement_decisions=enforcement,
            session_uptime_sec=session_uptime_sec,
        )

    return {
        "selector": selector,
        "transition": transition,
        "controller": controller,
        "enforcement": enforcement,
        "bundle": bundle,
    }


def _assert_layer_shapes(result: dict[str, Any]) -> None:
    selector = result["selector"]
    transition = result["transition"]
    controller = result["controller"]
    enforcement = result["enforcement"]
    bundle = result["bundle"]

    assert isinstance(selector, list)
    assert isinstance(transition, list)
    assert isinstance(controller, list)
    assert isinstance(enforcement, list)

    for row in selector:
        assert "epic" in row
        assert "recommended_strategy_profile" in row
        assert "confidence" in row
        assert 0 <= int(row["confidence"]) <= 100

    for row in transition:
        assert "current_profile" in row
        assert "target_profile" in row
        assert "transition_confidence" in row

    for row in controller:
        assert "allowed_paths" in row
        assert "blocked_paths" in row
        assert "ownership" in row

    for row in enforcement:
        assert "soft_block_paths" in row
        assert "soft_allow_paths" in row
        assert "enforcement_confidence" in row

    review = bundle["session_review"]
    assert "session_summary" in review
    assert "session_quality_score" in review
    assert "session_risk_score" in review
    assert "session_stability_score" in review
    assert "session_flags" in review

    loosening = bundle["loosening_advice"]
    assert "recommended_changes" in loosening
    assert "confidence" in loosening

    reflection = bundle["self_reflection"]
    assert "critique_summary" in reflection
    assert "weaknesses" in reflection
    assert "reflection_confidence" in reflection


@pytest.fixture(autouse=True)
def _reset_caches():
    reset_strategy_controller_for_tests()
    reset_strategy_enforcement_for_tests()
    yield
    reset_strategy_controller_for_tests()
    reset_strategy_enforcement_for_tests()


def test_scenario_high_volatility_scalp_opportunities():
    rows = [
        _epic_row(
            "CS.D.EURUSD.CFD.IP",
            profile="SCALP",
            source="MICRO",
            order_dispatched=True,
            pipeline_state="LIVE",
        ),
    ]
    result = _run_full_stack(
        pipeline_rows=rows,
        api_feed_health=_feed_ok(),
        session_governance={"overall_session_health_score": 90, "session_anomalies": []},
        market_rotation=_rotation_idle(),
        volatility_z=2.2,
        z_pierce=True,
        trade_data={
            "total_trades": 8,
            "trades_by_path": {"PATH_A": 0, "MICRO": 8, "PATH_B_HANDOFF": 0},
            "trades_by_strategy_profile": {"SCALP": 8, "MOMENTUM": 0, "SWING": 0, "ROTATION": 0, "STAND_DOWN": 0},
            "closed_pnl_gbp": [2.0, 1.5, -0.5, 3.0],
            "unrealised_pnl_gbp": [1.0],
        },
    )
    _assert_layer_shapes(result)
    sel = result["selector"][0]
    assert sel["recommended_strategy_profile"] in ("SCALP", "STAND_DOWN", "ROTATION")


def test_scenario_low_vol_momentum_swing_bias():
    rows = [
        _epic_row(
            "IX.D.DOW.IFM.IP",
            profile="MOMENTUM",
            source="PATH_A",
            signal_ingested=True,
            order_prepared=True,
            ml_appetite={"appetite": "STRONG", "probability": 0.75, "reason": "blend"},
            trailing_guards={"active": True},
            live_tracking=True,
        ),
    ]
    result = _run_full_stack(
        pipeline_rows=rows,
        api_feed_health=_feed_ok(),
        session_governance={"overall_session_health_score": 88, "session_anomalies": []},
        market_rotation=_rotation_idle(),
        volatility_z=0.7,
    )
    _assert_layer_shapes(result)
    assert result["selector"][0]["recommended_strategy_profile"] in (
        "MOMENTUM", "SWING", "STAND_DOWN", "SCALP"
    )


def test_scenario_mixed_feed_health():
    rows = [_epic_row("CS.D.EURUSD.CFD.IP", profile="ROTATION", source="PATH_B_HANDOFF")]
    result = _run_full_stack(
        pipeline_rows=rows,
        api_feed_health=_feed_mixed(),
        session_governance={"overall_session_health_score": 70, "session_anomalies": []},
        market_rotation={"active_markets": ["CS.D.EURUSD.CFD.IP"], "candidate_markets": [], "rotation_state": "EVALUATING"},
        volatility_z=1.1,
    )
    _assert_layer_shapes(result)


def test_scenario_heavy_governance_anomalies():
    rows = [
        _epic_row(
            "CS.D.EURUSD.CFD.IP",
            profile="MOMENTUM",
            source="PATH_A",
            pipeline_state="ORDER_PENDING",
            order_dispatched=True,
        ),
    ]
    gov = build_pipeline_governance(
        trade_pipeline_health=rows,
        api_feed_health=_feed_degraded(),
    )
    result = _run_full_stack(
        pipeline_rows=rows,
        api_feed_health=_feed_degraded(),
        session_governance=gov.get("session_governance") or {"overall_session_health_score": 30, "session_anomalies": ["X"]},
        market_rotation=_rotation_idle(),
        volatility_z=2.8,
        session_uptime_sec=7200.0,
        trade_data={
            "total_trades": 0,
            "trades_by_path": {"PATH_A": 0, "MICRO": 0, "PATH_B_HANDOFF": 0},
            "trades_by_strategy_profile": {"SCALP": 0, "MOMENTUM": 0, "SWING": 0, "ROTATION": 0, "STAND_DOWN": 0},
            "closed_pnl_gbp": [],
            "unrealised_pnl_gbp": [],
        },
    )
    _assert_layer_shapes(result)
    flags = set(result["bundle"]["session_review"]["session_flags"])
    assert flags & {"FEED_DEGRADED", "UNDER_TRADING", "OVER_BLOCKED", "GOVERNANCE_WEAK", "VOLATILITY_SPIKE"}


def test_scenario_drawdown_and_recovery():
    rows = [_epic_row("CS.D.CFPGOLD.CFP.IP", profile="SWING", source="PATH_A", live_tracking=True)]
    result = _run_full_stack(
        pipeline_rows=rows,
        api_feed_health=_feed_ok(),
        session_governance={"overall_session_health_score": 75, "session_anomalies": []},
        market_rotation=_rotation_idle(),
        volatility_z=1.3,
        drawdown={"max_drawdown_pct": 6.5, "max_drawdown_gbp": 12.0, "current_drawdown_pct": 2.0},
        trade_data={
            "total_trades": 3,
            "trades_by_path": {"PATH_A": 3, "MICRO": 0, "PATH_B_HANDOFF": 0},
            "trades_by_strategy_profile": {"SCALP": 0, "MOMENTUM": 0, "SWING": 3, "ROTATION": 0, "STAND_DOWN": 0},
            "closed_pnl_gbp": [-5.0, 8.0, 4.0],
            "unrealised_pnl_gbp": [2.0],
        },
    )
    _assert_layer_shapes(result)
    assert "DRAWDOWN_HIGH" in result["bundle"]["session_review"]["session_flags"]


def test_scenario_under_trading_over_blocked():
    rows = [
        _epic_row("CS.D.EURUSD.CFD.IP", profile="STAND_DOWN"),
        _epic_row("IX.D.DOW.IFM.IP", profile="STAND_DOWN"),
    ]
    result = _run_full_stack(
        pipeline_rows=rows,
        api_feed_health=_feed_ok(),
        session_governance={"overall_session_health_score": 85, "session_anomalies": []},
        market_rotation=_rotation_idle(),
        session_uptime_sec=10800.0,
        trade_data={
            "total_trades": 0,
            "trades_by_path": {"PATH_A": 0, "MICRO": 0, "PATH_B_HANDOFF": 0},
            "trades_by_strategy_profile": {"SCALP": 0, "MOMENTUM": 0, "SWING": 0, "ROTATION": 0, "STAND_DOWN": 0},
            "closed_pnl_gbp": [],
            "unrealised_pnl_gbp": [],
        },
    )
    _assert_layer_shapes(result)
    flags = set(result["bundle"]["session_review"]["session_flags"])
    assert "UNDER_TRADING" in flags
    assert "STAND_DOWN_DOMINANT" in flags
    loosening = result["bundle"]["loosening_advice"]
    assert "STAND_DOWN_SUPPRESS" in loosening.get("loosening_flags", [])


def test_scenario_aggressive_profile_switching():
    rows = [
        _epic_row("CS.D.EURUSD.CFD.IP", profile="SCALP", source="MICRO", order_confirmed=True),
        _epic_row("IX.D.DOW.IFM.IP", profile="MOMENTUM", source="PATH_A", signal_ingested=True, order_prepared=True),
    ]
    result = _run_full_stack(
        pipeline_rows=rows,
        api_feed_health=_feed_ok(),
        session_governance={"overall_session_health_score": 82, "session_anomalies": []},
        market_rotation={"active_markets": ["CS.D.EURUSD.CFD.IP"], "candidate_markets": ["IX.D.DOW.IFM.IP"], "rotation_state": "ROTATING"},
        volatility_z=1.9,
        z_pierce=True,
        trade_data={
            "total_trades": 15,
            "trades_by_path": {"PATH_A": 5, "MICRO": 10, "PATH_B_HANDOFF": 0},
            "trades_by_strategy_profile": {"SCALP": 10, "MOMENTUM": 5, "SWING": 0, "ROTATION": 0, "STAND_DOWN": 0},
            "closed_pnl_gbp": [1.0] * 10,
            "unrealised_pnl_gbp": [],
        },
    )
    _assert_layer_shapes(result)


def test_contradiction_selector_scalp_blocks_micro():
    epic = "CS.D.EURUSD.CFD.IP"
    review = {
        "session_summary": {"time_in_profile": {"STAND_DOWN": 0}, "points_summary": {}},
        "session_quality_score": 60,
        "session_stability_score": 55,
        "session_flags": [],
    }
    reflection = build_self_reflection(
        review,
        strategy_selector_advice=[
            {"epic": epic, "recommended_strategy_profile": "SCALP", "confidence": 85}
        ],
        strategy_controller_decisions=[
            {
                "epic": epic,
                "ownership": "MOMENTUM",
                "confidence": 80,
                "allowed_paths": ["PATH_A"],
                "blocked_paths": ["MICRO", "PATH_B_HANDOFF"],
            }
        ],
        strategy_enforcement_decisions=[
            {
                "epic": epic,
                "soft_block_paths": ["MICRO", "PATH_B_HANDOFF"],
                "soft_allow_paths": ["PATH_A"],
                "enforcement_confidence": 80,
                "enforcement_reason": "test",
                "enforcement_flags": [],
            }
        ],
    )
    assert "SELECTOR_ENFORCEMENT_CONFLICT" in reflection["reflection_flags"] or any(
        "MICRO" in c for c in reflection["contradictions"]
    )


def test_contradiction_transition_momentum_path_a_blocked():
    epic = "IX.D.DOW.IFM.IP"
    review = {
        "session_summary": {"time_in_profile": {}, "points_summary": {}},
        "session_quality_score": 65,
        "session_stability_score": 60,
        "session_flags": [],
    }
    reflection = build_self_reflection(
        review,
        strategy_transition_advice=[
            {
                "epic": epic,
                "current_profile": "SCALP",
                "target_profile": "MOMENTUM",
                "transition_confidence": 85,
            }
        ],
        strategy_enforcement_decisions=[
            {
                "epic": epic,
                "soft_block_paths": ["PATH_A", "MICRO"],
                "soft_allow_paths": ["PATH_B_HANDOFF"],
                "enforcement_confidence": 85,
                "enforcement_reason": "test",
                "enforcement_flags": ["HIGH_CONFIDENCE_TRANSITION"],
            }
        ],
    )
    assert "TRANSITION_ENFORCEMENT_MISALIGN" in reflection["reflection_flags"]


def test_contradiction_stand_down_vs_loosening():
    review = {
        "session_summary": {"time_in_profile": {"STAND_DOWN": 8000}, "points_summary": {}},
        "session_quality_score": 75,
        "session_risk_score": 30,
        "session_stability_score": 80,
        "session_flags": ["STAND_DOWN_DOMINANT"],
    }
    loosening = {
        "recommended_changes": ["Consider raising trade frequency caps slightly"],
        "confidence": 75,
        "reason": "test",
        "loosening_flags": ["HIGH_QUALITY_LOW_RISK"],
    }
    reflection = build_self_reflection(review, loosening_advice=loosening)
    assert "LOOSENING_STAND_DOWN_INCONSISTENCY" in reflection["reflection_flags"]


def test_loosening_suppressed_under_stand_down():
    review = {
        "session_quality_score": 80,
        "session_risk_score": 25,
        "session_flags": ["STAND_DOWN_DOMINANT"],
        "session_summary": {
            "drawdown_summary": {"max_drawdown_pct": 1.0},
            "governance_summary": {"session_anomalies": [], "epic_anomaly_count": 0},
        },
    }
    advice = build_loosening_advice(review)
    assert "STAND_DOWN_SUPPRESS" in advice["loosening_flags"]
    assert not any("frequency caps" in c for c in advice["recommended_changes"])


def test_under_trading_with_aggressive_blocks_flagged():
    review = {
        "session_summary": {
            "soft_blocks_count": 5,
            "controller_blocks_count": 4,
            "time_in_profile": {},
            "points_summary": {"closed_pnl_gbp": 10.0},
        },
        "session_quality_score": 55,
        "session_stability_score": 50,
        "session_flags": ["UNDER_TRADING", "OVER_BLOCKING_AGGRESSIVE"],
    }
    reflection = build_self_reflection(review)
    assert "UNDER_TRADING" in reflection["reflection_flags"]
    assert "OVER_BLOCKING" in reflection["reflection_flags"] or any(
        "over-block" in w.lower() for w in reflection["weaknesses"]
    )


def test_ideal_session_sanity():
    rows = [
        _epic_row(
            "CS.D.EURUSD.CFD.IP",
            profile="MOMENTUM",
            source="PATH_A",
            signal_ingested=True,
            order_prepared=True,
            ml_appetite={"appetite": "STRONG", "probability": 0.7, "reason": ""},
            trailing_guards={"active": True},
        ),
    ]
    result = _run_full_stack(
        pipeline_rows=rows,
        api_feed_health=_feed_ok(),
        session_governance={"overall_session_health_score": 92, "session_anomalies": []},
        market_rotation=_rotation_idle(),
        volatility_z=1.0,
        session_uptime_sec=3600.0,
        drawdown={"max_drawdown_pct": 1.2, "max_drawdown_gbp": 2.0, "current_drawdown_pct": 0.5},
        trade_data={
            "total_trades": 6,
            "trades_by_path": {"PATH_A": 6, "MICRO": 0, "PATH_B_HANDOFF": 0},
            "trades_by_strategy_profile": {"SCALP": 0, "MOMENTUM": 6, "SWING": 0, "ROTATION": 0, "STAND_DOWN": 0},
            "closed_pnl_gbp": [3.0, 2.5, 1.0, 4.0, 2.0, 1.5],
            "unrealised_pnl_gbp": [1.0],
        },
    )
    _assert_layer_shapes(result)
    review = result["bundle"]["session_review"]
    assert review["session_quality_score"] >= 65
    assert review["session_risk_score"] <= 50
    loosening = result["bundle"]["loosening_advice"]
    assert loosening["confidence"] >= 60
    reflection = result["bundle"]["self_reflection"]
    assert reflection["reflection_confidence"] >= 40
    assert len(reflection["weaknesses"]) <= 2
