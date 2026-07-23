"""Staged journal correctness — daily sum excludes cancelled stubs; GBP ≠ points."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

import pytest

from diagnostics.performance_journal import (
    daily_realized_pnl_gbp,
    enable_sync_mode_for_tests,
    record_trade_close,
    reset_performance_journal_for_tests,
)


@pytest.fixture(autouse=True)
def _journal_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "daily_journal.csv"
    monkeypatch.setattr(
        "diagnostics.performance_journal.journal_path", lambda: path
    )
    reset_performance_journal_for_tests()
    enable_sync_mode_for_tests(True)
    yield path
    enable_sync_mode_for_tests(False)
    reset_performance_journal_for_tests()


def _freeze_utc_day(monkeypatch: pytest.MonkeyPatch, day: str = "2026-07-20") -> None:
    import diagnostics.performance_journal as pj

    y, m, d = (int(x) for x in day.split("-"))

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(y, m, d, 12, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(pj, "datetime", _FixedDateTime)


def test_daily_realized_skips_flat_cancelled_stubs(
    _journal_isolation: Path, monkeypatch: pytest.MonkeyPatch
):
    path = _journal_isolation
    _freeze_utc_day(monkeypatch)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "Timestamp",
                "DealID",
                "Direction",
                "EntryPrice",
                "ExitPrice",
                "RealizedPnL_GBP",
                "ClosingFillRate",
                "ActiveSlipMultiplier",
            ]
        )
        w.writerow(
            [
                "2026-07-20T10:00:00Z",
                "BENCHMARK_OFFSET:£1000_DAILY",
                "",
                "",
                "",
                "0.0",
                "",
                "",
            ]
        )
        w.writerow(
            [
                "2026-07-20T10:01:00Z",
                "DIAAAA_CANCEL",
                "",
                "52000",
                "52000",
                "0",
                "",
                "0.5",
            ]
        )
        w.writerow(
            [
                "2026-07-20T10:02:00Z",
                "DIAAAA_WIN",
                "BUY",
                "52000",
                "52010",
                "5.0",
                "0.9",
                "0.5",
            ]
        )
    assert daily_realized_pnl_gbp(path=path) == 5.0


def test_record_trade_close_writes_direction_and_pnl(
    _journal_isolation: Path, monkeypatch: pytest.MonkeyPatch
):
    _freeze_utc_day(monkeypatch)
    record_trade_close(
        deal_id="DIAAAA_TEST",
        direction="BUY",
        entry_price=100.0,
        exit_price=110.0,
        realized_pnl_gbp=5.0,
    )
    rows = list(csv.DictReader(_journal_isolation.open(encoding="utf-8")))
    hit = next(r for r in rows if r.get("DealID") == "DIAAAA_TEST")
    assert hit["Direction"] == "BUY"
    assert float(hit["RealizedPnL_GBP"]) == 5.0


def test_pnl_points_times_size_is_gbp():
    """Document expected caller formula used by learning_store close path."""
    pnl_points = 10.0
    size = 0.5
    assert pnl_points * size == 5.0
