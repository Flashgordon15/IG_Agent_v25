"""Tests for v26 portfolio capital envelope allocator."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "v26"))

from portfolio.allocator import PortfolioAllocator


def test_from_config_defaults() -> None:
    alloc = PortfolioAllocator.from_config({})
    assert alloc.account_balance_gbp == 10_000.0
    assert alloc.max_concurrent_risk_gbp == 1_200.0


def test_can_allocate_within_caps() -> None:
    alloc = PortfolioAllocator(
        account_balance_gbp=10_000,
        max_concurrent_risk_gbp=1_200,
        max_daily_risk_deployed_gbp=2_500,
        min_available_gbp=100,
        reserve_pct=0.10,
    )
    ok, reason = alloc.can_allocate(200.0)
    assert ok is True
    assert reason == "ok"


def test_can_allocate_blocks_concurrent_cap() -> None:
    alloc = PortfolioAllocator(
        account_balance_gbp=10_000,
        max_concurrent_risk_gbp=500,
        max_daily_risk_deployed_gbp=2_500,
        concurrent_risk_gbp=450,
    )
    ok, reason = alloc.can_allocate(100.0)
    assert ok is False
    assert "concurrent cap" in reason


def test_can_allocate_blocks_daily_loss() -> None:
    alloc = PortfolioAllocator(max_daily_loss_gbp=500, daily_pnl_gbp=-500)
    ok, reason = alloc.can_allocate(50.0)
    assert ok is False
    assert "daily loss limit" in reason


def test_record_and_release_updates_snapshot() -> None:
    alloc = PortfolioAllocator(
        account_balance_gbp=10_000,
        max_concurrent_risk_gbp=1_200,
        reserve_pct=0.10,
    )
    alloc.record_intent(300.0)
    snap = alloc.snapshot()
    assert snap["concurrent_risk_gbp"] == 300.0
    assert snap["open_positions"] == 1
    assert snap["utilization_pct"] == 25.0

    alloc.release_risk(300.0, pnl_gbp=50.0)
    snap2 = alloc.snapshot()
    assert snap2["concurrent_risk_gbp"] == 0.0
    assert snap2["daily_pnl_gbp"] == 50.0
    assert snap2["open_positions"] == 0
