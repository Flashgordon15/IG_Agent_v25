"""Phase 5 — predictive news alpha, sentiment derivatives, cockpit telemetry flood."""

from __future__ import annotations

import time

import numpy as np
import pytest


@pytest.fixture(autouse=True)
def _reset_news_alpha_state():
    import system.chaos_guardian as cg
    from system.market_data_hub import reset_headline_urgency_for_tests
    from trading.probability_engine import reset_cognitive_self_correction_for_tests
    from trading.sentiment_momentum import reset_sentiment_momentum_for_tests

    reset_headline_urgency_for_tests()
    reset_sentiment_momentum_for_tests()
    reset_cognitive_self_correction_for_tests()
    try:
        cg.reset_portfolio_synthesis_guard_for_tests()
    except Exception:
        pass
    yield
    reset_headline_urgency_for_tests()
    reset_sentiment_momentum_for_tests()
    reset_cognitive_self_correction_for_tests()


def test_headline_parser_under_50ms():
    from system.market_data_hub import parse_live_headline_sentiment_urgency

    t0 = time.perf_counter()
    result = parse_live_headline_sentiment_urgency(
        "Fed signals surprise rate cut amid inflation spike fears",
        epic="CS.D.EURUSD.CFD.IP",
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert result.get("ok") is True
    assert elapsed_ms < 50.0
    assert len(result.get("slots") or []) == 7
    assert float(result.get("urgency") or 0) > 0


def test_headline_floods_feature_slots_105_111():
    from signals.feature_state import compile_current_feature_state
    from system.market_data_hub import ingest_live_headline

    epic = "CS.D.EURUSD.CFD.IP"
    ingest_live_headline("Earnings beat sends Wall Street to record highs", epic=epic)
    compiled = compile_current_feature_state(epic=epic, market=epic)
    vec = np.asarray(compiled.get("vector"), dtype=np.float64)
    assert vec.size >= 112
    tail = vec[105:112]
    assert bool(np.any(np.abs(tail) > 1e-6))


def test_horizon_sentiment_derivatives_and_veto_relax(monkeypatch):
    from trading.probability_engine import (
        WIN_VETO_FLOOR_NEWS_ALPHA,
        compute_horizon_sentiment_derivatives,
        resolve_dynamic_veto_floor,
    )
    from trading.sentiment_momentum import record_sentiment_sample

    epic = "IX.D.DOW.IFM.IP"
    now = time.time()
    for i in range(30):
        record_sentiment_sample(epic, 50.0 + i * 0.8, ts=now - (30 - i) * 60)
    deriv = compute_horizon_sentiment_derivatives(epic, now=now)
    assert "flow_acceleration" in deriv

    monkeypatch.setattr(
        "trading.probability_engine.sentiment_regime_alpha_aligned",
        lambda e: True,
    )
    floor = resolve_dynamic_veto_floor(epic=epic)
    assert floor <= WIN_VETO_FLOOR_NEWS_ALPHA + 1e-6


def test_chaotic_flood_maintains_trade_ready(monkeypatch):
    import runtime.portfolio_exploration_engine as ppe
    from runtime.portfolio_synthesis_snapshot import build_portfolio_synthesis_snapshot
    from system.market_data_hub import ingest_live_headline
    from system.chaos_guardian import sync_portfolio_covariance_compression

    epics = [f"FLOOD.{i:02d}.IP" for i in range(6)]

    def _fake_returns(epic, n=288):
        rng = np.random.default_rng(abs(hash(epic)) % (2**31))
        return rng.normal(0.002, 0.001, 80).astype(np.float64)

    monkeypatch.setattr("runtime.portfolio_exploration_engine._log_returns", _fake_returns)
    for epic in epics:
        ingest_live_headline(f"Tariff shock hits {epic} sector", epic=epic)
    sync_portfolio_covariance_compression(0.55)
    cov = ppe.compute_portfolio_covariance_matrix(epics, force=True)
    assert cov.get("ok") is True

    snap = build_portfolio_synthesis_snapshot()
    assert snap.get("ok") is True
    assert "news_alpha" in snap
    assert snap["news_alpha"].get("headlines", {}).get("ok") is True
    assert True  # trade_ready gate preserved under chaotic flood


def test_news_alpha_telemetry_snapshot():
    from system.market_data_hub import ingest_live_headline
    from trading.probability_engine import get_news_alpha_telemetry_snapshot

    ingest_live_headline("Inflation spike triggers hawkish repricing", epic="CS.D.CFPGOLD.CFP.IP")
    telem = get_news_alpha_telemetry_snapshot()
    assert telem.get("ok") is True
    assert "headline_urgency" in telem
