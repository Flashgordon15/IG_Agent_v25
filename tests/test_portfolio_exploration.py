"""Portfolio exploration engine — 50-asset margin safety & correlation guard."""

from __future__ import annotations

import numpy as np
import pytest
from unittest.mock import patch

from runtime import portfolio_exploration_engine as pee


@pytest.fixture(autouse=True)
def _isolate():
    pee.reset_portfolio_exploration_for_tests()
    with pee._lock:
        pee._enabled = True
    yield
    pee.reset_portfolio_exploration_for_tests()


def _ranking(epic: str, score: float = 0.8, state: int = 1) -> dict:
    return {
        "epic": epic,
        "asset_class": "indices",
        "regime_state": state,
        "confidence": 0.75,
        "profit_factor": 1.4,
        "score": score,
        "size_factor": 1.0,
        "tpm": 12.0,
        "spread_pts": 2.0,
    }


def _build_50_asset_rankings() -> list[dict]:
    classes = ("fx", "indices", "commodities", "crypto", "other")
    rows = []
    for i in range(50):
        rows.append(
            {
                "epic": f"TEST.EPIC.{i:03d}.IP",
                "asset_class": classes[i % len(classes)],
                "regime_state": 0 if i % 3 == 0 else 1,
                "confidence": 0.55 + (i % 10) * 0.04,
                "profit_factor": 1.1 + (i % 5) * 0.1,
                "score": 0.5 + (50 - i) * 0.01,
                "size_factor": 0.85 if i % 3 == 0 else 1.1,
                "tpm": 8.0 + i * 0.2,
                "spread_pts": 1.5 + (i % 7) * 0.3,
            }
        )
    return rows


def test_account_equity_target_hardcoded():
    assert pee.ACCOUNT_EQUITY_TARGET_GBP == 10_000.0


def test_max_concurrent_from_margin():
    per = pee.regime_adjusted_margin_per_trade(size_factor=1.0, stop_factor=1.0)
    max_trades = pee.compute_max_concurrent_trades(
        available_margin_gbp=pee.ACCOUNT_EQUITY_TARGET_GBP,
        size_factor=1.0,
        stop_factor=1.0,
    )
    assert max_trades == int(pee.ACCOUNT_EQUITY_TARGET_GBP // per)
    assert max_trades >= 10


def test_50_asset_frenzy_margin_cap_safe():
    """50 ranked markets — concurrent slots must not exceed margin budget."""
    rankings = _build_50_asset_rankings()
    per_trade = pee.regime_adjusted_margin_per_trade(
        size_factor=1.0, stop_factor=1.0, equity=pee.ACCOUNT_EQUITY_TARGET_GBP
    )
    max_allowed = pee.compute_max_concurrent_trades(
        available_margin_gbp=pee.ACCOUNT_EQUITY_TARGET_GBP,
        size_factor=1.0,
        stop_factor=1.0,
    )
    pee.inject_exploration_rankings_for_tests(
        rankings,
        max_concurrent=max_allowed,
        open_positions=0,
    )
    assert max_allowed * per_trade <= pee.ACCOUNT_EQUITY_TARGET_GBP + per_trade
    snap = pee.get_exploration_state_snapshot()
    assert snap["universe_size"] == 50
    assert snap["max_concurrent_trades"] == max_allowed
    approved_count = 0
    with patch.object(pee, "regime_direction_aligned", return_value=(True, "")):
        for row in rankings[:max_allowed]:
            result = pee.assess_portfolio_exploration(
                epic=row["epic"],
                direction="BUY",
                size=1.0,
                stop_distance=10.0,
                limit_distance=15.0,
                account_available=pee.ACCOUNT_EQUITY_TARGET_GBP,
            )
            if result.approved:
                approved_count += 1
    assert approved_count == max_allowed


def test_blocks_when_at_capacity(monkeypatch):
    rankings = [_ranking("IX.D.DOW.IFM.IP")]
    pee.inject_exploration_rankings_for_tests(
        rankings, max_concurrent=2, open_positions=2
    )
    monkeypatch.setattr(
        pee,
        "_load_open_book",
        lambda: [{"epic": "A", "direction": "BUY", "size": 1}] * 2,
    )
    monkeypatch.setattr(pee, "compute_max_concurrent_trades", lambda **kw: 2)
    result = pee.assess_portfolio_exploration(
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=1.0,
        stop_distance=10.0,
        limit_distance=15.0,
        account_available=5000.0,
    )
    assert result.approved is False
    assert "max_concurrent" in result.reason


def test_correlation_blocks_highly_correlated(monkeypatch):
    rng = np.random.default_rng(42)
    base = rng.normal(0, 0.01, 100)
    noise = rng.normal(0, 0.0001, 100)

    def _fake_returns(epic: str, **kwargs):
        if epic == "IX.D.DOW.IFM.IP":
            return base
        if epic == "IX.D.NASDAQ.IFM.IP":
            return base + noise
        return rng.normal(0, 0.01, 80)

    monkeypatch.setattr(pee, "_log_returns", _fake_returns)
    blocked, reason, exposures = pee.correlation_blocks_entry(
        "IX.D.NASDAQ.IFM.IP",
        "BUY",
        [{"epic": "IX.D.DOW.IFM.IP", "direction": "BUY", "size": 1.0}],
    )
    assert blocked is True
    assert "correlation" in reason
    assert exposures[0]["correlation"] > pee.CORRELATION_THRESHOLD


def test_correlation_allows_uncorrelated(monkeypatch):
    rng = np.random.default_rng(7)

    def _fake_returns(epic: str, **kwargs):
        return rng.normal(0, 0.02, 120)

    monkeypatch.setattr(pee, "_log_returns", _fake_returns)
    blocked, reason, _ = pee.correlation_blocks_entry(
        "CS.D.EURUSD.CFD.IP",
        "BUY",
        [{"epic": "CS.D.CFPGOLD.CFP.IP", "direction": "BUY", "size": 1.0}],
    )
    assert blocked is False
    assert reason == ""


def test_exploration_allows_hot_path_when_ranked():
    pee.inject_exploration_rankings_for_tests(
        [_ranking("CS.D.CRUDE.CFD.IP")],
        max_concurrent=10,
        open_positions=1,
    )
    pee._enabled = True
    assert pee.exploration_allows_hot_path("CS.D.CRUDE.CFD.IP") is True
    assert pee.exploration_allows_hot_path("UNKNOWN.EPIC.IP") is False


def test_kelly_fraction_capped():
    k = pee._kelly_fraction(0.9, 2.5)
    assert 0.02 <= k <= pee.KELLY_CAP


def test_exploration_state_snapshot_fields():
    pee.inject_exploration_rankings_for_tests(
        _build_50_asset_rankings()[:5],
        max_concurrent=8,
        margin_used_gbp=400.0,
    )
    snap = pee.get_exploration_state_snapshot()
    assert snap["account_equity_target_gbp"] == 10_000.0
    assert snap["universe_size"] == 5
    assert "market_rankings" in snap
    assert "capital_allocation_pct" in snap
    assert "correlation_exposures" in snap
    assert "position_tree" in snap


def test_risk_parity_weights_sum_to_one():
    candidates = [
        pee._MarketCandidate(
            epic=f"E{i}",
            asset_class="fx",
            state=1,
            confidence=0.7,
            profit_factor=1.2,
            score=0.5 + i * 0.1,
            stop_factor=1.0 + i * 0.05,
        )
        for i in range(5)
    ]
    weights = pee._risk_parity_weights(candidates)
    assert abs(sum(weights.values()) - 1.0) < 1e-9


def test_disabled_exploration_passes_through():
    pee.reset_portfolio_exploration_for_tests()
    with pee._lock:
        pee._enabled = False
    result = pee.assess_portfolio_exploration(
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=2.0,
        stop_distance=10.0,
        limit_distance=15.0,
    )
    assert result.approved is True
    assert result.size == 2.0
