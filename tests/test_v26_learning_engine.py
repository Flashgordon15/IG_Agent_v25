"""Tests for v26 learning engine and L1 certification."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "v26"))

from research.bar_analyzer import analyze_bars, bar_report_to_dict
from research.l1_certification import evaluate_l1
from research.learning_engine import build_learning_snapshot


def test_bar_analyzer_detects_s2_range(tmp_path: Path, monkeypatch) -> None:
    events = tmp_path / "events"
    events.mkdir()
    day = "2026-06-08"
    bar = {
        "event_type": "bar_close",
        "ts": "2026-06-08T14:00:00Z",
        "epic": "IX.D.NASDAQ.IFM.IP",
        "session": "london_us_overlap",
        "payload": {"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.45},
    }
    (events / f"{day}.jsonl").write_text(json.dumps(bar) + "\n", encoding="utf-8")

    import ingest.lake_reader as lr

    monkeypatch.setattr(lr, "events_dir", lambda: events)
    report = bar_report_to_dict(analyze_bars(day=day))
    assert report["total_bars"] == 1
    assert report["s2_eligible"] == 1
    assert report["s2_would_trade"] == 1


def test_l1_insufficient_with_two_days(tmp_path: Path, monkeypatch) -> None:
    events = tmp_path / "events"
    events.mkdir()
    for day, pnl in (("2026-06-07", 50.0), ("2026-06-08", -30.0)):
        rows = [
            {
                "event_type": "fill_close",
                "ts": f"{day}T12:00:00Z",
                "epic": "IX.D.NIKKEI.IFM.IP",
                "payload": {"pnl_gbp": pnl, "setup_key": "SELL|x"},
            }
        ]
        (events / f"{day}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n",
            encoding="utf-8",
        )

    import ingest.lake_reader as lr

    monkeypatch.setattr(lr, "events_dir", lambda: events)
    l1 = evaluate_l1(["2026-06-08", "2026-06-07"])
    assert l1["status"] == "INSUFFICIENT"
    assert l1["days_available"] == 2
    assert l1["metrics"]["total_pnl_gbp"] == 20.0


def test_learning_snapshot_has_focus(tmp_path: Path, monkeypatch) -> None:
    events = tmp_path / "events"
    events.mkdir()
    day = "2026-06-08"
    (events / f"{day}.jsonl").write_text(
        json.dumps(
            {
                "event_type": "signal_eval",
                "ts": f"{day}T14:00:00Z",
                "epic": "IX.D.NASDAQ.IFM.IP",
                "payload": {"adjusted_score": 72.0, "would_fire": False},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    import ingest.lake_reader as lr
    import research.learning_engine as le

    monkeypatch.setattr(lr, "events_dir", lambda: events)
    monkeypatch.setattr(le, "list_event_days", lambda max_days=14: [day])
    monkeypatch.setattr(le, "replay_days", lambda days, thresholds=None: [])
    snap = build_learning_snapshot(days=[day])
    assert snap["latest_day"] == day
    assert isinstance(snap.get("learning_focus"), list)
    assert len(snap["learning_focus"]) >= 1
