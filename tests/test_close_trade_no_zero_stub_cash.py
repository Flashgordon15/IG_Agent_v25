"""close_trade must not fake ig_pnl_currency=0 on phantom flat exits."""

from __future__ import annotations

from pathlib import Path

from data.learning_store import LearningStore
from diagnostics.performance_journal import (
    daily_realized_pnl_gbp,
    enable_sync_mode_for_tests,
    reset_performance_journal_for_tests,
)


def _insert_open(store: LearningStore, *, entry: float = 52000.0) -> int:
    """Insert an open row without open_trade side-effect workers."""
    cur = store.conn.execute(
        """
        INSERT INTO trades(
            opened_at, market, epic, side, entry, size, stop, target,
            confidence, adjusted_confidence, setup_key, dry_run,
            deal_reference, ig_deal_id, notes
        )
        VALUES(
            datetime('now'), 'Wall Street', 'IX.D.DOW.IFM.IP', 'BUY', ?, 0.5,
            ?, ?, 0.7, 0.7, 'TEST|unit', 0, 'REFTEST1', 'DIAAAAXTESTSTUB01', ''
        )
        """,
        (entry, entry - 20, entry + 20),
    )
    store.conn.commit()
    return int(cur.lastrowid)


def test_phantom_flat_close_leaves_cash_null_and_skips_journal(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "learning.sqlite3"
    store = LearningStore(str(db))
    journal = tmp_path / "daily_journal.csv"
    reset_performance_journal_for_tests()
    enable_sync_mode_for_tests(True)
    monkeypatch.setattr(
        "diagnostics.performance_journal.journal_path", lambda: journal
    )
    monkeypatch.setattr(
        "system.shutdown_cleanup.notify_position_state_change",
        lambda **kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        "feeder.event_bus.emit_fill_close",
        lambda *a, **k: None,
        raising=False,
    )
    store._emit_ml_training_close = lambda *a, **k: None  # type: ignore[method-assign]
    store._rebuild_stats_for = lambda *a, **k: None  # type: ignore[method-assign]

    tid = _insert_open(store)
    store.close_trade(
        tid,
        exit_price=52000.0,
        pnl_points=0.0,
        result="CANCELLED",
        notes="session_phantom_reconcile",
    )
    row = store.conn.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
    assert row is not None
    assert row["closed_at"] is not None
    assert row["result"] == "CANCELLED"
    assert row["ig_pnl_currency"] is None
    assert abs(float(row["entry"]) - float(row["exit"])) < 1e-9
    if journal.is_file():
        text = journal.read_text(encoding="utf-8")
        assert "DIAAAAXTESTSTUB01" not in text
    assert daily_realized_pnl_gbp(path=journal) == 0.0
    reset_performance_journal_for_tests()


def test_apply_ig_overwrites_zero_stub_and_journals(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "learning.sqlite3"
    store = LearningStore(str(db))
    journal = tmp_path / "daily_journal.csv"
    reset_performance_journal_for_tests()
    enable_sync_mode_for_tests(True)
    monkeypatch.setattr(
        "diagnostics.performance_journal.journal_path", lambda: journal
    )

    tid = _insert_open(store, entry=52100.0)
    store.conn.execute(
        """
        UPDATE trades
        SET closed_at=datetime('now'), exit=entry, pnl_points=0,
            result='BREAKEVEN', ig_pnl_currency=0.0,
            notes=' | session_phantom_reconcile'
        WHERE id=?
        """,
        (tid,),
    )
    store.conn.commit()

    ok = store.apply_ig_transaction_pnl(
        "REFTEST1",
        "DIAAAAXTESTSTUB01",
        4.5,
        "WIN",
        ig_close_deal_id="4ZTESTREF",
        exit_price=52109.0,
        entry_price=52100.0,
        emit_hooks=False,
    )
    assert ok is True
    row = store.conn.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
    assert float(row["ig_pnl_currency"]) == 4.5
    assert row["result"] == "WIN"
    assert float(row["exit"]) == 52109.0
    text = journal.read_text(encoding="utf-8")
    assert "DIAAAAXTESTSTUB01" in text
    assert "4.5" in text
    assert daily_realized_pnl_gbp(path=journal) == 4.5
    reset_performance_journal_for_tests()


def test_local_needs_ig_cash_treats_zero_stub() -> None:
    from runtime.ig_transaction_sync import IgTransactionSync

    assert IgTransactionSync._local_needs_ig_cash(
        {"ig_pnl_currency": None, "entry": 1, "exit": 2}
    )
    assert IgTransactionSync._local_needs_ig_cash(
        {"ig_pnl_currency": 0.0, "entry": 52000.0, "exit": 52000.0}
    )
    assert not IgTransactionSync._local_needs_ig_cash(
        {"ig_pnl_currency": 3.2, "entry": 52000.0, "exit": 52000.0}
    )
    assert not IgTransactionSync._local_needs_ig_cash(
        {"ig_pnl_currency": 0.0, "entry": 52000.0, "exit": 52010.0}
    )
