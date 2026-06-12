"""Tests for trading blocker reset helpers."""

from __future__ import annotations

from data.learning_store import LearningStore


def _insert_loss(store: LearningStore, closed_at: str) -> None:
    store.conn.execute(
        """
        INSERT INTO trades (
            opened_at, closed_at, market, epic, side, entry, exit,
            size, pnl_points, result, dry_run, ig_pnl_currency, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'strategy')
        """,
        (
            closed_at,
            closed_at,
            "GBP/USD",
            "CS.D.GBPUSD.CFD.IP",
            "BUY",
            1.0,
            0.9,
            1.0,
            -1.0,
            "LOSS",
            -10.0,
        ),
    )
    store.conn.commit()


def test_archive_trailing_loss_streak_clears_circuit_count(tmp_path) -> None:
    db = tmp_path / "learning.db"
    store = LearningStore(db)
    for i in range(5):
        _insert_loss(store, f"2026-06-11 10:0{i}:00")
    assert store.consecutive_losses(6) == 5
    store.set_runtime_state("circuit_breaker_tripped_at", "2026-06-12T12:00:00")

    info = store.reset_consecutive_loss_streak(reason="pytest")
    assert info["archived_streak"] == 5
    assert store.consecutive_losses(6) == 0
    assert store.get_runtime_state("circuit_breaker_tripped_at") is None
