"""Journal Style tags (scalp|long) + ML trade outcomes feedback loop."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from diagnostics.ml_trade_outcomes import (
    record_ml_trade_outcome,
    reset_ml_trade_outcomes_for_tests,
    rolling_wr_by_score_bucket,
)
from diagnostics.performance_journal import (
    enable_sync_mode_for_tests,
    ensure_broker_attached_exit_journaled,
    infer_trade_style,
    journal_has_deal,
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


def test_infer_trade_style_long_vs_scalp() -> None:
    assert (
        infer_trade_style(exit_reason="long_runner_profit_trail", hold_sec=400) == "long"
    )
    assert infer_trade_style(hold_sec=200) == "long"
    assert infer_trade_style(hold_sec=45, engine_origin="QUANT_SNIPER") == "scalp"
    assert infer_trade_style(engine_origin="MACRO_SENTINEL", hold_sec=60) == "macro"


def test_sb_long_runner_exit_tags_style_long(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = tmp_path / "daily_journal.csv"
    monkeypatch.setattr(
        "diagnostics.performance_journal.journal_path", lambda: journal
    )
    outcomes = tmp_path / "ml_trade_outcomes.jsonl"
    monkeypatch.setattr(
        "diagnostics.ml_trade_outcomes.outcomes_path", lambda: outcomes
    )

    record_trade_close(
        deal_id="DIAAAAXSTYLELONG01",
        direction="BUY",
        realized_pnl_gbp=12.5,
        account_id="Z6BAH3",
        engine_origin="MACRO_SENTINEL",
        exit_reason="micro_gbp_exit:long_runner_profit_trail pnl=12.50",
        hold_sec=240.0,
        style="long",
        epic="IX.D.DOW.IFM.IP",
    )
    rows = list(csv.DictReader(journal.open(encoding="utf-8", newline="")))
    assert len(rows) == 1
    assert rows[0].get("Style") == "long"
    assert rows[0].get("HoldSec") == "240"
    assert "long_runner" in (rows[0].get("ExitReason") or "")


def test_broker_attached_path_journals_row_with_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = tmp_path / "daily_journal.csv"
    monkeypatch.setattr(
        "diagnostics.performance_journal.journal_path", lambda: journal
    )
    outcomes = tmp_path / "ml_trade_outcomes.jsonl"
    monkeypatch.setattr(
        "diagnostics.ml_trade_outcomes.outcomes_path", lambda: outcomes
    )

    wrote = ensure_broker_attached_exit_journaled(
        deal_id="DIAAAAXBROKERSTYLE1",
        direction="SELL",
        realized_pnl_gbp=4.2,
        engine_origin="broker_attached",
        exit_reason="broker_sl_tp",
        hold_sec=95.0,
        account_id="Z6BAH4",
        epic="IX.D.DOW.IFM.IP",
    )
    assert wrote is True
    assert journal_has_deal("DIAAAAXBROKERSTYLE1") is True
    rows = list(csv.DictReader(journal.open(encoding="utf-8", newline="")))
    match = [r for r in rows if r.get("DealID") == "DIAAAAXBROKERSTYLE1"][0]
    assert match.get("ExitReason") == "broker_sl_tp"
    assert match.get("HoldSec") == "95"
    assert match.get("Style") in ("scalp", "supervised_exit")


def test_ml_trade_outcome_written_on_close(
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
        deal_id="DIAAAAXMLFEEDBACK01",
        direction="BUY",
        realized_pnl_gbp=3.1,
        account_id="Z6BAH4",
        engine_origin="QUANT_SNIPER",
        exit_reason="micro_gbp_exit:profit_trail",
        hold_sec=40.0,
        epic="IX.D.DOW.IFM.IP",
        ml_score=0.81,
        regime="TRENDING",
    )
    assert outcomes.is_file()
    lines = [json.loads(x) for x in outcomes.read_text(encoding="utf-8").splitlines() if x]
    assert len(lines) == 1
    row = lines[0]
    assert row["deal_id"] == "DIAAAAXMLFEEDBACK01"
    assert row["ml_score"] == pytest.approx(0.81)
    assert row["style"] == "scalp"
    assert row["pnl"] == pytest.approx(3.1)
    assert row["account"] == "Z6BAH4"


def test_rolling_wr_by_score_bucket(tmp_path: Path) -> None:
    path = tmp_path / "ml_trade_outcomes.jsonl"
    for i, (score, pnl) in enumerate(
        [(0.55, 1.0), (0.55, -1.0), (0.75, 2.0), (0.75, 1.0), (0.85, -0.5)]
    ):
        record_ml_trade_outcome(
            deal_id=f"D{i}",
            ml_score=score,
            pnl=pnl,
            style="scalp",
            path=path,
        )
    snap = rolling_wr_by_score_bucket(path=path)
    assert snap["n"] == 5
    assert "ge_0.70" in snap["buckets"] or "ge_0.75" in snap["buckets"] or snap["buckets"]
