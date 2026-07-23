"""Stability trio: broker-attached journal, unified halt clear, capital preservation."""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from diagnostics.performance_journal import (
    enable_sync_mode_for_tests,
    ensure_broker_attached_exit_journaled,
    journal_has_deal,
    reset_performance_journal_for_tests,
)
from intelligence.target_engine import TargetSeekingEngine, reset_target_engine_for_tests


@pytest.fixture(autouse=True)
def _reset_journal_and_target() -> None:
    reset_performance_journal_for_tests()
    enable_sync_mode_for_tests(True)
    reset_target_engine_for_tests()
    yield
    reset_performance_journal_for_tests()
    reset_target_engine_for_tests()


def test_broker_attached_exit_without_exit_gate_journals_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Broker SL/TP path skips ExitGate but must still write DealID + PnL."""
    journal = tmp_path / "daily_journal.csv"
    monkeypatch.setattr(
        "diagnostics.performance_journal.journal_path", lambda: journal
    )

    wrote = ensure_broker_attached_exit_journaled(
        deal_id="DIAAAAXTESTBROKER01",
        direction="BUY",
        entry_price=51775.6,
        exit_price=51788.5,
        realized_pnl_gbp=6.45,
        engine_origin="broker_attached",
    )
    assert wrote is True
    assert journal_has_deal("DIAAAAXTESTBROKER01") is True
    text = journal.read_text(encoding="utf-8")
    assert "DIAAAAXTESTBROKER01" in text
    assert "6.45" in text
    assert "broker_attached" in text

    # Idempotent — second call must not duplicate.
    wrote2 = ensure_broker_attached_exit_journaled(
        deal_id="DIAAAAXTESTBROKER01",
        realized_pnl_gbp=6.45,
        engine_origin="broker_attached",
    )
    assert wrote2 is False
    rows = list(csv.DictReader(journal.open(encoding="utf-8", newline="")))
    matches = [r for r in rows if r.get("DealID") == "DIAAAAXTESTBROKER01"]
    assert len(matches) == 1


def test_ingest_ig_closed_transaction_journals_broker_attached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IG history ingest (no ExitGate) must land a journal row."""
    from data.learning_store import LearningStore

    db = tmp_path / "learning.sqlite3"
    store = LearningStore(str(db))
    journal = tmp_path / "daily_journal.csv"
    monkeypatch.setattr(
        "diagnostics.performance_journal.journal_path", lambda: journal
    )

    ok = store.ingest_ig_closed_transaction(
        {
            "deal_reference": "DIAAAAXTESTIGINGEST1",
            "ig_deal_id": "DIAAAAXTESTIGINGEST1",
            "ig_pnl_currency": 3.25,
            "result": "WIN",
            "side": "SELL",
            "entry": 51600.0,
            "exit": 51590.0,
            "size": 0.5,
            "market": "Wall Street Cash",
            "epic": "IX.D.DOW.IFM.IP",
            "closed_at": "2026-07-23 12:00:00",
        }
    )
    assert ok is True
    assert journal_has_deal("DIAAAAXTESTIGINGEST1") is True
    assert "3.25" in journal.read_text(encoding="utf-8")


def test_resume_entries_clears_shared_and_lane_halts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Halt in state_cfd must clear on resume along with shared state/."""
    monkeypatch.setenv("IG_TEST_HARNESS", "1")
    monkeypatch.setattr("runtime.desk_dev_controls.data_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "runtime.desk_dev_controls.state_dir", lambda: tmp_path / "state"
    )
    monkeypatch.setattr(
        "runtime.desk_dev_controls._is_production_state_path", lambda _p: False
    )

    for sub in ("state", "state_cfd", "state_sb"):
        d = tmp_path / sub
        d.mkdir(parents=True)
        (d / "entry_halt.json").write_text(
            json.dumps({"active": True, "reason": "unit_lane_halt", "ts": time.time()}),
            encoding="utf-8",
        )
        (d / "trading_paused.json").write_text(
            json.dumps({"active": True, "reason": "unit_lane_halt", "ts": time.time()}),
            encoding="utf-8",
        )

    from runtime.desk_dev_controls import resume_entries

    out = resume_entries(reason="unit_test_resume_lanes")
    assert out["ok"] is True

    for sub in ("state", "state_cfd", "state_sb"):
        halt = json.loads((tmp_path / sub / "entry_halt.json").read_text(encoding="utf-8"))
        paused = json.loads(
            (tmp_path / sub / "trading_paused.json").read_text(encoding="utf-8")
        )
        assert halt.get("active") is False, sub
        assert paused.get("active") is False, sub


def test_capital_preservation_no_false_halt_on_inflated_journal() -> None:
    """Inflated store/journal alone must not pause when REST is bound."""
    engine = TargetSeekingEngine(target_daily_gbp=1000.0, enabled=True)
    store = MagicMock()
    engine.bind_store(store)

    rest = MagicMock()
    rest.maybe_refresh_account_summary.return_value = {"balance": 10360.0}
    engine.bind_rest_client(rest)
    engine.mark_session_start(10000.0)  # broker +£360 << £1000

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "system.daily_loss_policy.effective_daily_pnl",
            lambda *_a, **_k: 2500.0,
        )
        snap = engine.refresh(force_balance=True)

    assert snap["capital_preservation"] is False
    assert engine.capital_preservation is False


def test_txn_sync_resolves_open_diaaaa_for_journal() -> None:
    """Short IG close refs must journal under open DIAAAA* when learning store matches."""
    from runtime.ig_transaction_sync import IgTransactionSync

    class _Store:
        def __init__(self) -> None:
            import sqlite3

            self.conn = sqlite3.connect(":memory:")
            self.conn.row_factory = sqlite3.Row
            self.conn.execute(
                """
                CREATE TABLE trades(
                    id INTEGER PRIMARY KEY,
                    ig_deal_id TEXT,
                    side TEXT,
                    entry REAL,
                    closed_at TEXT
                )
                """
            )
            self.conn.execute(
                """
                INSERT INTO trades(ig_deal_id, side, entry, closed_at)
                VALUES('DIAAAAX58MS5ZAP', 'SELL', 51714.6, '2026-07-23 17:20:00')
                """
            )
            self.conn.commit()

    sync = IgTransactionSync.__new__(IgTransactionSync)
    sync._store = _Store()
    open_id = sync._resolve_open_deal_id_for_journal(
        {
            "ig_deal_id": "58LCVRAR",
            "deal_reference": "58LCVRAR",
            "side": "SELL",
            "entry": 51714.6,
            "closed_at": "2026-07-23 00:00:00",
        }
    )
    assert open_id == "DIAAAAX58MS5ZAP"


def test_capital_preservation_trips_on_broker_confirmed_target() -> None:
    """Real broker session delta at/above target must still engage preservation."""
    engine = TargetSeekingEngine(target_daily_gbp=1000.0, enabled=True)
    store = MagicMock()
    engine.bind_store(store)

    rest = MagicMock()
    rest.maybe_refresh_account_summary.return_value = {"balance": 11100.0}
    engine.bind_rest_client(rest)
    engine.mark_session_start(10000.0)  # broker +£1100

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "system.daily_loss_policy.effective_daily_pnl",
            lambda *_a, **_k: 200.0,  # journal under-reports
        )
        snap = engine.refresh(force_balance=True)

    assert snap["capital_preservation"] is True
    assert engine.capital_preservation is True
    assert engine.mission_accomplished is True
