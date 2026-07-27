"""Offline stamp-path proof: LearningStore fill→close → performance_journal.

SIM/shadow does not exercise this path. This test drives a real open row through
``LearningStore.close_trade`` (the DEMO/live journal bridge) and asserts the
repaired stamps land on the daily journal + ml_trade_outcomes:
HoldSec, MlScoreAtEntry, Style, and epic (outcomes plane — journal CSV has no Epic col).

Also exercises TradeManager stop-hit → close_trade → journal for the in-process
manager close path (still no broker).
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from config_test_helpers import trade_manager_test_config as _cfg
from data.learning_store import LearningStore
from data.ml_training_store import (
    MLTrainingStore,
    reset_ml_training_store_for_tests,
    set_store_path_for_tests,
)
from data.models import Quote
from diagnostics.ml_trade_outcomes import reset_ml_trade_outcomes_for_tests
from diagnostics.performance_journal import (
    enable_sync_mode_for_tests,
    reset_performance_journal_for_tests,
)


DEAL = "DIAAAAXSTAMPPROOF01"
EPIC = "IX.D.DOW.IFM.IP"
ML_SCORE = 0.734
HOLD_SEC = 180.0


@pytest.fixture(autouse=True)
def _reset_journal_planes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    reset_performance_journal_for_tests()
    enable_sync_mode_for_tests(True)
    reset_ml_trade_outcomes_for_tests()
    reset_ml_training_store_for_tests()

    journal = tmp_path / "daily_journal.csv"
    outcomes = tmp_path / "ml_trade_outcomes.jsonl"
    monkeypatch.setattr(
        "diagnostics.performance_journal.journal_path", lambda: journal
    )
    monkeypatch.setattr(
        "diagnostics.ml_trade_outcomes.outcomes_path", lambda: outcomes
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

    set_store_path_for_tests(tmp_path / "ml_training.jsonl")
    monkeypatch.setattr(
        "data.ml_training_store._buffer_path",
        lambda: tmp_path / "state" / "ml_entry_buffer.json",
    )

    yield {"journal": journal, "outcomes": outcomes}

    reset_performance_journal_for_tests()
    reset_ml_trade_outcomes_for_tests()
    reset_ml_training_store_for_tests()


def _insert_open(
    store: LearningStore,
    *,
    opened_at: str,
    deal_id: str = DEAL,
    confidence: float | None = None,
) -> int:
    store.conn.execute(
        """
        INSERT INTO trades(
            opened_at, market, epic, side, entry, size, stop, target,
            confidence, adjusted_confidence, setup_key, dry_run,
            deal_reference, ig_deal_id, notes,
            account_id, product_type, engine_origin
        )
        VALUES(
            ?, 'Wall Street', ?, 'BUY', 52000.0, 0.5,
            51980.0, 52040.0, ?, ?, 'TEST|stamp', 0,
            ?, ?, '',
            'Z6BAH4', 'CFD', 'QUANT_SNIPER'
        )
        """,
        (
            opened_at,
            EPIC,
            confidence,
            confidence,
            f"REF-{deal_id[-6:]}",
            deal_id,
        ),
    )
    store.conn.commit()
    return int(store.conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def test_learning_store_close_stamps_hold_ml_style_epic(
    tmp_path: Path, _reset_journal_planes: dict
) -> None:
    """Fill buffer + LearningStore.close_trade must stamp journal repair fields."""
    journal: Path = _reset_journal_planes["journal"]
    outcomes: Path = _reset_journal_planes["outcomes"]
    store = LearningStore(str(tmp_path / "learning.sqlite3"))
    store._emit_ml_training_close = lambda *a, **k: None  # type: ignore[method-assign]
    store._rebuild_stats_for = lambda *a, **k: None  # type: ignore[method-assign]

    opened = datetime(2026, 7, 25, 18, 0, 0, tzinfo=timezone.utc)
    closed = opened + timedelta(seconds=HOLD_SEC)
    tid = _insert_open(
        store,
        opened_at=opened.strftime("%Y-%m-%d %H:%M:%S"),
        confidence=None,  # force MlScoreAtEntry recovery from entry buffer
    )

    ml_store = MLTrainingStore(tmp_path / "ml_training.jsonl")
    ml_store.record_entry(
        DEAL,
        {
            "ml_score_at_entry": ML_SCORE,
            "p_success": ML_SCORE,
            "market_regime": "TRENDING",
            "instrument": EPIC,
            "entry_time": opened.isoformat(),
        },
    )

    store.close_trade(
        tid,
        exit_price=52010.0,
        pnl_points=10.0,
        result="WIN",
        notes="weekend_stamp_proof",
        ig_pnl_currency=5.0,
        closed_at=closed.strftime("%Y-%m-%d %H:%M:%S"),
    )

    assert journal.is_file(), "performance_journal CSV missing after LearningStore close"
    rows = list(csv.DictReader(journal.open(encoding="utf-8", newline="")))
    match = [r for r in rows if r.get("DealID") == DEAL]
    assert match, f"DealID {DEAL} not in journal; rows={rows}"
    row = match[0]

    assert float(row["HoldSec"]) == pytest.approx(HOLD_SEC, abs=1.0)
    assert float(row["MlScoreAtEntry"]) == pytest.approx(ML_SCORE)
    assert row.get("Style") in ("scalp", "macro", "long", "supervised_exit")
    assert row.get("EngineOrigin") == "QUANT_SNIPER"
    assert float(row["RealizedPnL_GBP"]) == pytest.approx(5.0)
    # MarketRegime must never be blank — entry buffer / snapshot / UNKNOWN fallback.
    assert (row.get("MarketRegime") or "").strip(), "MarketRegime blank on journal close"
    assert row["MarketRegime"] == "TRENDING"

    # Epic is stamped on the outcomes plane (journal CSV schema has no Epic column).
    assert outcomes.is_file()
    outcome_lines = [
        json.loads(x) for x in outcomes.read_text(encoding="utf-8").splitlines() if x
    ]
    assert outcome_lines, "ml_trade_outcomes empty after close"
    assert outcome_lines[0]["epic"] == EPIC
    assert outcome_lines[0]["ml_score"] == pytest.approx(ML_SCORE)
    assert float(outcome_lines[0].get("hold_sec") or outcome_lines[0].get("hold_duration_seconds") or 0) == pytest.approx(
        HOLD_SEC, abs=1.0
    )
    assert (outcome_lines[0].get("market_regime") or outcome_lines[0].get("regime") or "").strip()
    assert (outcome_lines[0].get("market_regime") or outcome_lines[0].get("regime")) == "TRENDING"

    closed_row = store.conn.execute(
        "SELECT epic, closed_at FROM trades WHERE id=?", (tid,)
    ).fetchone()
    assert closed_row["epic"] == EPIC
    assert closed_row["closed_at"]


def test_trade_manager_stop_close_stamps_journal(
    tmp_path: Path, _reset_journal_planes: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TradeManager stop-hit → LearningStore.close_trade → journal stamps."""
    # Lazy import avoids circular import at collection time under some runners.
    from trading.trade_manager import TradeManager

    journal: Path = _reset_journal_planes["journal"]
    outcomes: Path = _reset_journal_planes["outcomes"]
    store = LearningStore(str(tmp_path / "learning_tm.sqlite3"))
    store._emit_ml_training_close = lambda *a, **k: None  # type: ignore[method-assign]
    store._rebuild_stats_for = lambda *a, **k: None  # type: ignore[method-assign]

    opened = datetime(2026, 7, 25, 19, 0, 0, tzinfo=timezone.utc)
    deal = "DIAAAAXSTAMPTM0001"
    tid = _insert_open(
        store,
        opened_at=opened.strftime("%Y-%m-%d %H:%M:%S"),
        deal_id=deal,
        confidence=0.81,  # row-carried score → MlScoreAtEntry
    )
    store.conn.execute(
        "UPDATE trades SET stop=51980.0, entry=52000.0, side='BUY' WHERE id=?",
        (tid,),
    )
    store.conn.commit()

    ml_store = MLTrainingStore(tmp_path / "ml_training.jsonl")
    ml_store.record_entry(
        deal,
        {
            "ml_score_at_entry": 0.81,
            "entry_time": opened.isoformat(),
            "instrument": EPIC,
        },
    )

    # skip_ig_synced_exits=True would skip any row with ig_deal_id (broker-managed
    # exit path). Use False so the local stop-hit still journals DIAAAA* deals.
    mgr = TradeManager(_cfg(), store, skip_ig_synced_exits=False)
    monkeypatch.setattr(TradeManager, "_is_friday_close_window", lambda self: False)

    mgr.update_from_quote(
        "Wall Street",
        EPIC,
        Quote(datetime.now(timezone.utc), 51970.0, 51971.0),
    )

    closed = store.conn.execute(
        "SELECT closed_at, result, epic FROM trades WHERE id=?", (tid,)
    ).fetchone()
    assert closed is not None and closed["closed_at"], "TradeManager did not close"
    assert closed["epic"] == EPIC
    assert journal.is_file(), "journal missing after TradeManager stop close"
    rows = list(csv.DictReader(journal.open(encoding="utf-8", newline="")))
    match = [r for r in rows if deal in (r.get("DealID") or "")]
    assert match, f"DealID {deal} missing from journal after TM close; rows={rows}"
    row = match[0]
    assert row.get("MlScoreAtEntry") not in ("", None)
    assert float(row["MlScoreAtEntry"]) == pytest.approx(0.81, abs=0.02)
    assert row.get("HoldSec") not in ("", None)
    assert float(row["HoldSec"]) >= 0.0
    assert (row.get("Style") or "") in ("scalp", "macro", "long", "supervised_exit")
    # Unresolved regime must stay blank — "UNKNOWN" was a placeholder that
    # previously fooled autopsy stamp gates (see stamp_provenance).
    assert (row.get("MarketRegime") or "").strip() == ""

    assert outcomes.is_file()
    outcome_lines = [
        json.loads(x) for x in outcomes.read_text(encoding="utf-8").splitlines() if x
    ]
    assert any(o.get("epic") == EPIC for o in outcome_lines)
    match_out = [o for o in outcome_lines if o.get("epic") == EPIC]
    assert match_out
    regime_out = (
        match_out[0].get("market_regime") or match_out[0].get("regime") or ""
    ).strip()
    assert regime_out in ("", "UNKNOWN")  # outcomes may still carry legacy UNKNOWN
