"""Sovereign simplified accounting payload — journal-backed unit path."""

from __future__ import annotations

from pathlib import Path

import pytest

from diagnostics.performance_journal import (
    enable_sync_mode_for_tests,
    record_trade_close,
    reset_performance_journal_for_tests,
    simplified_accounting_payload,
)


def test_simplified_accounting_last_10_and_today(tmp_path: Path, monkeypatch) -> None:
    reset_performance_journal_for_tests()
    enable_sync_mode_for_tests(True)
    path = tmp_path / "daily_journal.csv"
    monkeypatch.setattr(
        "diagnostics.performance_journal.journal_path", lambda: path
    )
    monkeypatch.setattr(
        "diagnostics.performance_journal._ig_ledger_closed_rows",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "diagnostics.performance_journal._learning_db_closed_rows",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(
        "diagnostics.performance_journal._learning_deal_epic_map",
        lambda: {},
    )
    for i in range(12):
        record_trade_close(
            deal_id=f"DEAL{i:02d}",
            direction="BUY" if i % 2 == 0 else "SELL",
            entry_price=100.0,
            exit_price=101.0,
            realized_pnl_gbp=1.0 + i * 0.1,
        )
    payload = simplified_accounting_payload()
    assert payload["ok"] is True
    assert payload["source"] == "journal_csv"
    assert len(payload["last_10_closed_trades"]) == 10
    assert payload["today_net_realized_pnl_gbp"] > 0
    assert isinstance(payload["daily_history"], list)
    # Day-by-day blotter: newest calendar day first
    dates = [d["date"] for d in payload["daily_history"]]
    assert dates == sorted(dates, reverse=True)
    assert "system_state" in payload
    pm = payload.get("performance_metrics") or {}
    assert "intraday_sharpe" in pm
    assert "profit_factor" in pm
    assert pm.get("true_wins", pm.get("wins", 0)) >= 1
    reset_performance_journal_for_tests()


def test_performance_metrics_exclude_breakeven_noise(
    tmp_path: Path, monkeypatch
) -> None:
    reset_performance_journal_for_tests()
    enable_sync_mode_for_tests(True)
    path = tmp_path / "daily_journal.csv"
    monkeypatch.setattr(
        "diagnostics.performance_journal.journal_path", lambda: path
    )
    monkeypatch.setattr(
        "diagnostics.performance_journal._ig_ledger_closed_rows",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "diagnostics.performance_journal._learning_db_closed_rows",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(
        "diagnostics.performance_journal._learning_deal_epic_map",
        lambda: {},
    )
    closes = [
        (2.5, "WIN"),
        (-1.0, "LOSS"),
        (0.0, "BREAKEVEN"),
        (0.005, "BREAKEVEN"),
        (1.25, "WIN"),
    ]
    for i, (pnl, _tag) in enumerate(closes):
        record_trade_close(
            deal_id=f"BE{i:02d}",
            direction="BUY",
            entry_price=100.0,
            exit_price=100.0 + pnl,
            realized_pnl_gbp=pnl,
        )
    payload = simplified_accounting_payload()
    pm = payload["performance_metrics"]
    assert pm["true_wins"] == 2
    assert pm["true_losses"] == 1
    assert pm["breakeven_excluded"] >= 2
    assert pm["sample_n"] == 3
    assert pm["gross_wins_gbp"] == pytest.approx(3.75, abs=0.01)
    assert pm["gross_losses_gbp"] == pytest.approx(1.0, abs=0.01)
    assert pm["net_true_outcome_gbp"] == pytest.approx(2.75, abs=0.01)
    assert pm["profit_factor"] == pytest.approx(3.75, rel=0.05)
    reset_performance_journal_for_tests()


def test_simplified_accounting_falls_back_to_learning_when_journal_stubs(
    tmp_path: Path, monkeypatch
) -> None:
    reset_performance_journal_for_tests()
    enable_sync_mode_for_tests(True)
    path = tmp_path / "daily_journal.csv"
    monkeypatch.setattr(
        "diagnostics.performance_journal.journal_path", lambda: path
    )
    monkeypatch.setattr(
        "diagnostics.performance_journal._ig_ledger_closed_rows",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "diagnostics.performance_journal._learning_deal_epic_map",
        lambda: {},
    )
    # Journal: phantom £0 stubs (entry==exit)
    for i in range(5):
        record_trade_close(
            deal_id=f"DIAAAASTUB{i:02d}",
            direction="BUY",
            entry_price=52000.0,
            exit_price=52000.0,
            realized_pnl_gbp=0.0,
        )
    learning = [
        {
            "timestamp": f"2026-07-01T12:0{i}:00Z",
            "asset": "DOW",
            "direction": "BUY",
            "net_pnl_gbp": 12.5 - i,
            "deal_id": f"OLD{i}",
            "epic": "IX.D.DOW.IFM.IP",
            "entry": 100.0,
            "exit": 110.0,
            "result": "WIN",
        }
        for i in range(10)
    ]
    monkeypatch.setattr(
        "diagnostics.performance_journal._learning_db_closed_rows",
        lambda **kwargs: learning,
    )
    payload = simplified_accounting_payload()
    assert payload["ok"] is True
    assert payload["source"] == "learning_db"
    assert payload["empty_day"] is True  # today still £0
    assert len(payload["last_10_closed_trades"]) == 10
    assert all(abs(t["net_pnl_gbp"]) > 0 for t in payload["last_10_closed_trades"])
    assert payload["last_10_closed_trades"][0]["asset"] == "DOW"
    reset_performance_journal_for_tests()


def test_simplified_accounting_cache_ttl_avoids_empty_flash(
    tmp_path: Path, monkeypatch
) -> None:
    """Cached learning_db payload must stick across rapid polls."""
    reset_performance_journal_for_tests()
    enable_sync_mode_for_tests(True)
    path = tmp_path / "daily_journal.csv"
    monkeypatch.setattr(
        "diagnostics.performance_journal.journal_path", lambda: path
    )
    monkeypatch.setattr(
        "diagnostics.performance_journal._ig_ledger_closed_rows",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "diagnostics.performance_journal._learning_deal_epic_map",
        lambda: {},
    )
    learning = [
        {
            "timestamp": "2026-07-02T12:00:00Z",
            "asset": "DOW",
            "direction": "BUY",
            "net_pnl_gbp": 9.5,
            "deal_id": "CASH1",
            "epic": "IX.D.DOW.IFM.IP",
            "entry": 100.0,
            "exit": 110.0,
            "result": "WIN",
        }
    ]
    monkeypatch.setattr(
        "diagnostics.performance_journal._learning_db_closed_rows",
        lambda **kwargs: learning,
    )
    first = simplified_accounting_payload()
    assert first["source"] == "learning_db"
    # Simulate journal growing £0 stubs mid-poll — cache must ignore
    for i in range(3):
        record_trade_close(
            deal_id=f"STUB{i}",
            direction="BUY",
            entry_price=1.0,
            exit_price=1.0,
            realized_pnl_gbp=0.0,
        )
    monkeypatch.setattr(
        "diagnostics.performance_journal._learning_db_closed_rows",
        lambda **kwargs: [],
    )
    second = simplified_accounting_payload()
    assert second["source"] == "learning_db"
    assert second["last_10_closed_trades"][0]["net_pnl_gbp"] == 9.5
    reset_performance_journal_for_tests()
