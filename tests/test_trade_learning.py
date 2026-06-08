"""Tests for v26 unified trade learning (live + ML store + replay)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "v26"))

from research import trade_learning as tl


def test_summarize_replay_historical_counts(tmp_path: Path, monkeypatch) -> None:
    rows = [
        {
            "epic": "X",
            "fired": True,
            "label_3bar": "WIN",
            "setup_key": "BUY|bull|volnormal",
        },
        {
            "epic": "X",
            "fired": True,
            "label_3bar": "LOSS",
            "setup_key": "BUY|bull|volnormal",
        },
        {"epic": "X", "fired": False, "label_3bar": "BREAKEVEN", "setup_key": "WAIT"},
    ]
    report = tl.summarize_replay_historical(rows)
    assert report["total_rows"] == 3
    assert report["fired_rows"] == 2
    assert report["fired_portfolio"]["decided"] == 2
    assert report["fired_portfolio"]["wr"] == 0.5


def test_summarize_ml_store(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "ml_training_store.jsonl"
    store.write_text(
        json.dumps({"instrument": "gold", "gbp_pnl": 10.0, "result": "WIN"})
        + "\n"
        + json.dumps({"instrument": "gold", "gbp_pnl": -5.0, "result": "LOSS"})
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tl, "_ml_store_path", lambda: store)
    report = tl.summarize_ml_store()
    assert report["total_records"] == 2
    assert report["portfolio"]["n"] == 2
    assert report["portfolio"]["wr"] == 0.5


def test_ml_readiness_uses_replay_decided(monkeypatch) -> None:
    monkeypatch.setattr(
        tl,
        "_load_v26_config",
        lambda: {"ml_veto": {"min_labelled_rows": 100, "enabled": False}},
    )
    live = {"portfolio": {"n": 5}}
    ml_store = {"labelled_records": 10}
    replay = {"fired_portfolio": {"decided": 90}}
    ready = tl.ml_readiness(live=live, ml_store=ml_store, replay=replay)
    assert ready["combined_proxy"] == 105
    assert ready["ready_for_ml_veto"] is True


def test_build_trade_learning_report(monkeypatch) -> None:
    monkeypatch.setattr(
        tl,
        "summarize_live_fills",
        lambda **_: {"source": "feeder_fill_close", "portfolio": {"n": 0}},
    )
    monkeypatch.setattr(
        tl,
        "summarize_ml_store",
        lambda: {"source": "ml_training_store", "total_records": 0},
    )
    monkeypatch.setattr(
        tl,
        "summarize_replay_historical",
        lambda rows=None: {
            "source": "replay_results_jsonl",
            "total_rows": 1000,
            "fired_rows": 200,
            "fired_portfolio": {"decided": 150, "wr": 0.52},
        },
    )
    report = tl.build_trade_learning_report()
    assert report["ok"] is True
    assert report["s4_ml_meta_status"] in ("not_wired", "wired", "pending_retrain")
    assert len(report["learning_tips"]) >= 1
