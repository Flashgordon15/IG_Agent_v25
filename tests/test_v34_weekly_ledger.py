"""v34 weekly performance ledger — journal-backed 7-day metrics."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from analytics.weekly_performance_ledger import (
    WeeklyPerformanceLedger,
    enable_weekly_ledger_sync_mode_for_tests,
    reset_weekly_performance_ledger_for_tests,
)
from diagnostics.performance_journal import (
    enable_sync_mode_for_tests,
    record_trade_close,
    reset_performance_journal_for_tests,
)
from system.engine_lane import (
    DEFAULT_ACCOUNT_CFD,
    DEFAULT_ACCOUNT_SB,
    ENGINE_CFD_SNIPER,
    ENGINE_SB_SENTINEL,
    ENGINE_ORIGIN_CFD,
    ENGINE_ORIGIN_SB,
)


@pytest.fixture(autouse=True)
def _clean_ledgers() -> None:
    reset_performance_journal_for_tests()
    reset_weekly_performance_ledger_for_tests()
    enable_sync_mode_for_tests(True)
    enable_weekly_ledger_sync_mode_for_tests(True)
    yield
    reset_performance_journal_for_tests()
    reset_weekly_performance_ledger_for_tests()


def test_empty_history_returns_clean_nulls_and_zeros(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "daily_journal.csv"
    monkeypatch.setattr("analytics.weekly_performance_ledger._journal_path", lambda p=None: path)
    monkeypatch.setattr(
        "diagnostics.performance_journal._learning_deal_epic_map",
        lambda: {},
    )

    payload = WeeklyPerformanceLedger.compile_weekly_metrics(force_refresh=True, path=path)
    assert payload["ok"] is True
    merged = payload["merged"]
    assert merged["weekly_sharpe"] is None
    assert merged["asymmetric_profit_factor"] == 0.0
    assert merged["win_rate"] == 0.0
    assert merged["sample_n"] == 0
    assert merged["asset_breakdown"] == []
    assert DEFAULT_ACCOUNT_CFD in payload["accounts"]
    assert DEFAULT_ACCOUNT_SB in payload["accounts"]
    assert payload["accounts"][DEFAULT_ACCOUNT_CFD]["weekly_sharpe"] is None


def test_asymmetric_profit_factor_from_wins_and_losses(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "daily_journal.csv"
    monkeypatch.setattr("diagnostics.performance_journal.journal_path", lambda: path)
    monkeypatch.setattr("analytics.weekly_performance_ledger._journal_path", lambda p=None: path)
    monkeypatch.setattr(
        "diagnostics.performance_journal._learning_deal_epic_map",
        lambda: {},
    )

    record_trade_close(
        deal_id="WIN01",
        direction="BUY",
        entry_price=100.0,
        exit_price=102.0,
        realized_pnl_gbp=20.0,
        account_id=DEFAULT_ACCOUNT_SB,
        product_type="SPREADBET",
        engine_origin=ENGINE_ORIGIN_SB,
        engine_id=ENGINE_SB_SENTINEL,
    )
    record_trade_close(
        deal_id="LOSS01",
        direction="SELL",
        entry_price=100.0,
        exit_price=101.0,
        realized_pnl_gbp=-10.0,
        account_id=DEFAULT_ACCOUNT_SB,
        product_type="SPREADBET",
        engine_origin=ENGINE_ORIGIN_SB,
        engine_id=ENGINE_SB_SENTINEL,
    )

    payload = WeeklyPerformanceLedger.compile_weekly_metrics(force_refresh=True, path=path)
    merged = payload["merged"]
    assert merged["sample_n"] == 2
    assert merged["wins"] == 1
    assert merged["losses"] == 1
    assert merged["asymmetric_profit_factor"] == 2.0
    assert merged["win_rate"] == 0.5


def test_multi_account_asset_breakdown(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "daily_journal.csv"
    monkeypatch.setattr("diagnostics.performance_journal.journal_path", lambda: path)
    monkeypatch.setattr("analytics.weekly_performance_ledger._journal_path", lambda p=None: path)
    monkeypatch.setattr(
        "diagnostics.performance_journal._learning_deal_epic_map",
        lambda: {
            "CFDWIN": "IX.D.DOW.IFM.IP",
            "SBWIN": "CS.D.CFPGOLD.CFP.IP",
        },
    )
    fixed_now = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
    closed_ts = fixed_now.timestamp()

    record_trade_close(
        deal_id="CFDWIN",
        direction="BUY",
        entry_price=100.0,
        exit_price=101.0,
        realized_pnl_gbp=15.0,
        closed_at_ts=closed_ts,
        account_id=DEFAULT_ACCOUNT_CFD,
        product_type="CFD",
        engine_origin=ENGINE_ORIGIN_CFD,
        engine_id=ENGINE_CFD_SNIPER,
    )
    record_trade_close(
        deal_id="SBWIN",
        direction="BUY",
        entry_price=2000.0,
        exit_price=2010.0,
        realized_pnl_gbp=25.0,
        closed_at_ts=closed_ts,
        account_id=DEFAULT_ACCOUNT_SB,
        product_type="SPREADBET",
        engine_origin=ENGINE_ORIGIN_SB,
        engine_id=ENGINE_SB_SENTINEL,
    )

    payload = WeeklyPerformanceLedger.compile_weekly_metrics(
        force_refresh=True,
        path=path,
    )
    accounts = payload["accounts"]
    assert accounts[DEFAULT_ACCOUNT_CFD]["wins"] == 1
    assert accounts[DEFAULT_ACCOUNT_SB]["wins"] == 1
    assert payload["merged"]["net_pnl_gbp"] == 40.0

    assets = {row["asset"]: row for row in payload["asset_breakdown"]}
    assert "DOW" in assets
    assert "GOLD" in assets
    assert assets["DOW"]["pnl_gbp"] == 15.0
    assert assets["GOLD"]["pnl_gbp"] == 25.0
