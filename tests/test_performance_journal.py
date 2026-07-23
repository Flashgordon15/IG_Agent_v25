"""Performance journal CSV — non-blocking milestone telemetry."""

from __future__ import annotations

from pathlib import Path

from diagnostics.fill_rate_monitor import (
    get_fill_rate_monitor,
    reset_fill_rate_monitor_for_tests,
)
from diagnostics.performance_journal import (
    daily_realized_pnl_gbp,
    enable_sync_mode_for_tests,
    journal_path,
    record_flat_session,
    record_trade_close,
    reset_performance_journal_for_tests,
)


def test_trade_close_appends_csv(tmp_path: Path, monkeypatch) -> None:
    reset_performance_journal_for_tests()
    reset_fill_rate_monitor_for_tests()
    enable_sync_mode_for_tests(True)
    path = tmp_path / "daily_journal.csv"
    monkeypatch.setattr(
        "diagnostics.performance_journal.journal_path", lambda: path
    )
    mon = get_fill_rate_monitor(sync_mode=True)
    mon.record_attempt()
    mon.record_fill()
    record_trade_close(
        deal_id="DIAAAAXY5H2RZAR",
        direction="BUY",
        entry_price=52388.7,
        exit_price=52400.0,
        realized_pnl_gbp=4.5,
    )
    text = path.read_text(encoding="utf-8")
    assert "Timestamp,DealID,Direction" in text
    assert "DIAAAAXY5H2RZAR" in text
    assert "BUY" in text
    assert "4.5" in text
    assert daily_realized_pnl_gbp(path=path) == 4.5
    reset_performance_journal_for_tests()
    reset_fill_rate_monitor_for_tests()


def test_flat_session_row(tmp_path: Path, monkeypatch) -> None:
    reset_performance_journal_for_tests()
    enable_sync_mode_for_tests(True)
    path = tmp_path / "daily_journal.csv"
    monkeypatch.setattr(
        "diagnostics.performance_journal.journal_path", lambda: path
    )
    record_flat_session(reason="unit")
    assert "FLAT_SESSION:unit" in path.read_text(encoding="utf-8")
    reset_performance_journal_for_tests()


def test_benchmark_offset_and_milestone_payload(tmp_path: Path, monkeypatch) -> None:
    reset_performance_journal_for_tests()
    path = tmp_path / "daily_journal.csv"
    monkeypatch.setattr(
        "diagnostics.performance_journal.journal_path", lambda: path
    )
    from diagnostics.performance_journal import (
        ensure_benchmark_offset,
        milestone_progress_payload,
        BENCHMARK_DEAL_ID,
    )

    ensure_benchmark_offset(path=path)
    assert BENCHMARK_DEAL_ID in path.read_text(encoding="utf-8")
    payload = milestone_progress_payload()
    assert payload["daily_milestone_gbp"] == 1000.0
    assert payload["daily_realized_pnl_gbp"] == 0.0
    assert payload["progress_pct"] == 0.0
    reset_performance_journal_for_tests()
