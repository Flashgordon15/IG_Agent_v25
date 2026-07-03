"""Phase 3 alpha acceleration — decay curves, HVN gate, asymmetric trails, lead-lag, RLS."""

from __future__ import annotations

import time

import numpy as np
import pytest


@pytest.fixture(autouse=True)
def _reset_alpha_state():
    import execution.risk_manager as rm
    import runtime.master_orchestrator as mo
    import runtime.portfolio_exploration_engine as ppe
    from trading.probability_engine import (
        reset_alpha_decay_for_tests,
        reset_cognitive_self_correction_for_tests,
        reset_multi_horizon_cache_for_tests,
    )

    mo.reset_master_orchestrator_for_tests()
    ppe.reset_portfolio_exploration_for_tests()
    rm.reset_asymmetric_risk_for_tests()
    reset_cognitive_self_correction_for_tests()
    reset_multi_horizon_cache_for_tests()
    reset_alpha_decay_for_tests()
    yield
    mo.reset_master_orchestrator_for_tests()
    ppe.reset_portfolio_exploration_for_tests()
    rm.reset_asymmetric_risk_for_tests()
    reset_cognitive_self_correction_for_tests()
    reset_multi_horizon_cache_for_tests()
    reset_alpha_decay_for_tests()


def test_alpha_decay_exponential_kill_after_1500ms():
    from trading.probability_engine import (
        _ALPHA_DECAY_STRICT_FLOOR,
        compute_alpha_decayed_score,
        register_limit_chase_alpha,
    )

    epic = "CS.D.EURUSD.CFD.IP"
    register_limit_chase_alpha(
        epic=epic, direction="BUY", expectation_score=0.72, ts=time.time() - 3.5
    )
    decayed, elapsed_ms, kill = compute_alpha_decayed_score(epic=epic, direction="BUY")
    assert elapsed_ms > 1500.0
    assert decayed < 0.72
    assert kill is True or decayed < _ALPHA_DECAY_STRICT_FLOOR


def test_alpha_decay_no_kill_within_fill_window():
    from trading.probability_engine import (
        compute_alpha_decayed_score,
        register_limit_chase_alpha,
    )

    epic = "CS.D.CFPGOLD.CFP.IP"
    now = time.time()
    register_limit_chase_alpha(
        epic=epic, direction="SELL", expectation_score=0.68, ts=now - 0.5
    )
    decayed, elapsed_ms, kill = compute_alpha_decayed_score(
        epic=epic, direction="SELL", now=now
    )
    assert elapsed_ms < 1500.0
    assert decayed == pytest.approx(0.68, rel=1e-3)
    assert kill is False


def test_hvn_volume_gate_blocks_low_volume_entry():
    from runtime.portfolio_exploration_engine import (
        _session_clock_minute,
        _session_hvn,
        _volume_profile_lock,
        _volume_ticks,
        record_volume_tick,
        volume_profile_aligns_with_hvn,
    )

    epic = "IX.D.DOW.IFM.IP"
    bucket = _session_clock_minute()
    with _volume_profile_lock:
        _volume_ticks[epic] = __import__("collections").deque(maxlen=512)
        _session_hvn[epic] = {bucket: 50.0}
    record_volume_tick(epic, tpm=0.5)
    ok, reason = volume_profile_aligns_with_hvn(epic)
    assert ok is False
    assert "hvn" in reason.lower()


def test_asymmetric_trailing_long_relaxed_short_tightens():
    from execution.risk_manager import compute_asymmetric_trail_stop

    epic = "IX.D.NASDAQ.IFM.IP"
    atr = 25.0
    long_row = compute_asymmetric_trail_stop(
        epic=epic,
        side="BUY",
        entry=18000.0,
        current_stop=17950.0,
        atr=atr,
        bid=18020.0,
        offer=18022.0,
    )
    assert long_row["mode"] == "long_relaxed_atr"
    assert long_row["multiplier"] == 2.5
    assert long_row["proposed_stop"] >= 17950.0

    short_row = compute_asymmetric_trail_stop(
        epic=epic,
        side="SELL",
        entry=18000.0,
        current_stop=18080.0,
        atr=atr,
        bid=17980.0,
        offer=17982.0,
    )
    assert short_row["mode"] == "short_ema_high_tighten"
    assert short_row["proposed_stop"] <= 18080.0


def test_lead_lag_promotes_lag_and_fast_pass(monkeypatch):
    import runtime.master_orchestrator as mo

    tokens: list[dict] = []

    def _capture(**kwargs):
        tokens.append(kwargs)

    monkeypatch.setattr(mo, "_epic_conviction_score", lambda epic: 0.72)
    monkeypatch.setattr(
        "system.chaos_guardian.enqueue_fast_pass_token",
        lambda **kw: _capture(**kw),
    )

    fired = mo.scan_lead_lag_arbitrage()
    assert len(fired) >= 1
    assert mo.get_lead_lag_score_boost("IX.D.NASDAQ.IFM.IP") >= 0.75
    assert any(t.get("epic") == "IX.D.NASDAQ.IFM.IP" for t in tokens)


def test_rls_calibration_triggers_on_low_win_rate(monkeypatch):
    from trading.probability_engine import (
        _RLS_WIN_RATE_TARGET,
        detect_sentiment_news_feature_drift,
        record_strategy_route_outcome,
        run_rls_calibration_pass,
    )

    monkeypatch.setattr(
        "trading.probability_engine.detect_sentiment_news_feature_drift",
        lambda **kw: ["CS.D.EURUSD.CFD.IP"],
    )
    route = "limit_chase_hf"
    for won in [False] * 12 + [True] * 3:
        record_strategy_route_outcome(route, won)
    vec = np.zeros(128, dtype=np.float64)
    vec[98:112] = 0.8
    result = run_rls_calibration_pass(route=route, feature_vector=vec)
    assert result.get("adjusted") is True
    assert result.get("win_rate_48h", 1.0) < _RLS_WIN_RATE_TARGET


def test_institutional_snapshot_includes_alpha_acceleration_fields():
    from runtime.institutional_snapshot import build_institutional_matrix_snapshot

    snap = build_institutional_matrix_snapshot()
    assert snap.get("ok") is True
    assert "alpha_decay" in snap
    assert "lead_lag_arbitrage" in snap
    assert "asymmetric_risk" in snap
    assert "rls_calibrator" in snap
    assert "volume_profile" in snap
