"""Master lifecycle supervisor — end-to-end live trade verification suite."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from runtime import dual_core_execution as dce
from runtime import master_lifecycle_supervisor as mls
from runtime import master_orchestrator as mo
from runtime import portfolio_exploration_engine as pee
from system import chaos_guardian as cg


@pytest.fixture(autouse=True)
def _isolate():
    dce.reset_strategy_execution_for_tests()
    pee.reset_portfolio_exploration_for_tests()
    mo.reset_master_orchestrator_for_tests()
    cg.reset_chaos_guardian_for_tests()
    try:
        from system.alert_reporting_matrix import reset_alert_reporting_for_tests

        reset_alert_reporting_for_tests()
    except Exception:
        pass
    yield
    dce.reset_strategy_execution_for_tests()
    pee.reset_portfolio_exploration_for_tests()
    mo.reset_master_orchestrator_for_tests()
    cg.reset_chaos_guardian_for_tests()


def _ranking(epic: str, score: float) -> dict:
    return {
        "epic": epic,
        "score": score,
        "confidence": score,
        "regime_state": 0,
        "profit_factor": 1.0,
    }


def _microkernel_high_ofi() -> dict:
    return {
        "score_pct": 72.0,
        "promote_tier": "high",
        "direction": "BUY",
        "order_flow_aligned": True,
        "forecast_confidence": 0.88,
    }


def test_dynamic_correlation_threshold_high_conviction():
    assert pee.dynamic_correlation_threshold(0.45) == pee.CORRELATION_THRESHOLD
    assert pee.dynamic_correlation_threshold(0.72) == pee.CORRELATION_THRESHOLD_HIGH_CONVICTION


def test_correlation_guard_widens_above_060_expectation():
    rets = np.linspace(0.001, 0.002, 288)
    pee.inject_exploration_rankings_for_tests(
        [_ranking("CS.D.EURUSD.CFD.IP", 0.72), _ranking("IX.D.DOW.IFM.IP", 0.55)],
        open_positions=1,
        margin_used_gbp=100.0,
    )
    with patch.object(pee, "_log_returns", return_value=rets), patch.object(
        pee, "_pearson", return_value=0.75
    ):
        blocked_hi, _, exp_hi = pee.correlation_blocks_entry(
            "CS.D.EURUSD.CFD.IP",
            "BUY",
            [{"epic": "IX.D.DOW.IFM.IP", "direction": "BUY"}],
        )
    assert exp_hi[0]["threshold"] == 0.82
    assert blocked_hi is False


def test_fast_pass_token_queue_prioritizes_micro_scalp():
    cg.enqueue_fast_pass_token(epic="CS.D.EURUSD.CFD.IP", direction="BUY", score=80.0)
    snap = cg.get_fast_pass_queue_snapshot()
    assert snap and snap[0]["epic"] == "CS.D.EURUSD.CFD.IP"
    ok = cg.acquire_outbound_token(
        "ig",
        category="order",
        priority="fast_pass",
        epic="CS.D.EURUSD.CFD.IP",
        max_wait_sec=0.05,
    )
    assert ok is True


def test_five_stage_boot_machine_trade_ready():
    boot = mls.run_five_stage_boot()
    assert boot["trade_ready"] is True
    assert len(boot["stage_tokens"]) == 5
    assert all(v == mo._TOKEN_SUCCESS for v in boot["stage_tokens"].values())


def test_strategy_a_micro_scalp_market_ioc_with_fast_pass():
    mls.seed_exploration_universe()
    micro = MagicMock()
    micro.micro_trend_for.return_value = _microkernel_high_ofi()

    with patch.object(mls, "inject_multi_market_quotes", return_value={"published": ["CS.D.EURUSD.CFD.IP"], "count": 1, "elapsed_ms": 0.1}), patch(
        "apex.microkernel.get_microkernel", return_value=micro
    ), patch("runtime.trade_manager.dispatch_piercing_zone_order", return_value={"ok": True}), patch.object(
        mo, "validate_regime_entropy_arbitration", return_value=(True, "")
    ):
        result = mls.verify_strategy_a_micro_scalp()

    assert result["ok"] is True
    assert result["order_type"] == "MARKET_IOC"
    assert result["route"] == dce.ROUTE_MICRO_SCALP_IOC
    phases = result["phases"]
    assert phases["tick_ingestion_ms"] >= 0
    assert phases["token_acquisition_ms"] >= 0


def test_strategy_b_limit_chase_three_tick_cancel():
    result = mls.verify_strategy_b_limit_chase()
    assert result["ok"] is True
    assert result["tick_placements"] >= 3
    assert result["final_reason"] == "limit_chase_max_ticks_exceeded"
    assert result["order_type"] == "LIMIT"


def test_strategy_c_shadow_walk_passes_065_floor():
    import numpy as np
    from trading.probability_engine import _FORWARD_WALK_VETO_FLOOR

    regime_snap = MagicMock(state=1, confidence=0.95)
    transition = np.array(
        [[0.05, 0.90, 0.05], [0.05, 0.90, 0.05], [0.05, 0.90, 0.05]],
        dtype=np.float64,
    )
    with patch("runtime.regime_switch_engine.evaluate_epic_regime", return_value=regime_snap), patch(
        "runtime.regime_switch_engine.get_regime_transition_matrix", return_value=transition
    ):
        result = mls.verify_strategy_c_momentum_shadow_walk()
    assert result["ok"] is True
    assert result["projected_win_prob"] >= _FORWARD_WALK_VETO_FLOOR
    assert result["shadow_walk_veto"] is False
    assert result["order_type"] == "MARKET_IOC"


def test_trade_resolution_trailing_atr_and_scoreboard():
    with patch("analytics.triage_logger.get_triage_logger") as mock_logger, patch(
        "system.alert_reporting_matrix.notify_pp_boundary_crossing"
    ):
        mock_logger.return_value.log_closed_position.return_value = None
        result = mls.resolve_trade_with_trailing_and_triage()

    assert result["trail_base_stop"] is not None
    assert result["trail_expanded_stop"] is not None
    assert result["scoreboard"]["telemetry_tier"] == mo.TELEMETRY_TIER_EMERALD
    assert result["scoreboard"]["pp_event"]["delta"] == 50
    assert result["triage_record"]["written"] is True


def test_full_master_lifecycle_writes_manifest(tmp_path: Path):
    micro = MagicMock()
    micro.micro_trend_for.return_value = _microkernel_high_ofi()
    report = tmp_path / "master_lifecycle_report.json"

    with patch.object(mls, "inject_multi_market_quotes", return_value={"published": [], "count": 5, "elapsed_ms": 1.0}), patch(
        "cockpit.desktop_process_guard.audit_and_purge_bound_ports",
        return_value={"ok": True, "purged_pids": [], "ports": {}},
    ), patch("runtime.regime_switch_engine.evaluate_epic_regime", return_value=MagicMock(state=1, confidence=0.95)), patch(
        "runtime.regime_switch_engine.get_regime_transition_matrix",
        return_value=np.array(
            [[0.05, 0.90, 0.05], [0.05, 0.90, 0.05], [0.05, 0.90, 0.05]],
            dtype=np.float64,
        ),
    ), patch("apex.microkernel.get_microkernel", return_value=micro), patch(
        "runtime.trade_manager.dispatch_piercing_zone_order", return_value={"ok": True}
    ), patch.object(mo, "validate_regime_entropy_arbitration", return_value=(True, "")), patch(
        "system.alert_reporting_matrix.notify_pp_boundary_crossing"
    ), patch(
        "analytics.triage_logger.get_triage_logger"
    ) as mock_triage:
        mock_triage.return_value.log_closed_position.return_value = None
        manifest = mls.run_master_lifecycle_verification(
            write_report=True,
            report_path=report,
        )

    assert report.is_file()
    on_disk = json.loads(report.read_text(encoding="utf-8"))
    assert on_disk["final_status"] == "LIVE_LIFECYCLE_VERIFIED"
    assert manifest["pipeline_benchmark"]["benchmark_application_score"] == mo.BENCHMARK_APPLICATION_SCORE
    assert len(manifest["strategies"]) == 3
    assert manifest["dynamic_governance"]["eurusd_dynamic_threshold"] == 0.82
    assert manifest["trade_resolution"]["scoreboard"]["telemetry_tier"] == mo.TELEMETRY_TIER_EMERALD


def test_port_collision_audit_in_lifecycle_supervisor():
    with patch("cockpit.desktop_process_guard.audit_and_purge_bound_ports") as mock_audit, patch(
        "cockpit.desktop_process_guard.port_is_bound", return_value=False
    ):
        mock_audit.return_value = {"ok": True, "purged_pids": [9999], "ports": {}}
        audit = mls.audit_ports_for_lifecycle()
    assert audit["8080_vacant"] is True
    assert audit["8787_vacant"] is True
    assert 9999 in mock_audit.return_value["purged_pids"]
