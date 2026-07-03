"""Phase 4 portfolio synthesis — covariance, equilibrium allocator, stress harness."""

from __future__ import annotations

import time

import numpy as np
import pytest


@pytest.fixture(autouse=True)
def _reset_synthesis_state():
    import execution.risk_manager as rm
    import runtime.portfolio_exploration_engine as ppe
    import system.chaos_guardian as cg

    ppe.reset_portfolio_exploration_for_tests()
    rm.reset_equilibrium_risk_for_tests()
    rm.reset_asymmetric_risk_for_tests()
    try:
        cg.reset_portfolio_synthesis_guard_for_tests()
    except Exception:
        pass
    yield
    ppe.reset_portfolio_exploration_for_tests()
    rm.reset_equilibrium_risk_for_tests()
    rm.reset_asymmetric_risk_for_tests()
    try:
        cg.reset_portfolio_synthesis_guard_for_tests()
    except Exception:
        pass


def _seed_synthetic_returns(epic: str, n: int, *, bias: float = 0.0) -> None:
    """Inject synthetic log-return series via monkeypatched _log_returns path."""
    from runtime import portfolio_exploration_engine as ppe

    rng = np.random.default_rng(abs(hash(epic)) % (2**31))
    rets = rng.normal(bias, 0.002, n).astype(np.float64)
    if not hasattr(_seed_synthetic_returns, "_cache"):
        _seed_synthetic_returns._cache = {}
    _seed_synthetic_returns._cache[epic] = rets


@pytest.fixture
def synthetic_returns_monkeypatch(monkeypatch):
    cache: dict[str, np.ndarray] = {}

    def _fake(epic: str, n: int = 288):
        return cache.get(epic)

    def seed(epic: str, n: int = 288, bias: float = 0.0):
        rng = np.random.default_rng(abs(hash(epic)) % (2**31))
        cache[epic] = rng.normal(bias, 0.002, n).astype(np.float64)

    monkeypatch.setattr(
        "runtime.portfolio_exploration_engine._log_returns",
        lambda epic, n=288: cache.get(str(epic)),
    )
    return seed


def test_covariance_matrix_triggers_compression(synthetic_returns_monkeypatch):
    from runtime.portfolio_exploration_engine import (
        compute_portfolio_covariance_matrix,
        get_covariance_compression_factor,
    )

    epics = [f"STRESS.EPIC.{i:02d}.IP" for i in range(8)]
    for epic in epics:
        synthetic_returns_monkeypatch(epic, bias=0.0015)
    body = compute_portfolio_covariance_matrix(epics, force=True)
    assert body.get("ok") is True
    assert len(body.get("epics") or []) >= 2
    assert float(body.get("collective_coefficient") or 0) > 0
    factor = get_covariance_compression_factor()
    assert 0.25 <= factor <= 1.0


def test_equilibrium_allocator_downscales_under_margin_pressure(monkeypatch):
    from execution.risk_manager import (
        EQUILIBRIUM_EQUITY_CAP_GBP,
        apply_equilibrium_risk_allocation,
    )

    open_book = [
        {"epic": f"IX.D.STRESS{i}.IFM.IP", "direction": "BUY", "size": 8.0}
        for i in range(12)
    ]
    monkeypatch.setattr(
        "runtime.portfolio_exploration_engine._load_open_book",
        lambda: list(open_book),
    )
    monkeypatch.setattr(
        "runtime.portfolio_exploration_engine.regime_adjusted_margin_per_trade",
        lambda **kw: 450.0,
    )

    adjusted, meta = apply_equilibrium_risk_allocation(
        epic="IX.D.NEW.IFM.IP",
        proposed_size=5.0,
        equity=EQUILIBRIUM_EQUITY_CAP_GBP,
    )
    assert adjusted < 5.0
    assert adjusted == 0.0 or float(meta.get("scale_factor") or 1.0) < 1.0
    assert "equilibrium_margin_ceiling" in str(meta.get("block_reason") or "")


def test_equity_curve_trailing_fuse_tightens_below_800_pp(monkeypatch):
    from execution.risk_manager import get_equity_curve_trailing_fuse_snapshot

    class _SB:
        total_pp = 750
        def telemetry_tier_unlocked(self):
            return "amber_defense"

    monkeypatch.setattr(
        "runtime.master_orchestrator.get_platform_scoreboard",
        lambda: _SB(),
    )
    fuse = get_equity_curve_trailing_fuse_snapshot()
    assert fuse.get("defensive_fuse_active") is True
    assert float(fuse.get("l1_drawdown_pct") or 99) < 2.0
    assert float(fuse.get("l2_drawdown_pct") or 99) < 4.0


def test_fifteen_ticker_stress_no_margin_breach(synthetic_returns_monkeypatch, monkeypatch):
    from execution.risk_manager import (
        EQUILIBRIUM_EQUITY_CAP_GBP,
        apply_equilibrium_risk_allocation,
    )
    from runtime.portfolio_exploration_engine import (
        apply_covariance_compression,
        compute_portfolio_covariance_matrix,
    )

    epics = [f"HV.STRESS.{i:02d}.IP" for i in range(15)]
    for i, epic in enumerate(epics):
        synthetic_returns_monkeypatch(epic, bias=0.003 * (1 + i * 0.05))

    cov = compute_portfolio_covariance_matrix(epics, force=True)
    assert cov.get("ok") is True

    book: list[dict] = []
    margins: list[float] = []

    def _book():
        return list(book)

    monkeypatch.setattr("runtime.portfolio_exploration_engine._load_open_book", _book)
    monkeypatch.setattr(
        "runtime.portfolio_exploration_engine.regime_adjusted_margin_per_trade",
        lambda **kw: 200.0,
    )

    for epic in epics:
        raw_size = 6.0
        compressed = apply_covariance_compression(raw_size)
        adjusted, meta = apply_equilibrium_risk_allocation(
            epic=epic,
            proposed_size=compressed,
            equity=EQUILIBRIUM_EQUITY_CAP_GBP,
        )
        if adjusted <= 0:
            continue
        book.append({"epic": epic, "direction": "BUY", "size": adjusted})
        margins.append(float(meta.get("proposed_margin_gbp") or 0))

    total_margin = 200.0 * sum(float(r["size"]) for r in book)
    assert total_margin <= EQUILIBRIUM_EQUITY_CAP_GBP + 1.0
    assert len(book) >= 1


def test_portfolio_synthesis_snapshot_shape():
    from runtime.portfolio_synthesis_snapshot import build_portfolio_synthesis_snapshot

    snap = build_portfolio_synthesis_snapshot()
    assert snap.get("ok") is True
    assert "covariance" in snap
    assert "equilibrium_allocation" in snap
    assert "cognitive_risk_heatmap" in snap
    assert "drawdown_fuse" in snap


def test_cognitive_risk_heatmap_cells():
    from trading.probability_engine import build_cognitive_risk_heatmap

    heat = build_cognitive_risk_heatmap()
    assert heat.get("ok") is True
    assert "pair_cells" in heat
    assert "asset_weights" in heat


def test_chaos_guardian_syncs_compression():
    from system.chaos_guardian import (
        get_portfolio_synthesis_guard_snapshot,
        sync_portfolio_covariance_compression,
    )

    sync_portfolio_covariance_compression(0.62)
    snap = get_portfolio_synthesis_guard_snapshot()
    assert float(snap.get("compression_factor") or 1) == pytest.approx(0.62, rel=1e-3)
    assert snap.get("risk_parity_engaged") is True
