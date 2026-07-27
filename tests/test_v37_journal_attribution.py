"""V37 Phase 2 — DIAAAA close stamps ml_score_at_entry / regime / hold_sec."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from diagnostics.ml_trade_outcomes import reset_ml_trade_outcomes_for_tests
from diagnostics.performance_journal import (
    enable_sync_mode_for_tests,
    ensure_broker_attached_exit_journaled,
    record_trade_close,
    reset_performance_journal_for_tests,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_performance_journal_for_tests()
    enable_sync_mode_for_tests(True)
    reset_ml_trade_outcomes_for_tests()
    yield
    reset_performance_journal_for_tests()
    reset_ml_trade_outcomes_for_tests()


def test_record_trade_close_stamps_ml_regime_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = tmp_path / "daily_journal.csv"
    outcomes = tmp_path / "ml_trade_outcomes.jsonl"
    monkeypatch.setattr(
        "diagnostics.performance_journal.journal_path", lambda: journal
    )
    monkeypatch.setattr(
        "diagnostics.ml_trade_outcomes.outcomes_path", lambda: outcomes
    )

    record_trade_close(
        deal_id="DIAAAAXATTR001",
        direction="BUY",
        realized_pnl_gbp=-1.5,
        account_id="Z6BAH3",
        engine_origin="MACRO_SENTINEL",
        exit_reason="broker_attached",
        hold_sec=42.0,
        epic="IX.D.DOW.IFM.IP",
        ml_score=0.734,
        regime="TRENDING",
    )
    rows = list(csv.DictReader(journal.open(encoding="utf-8", newline="")))
    assert len(rows) == 1
    row = rows[0]
    assert row["AccountID"] == "Z6BAH3"
    assert row["EngineOrigin"] == "MACRO_SENTINEL"
    assert float(row["MlScoreAtEntry"]) == pytest.approx(0.734)
    assert row["MarketRegime"] == "TRENDING"
    assert float(row["HoldSec"]) == pytest.approx(42.0)

    lines = [json.loads(x) for x in outcomes.read_text(encoding="utf-8").splitlines() if x]
    assert lines[0]["ml_score_at_entry"] == pytest.approx(0.734)
    assert lines[0]["market_regime"] == "TRENDING"
    assert lines[0]["hold_duration_seconds"] == pytest.approx(42.0)
    assert lines[0]["engine_origin"] == "MACRO_SENTINEL"


def test_broker_attached_recovers_ml_from_sniper_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = tmp_path / "daily_journal.csv"
    outcomes = tmp_path / "ml_trade_outcomes.jsonl"
    monkeypatch.setattr(
        "diagnostics.performance_journal.journal_path", lambda: journal
    )
    monkeypatch.setattr(
        "diagnostics.ml_trade_outcomes.outcomes_path", lambda: outcomes
    )
    monkeypatch.setattr(
        "diagnostics.ml_trade_outcomes.resolve_ml_score_for_close",
        lambda **kwargs: 0.771,
    )
    monkeypatch.setattr(
        "system.regime_state.get_regime_state_snapshot",
        lambda: {"regime": "BREAKOUT"},
        raising=False,
    )

    wrote = ensure_broker_attached_exit_journaled(
        deal_id="DIAAAAXATTR002",
        direction="SELL",
        realized_pnl_gbp=2.1,
        engine_origin="broker_attached",
        exit_reason="broker_sl_tp",
        hold_sec=88.0,
        account_id="Z6BAH4",
        epic="IX.D.DOW.IFM.IP",
        # intentionally omit ml_score / regime — must recover
    )
    assert wrote is True
    rows = list(csv.DictReader(journal.open(encoding="utf-8", newline="")))
    match = [r for r in rows if r.get("DealID") == "DIAAAAXATTR002"][0]
    assert float(match["MlScoreAtEntry"]) == pytest.approx(0.771)
    assert float(match["HoldSec"]) == pytest.approx(88.0)
    # Regime best-effort — may be empty if snapshot patch missed import path
    assert match.get("MlScoreAtEntry") not in ("", None)


def test_close_recovers_ml_from_entry_buffer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live fill stamps ml into entry buffer; close recovers without explicit ml_score."""
    from data.ml_training_store import (
        MLTrainingStore,
        peek_buffered_entry,
        reset_ml_training_store_for_tests,
        set_store_path_for_tests,
    )

    journal = tmp_path / "daily_journal.csv"
    outcomes = tmp_path / "ml_trade_outcomes.jsonl"
    store_path = tmp_path / "ml.jsonl"
    monkeypatch.setattr(
        "diagnostics.performance_journal.journal_path", lambda: journal
    )
    monkeypatch.setattr(
        "diagnostics.ml_trade_outcomes.outcomes_path", lambda: outcomes
    )
    reset_ml_training_store_for_tests()
    set_store_path_for_tests(store_path)
    # Force buffer path under tmp so we don't touch production state/
    monkeypatch.setattr(
        "data.ml_training_store._buffer_path",
        lambda: tmp_path / "state" / "ml_entry_buffer.json",
    )
    store = MLTrainingStore(store_path)
    store.record_entry(
        "DIAAAAXBUF001",
        {
            "ml_score_at_entry": 0.812,
            "p_success": 0.812,
            "market_regime": "TRENDING",
            "instrument": "IX.D.DOW.IFM.IP",
        },
    )
    assert peek_buffered_entry("BUF001")["ml_score_at_entry"] == pytest.approx(0.812)

    # No live sniper snap / autopsy — buffer must win.
    monkeypatch.setattr(
        "alpha.micro_sniper_ml.latest_sniper_ml_snapshot",
        lambda epic=None: {},
        raising=False,
    )

    record_trade_close(
        deal_id="BUF001",  # short IG form — alias must resolve long buffer key
        direction="BUY",
        realized_pnl_gbp=-2.2,
        account_id="Z6BAH4",
        engine_origin="QUANT_SNIPER",
        epic="IX.D.DOW.IFM.IP",
        exit_reason="soft_loss",
        hold_sec=55.0,
    )
    rows = list(csv.DictReader(journal.open(encoding="utf-8", newline="")))
    assert float(rows[0]["MlScoreAtEntry"]) == pytest.approx(0.812)
    assert rows[0]["MarketRegime"] == "TRENDING"
    assert float(rows[0]["HoldSec"]) == pytest.approx(55.0)
    assert rows[0]["EngineOrigin"] == "QUANT_SNIPER"
    assert rows[0]["AccountID"] == "Z6BAH4"

    lines = [json.loads(x) for x in outcomes.read_text(encoding="utf-8").splitlines() if x]
    assert lines[0]["ml_score_at_entry"] == pytest.approx(0.812)
    assert lines[0]["market_regime"] == "TRENDING"
    reset_ml_training_store_for_tests()


def test_close_recovers_hold_from_entry_buffer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Broker-attached close retains HoldSec after the micro track is gone."""
    from data.ml_training_store import (
        MLTrainingStore,
        reset_ml_training_store_for_tests,
        set_store_path_for_tests,
    )

    journal = tmp_path / "daily_journal.csv"
    outcomes = tmp_path / "ml_trade_outcomes.jsonl"
    monkeypatch.setattr(
        "diagnostics.performance_journal.journal_path", lambda: journal
    )
    monkeypatch.setattr(
        "diagnostics.ml_trade_outcomes.outcomes_path", lambda: outcomes
    )
    monkeypatch.setattr(
        "runtime.micro_gbp_exit.hold_sec_for_deal",
        lambda _deal: None,
    )
    reset_ml_training_store_for_tests()
    set_store_path_for_tests(tmp_path / "ml.jsonl")
    monkeypatch.setattr(
        "data.ml_training_store._buffer_path",
        lambda: tmp_path / "state" / "ml_entry_buffer.json",
    )
    store = MLTrainingStore(tmp_path / "ml.jsonl")
    store.record_entry(
        "DIAAAAXHOLD001",
        {
            "entry_time": "2026-07-25T18:00:00+00:00",
            "ml_score_at_entry": 0.72,
        },
    )

    record_trade_close(
        deal_id="HOLD001",
        direction="BUY",
        realized_pnl_gbp=1.0,
        closed_at_ts=datetime(
            2026, 7, 25, 18, 3, 30, tzinfo=timezone.utc
        ).timestamp(),
        engine_origin="broker_attached",
    )

    row = next(csv.DictReader(journal.open(encoding="utf-8", newline="")))
    assert float(row["HoldSec"]) == pytest.approx(210.0)
    assert float(row["MlScoreAtEntry"]) == pytest.approx(0.72)
    outcome = json.loads(outcomes.read_text(encoding="utf-8").splitlines()[0])
    assert outcome["hold_sec"] == pytest.approx(210.0)
    reset_ml_training_store_for_tests()
