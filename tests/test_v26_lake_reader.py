"""v26 lake reader over feeder events."""

from __future__ import annotations

import json
from pathlib import Path

from ingest.lake_reader import event_utc_day, summarize_day, utc_today


def test_summarize_day(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "data_lake" / "events"
    root.mkdir(parents=True)
    day = "2026-06-07"
    path = root / f"{day}.jsonl"
    rows = [
        {
            "contract_version": "1.0",
            "event_type": "signal_eval",
            "ts": "2026-06-07T12:00:00Z",
            "epic": "IX.D.NASDAQ.IFM.IP",
            "payload": {"would_fire": True},
        },
        {
            "contract_version": "1.0",
            "event_type": "fill_close",
            "ts": "2026-06-07T13:00:00Z",
            "epic": "IX.D.NASDAQ.IFM.IP",
            "payload": {"pnl_gbp": 50.0},
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    import ingest.lake_reader as lr

    monkeypatch.setattr(lr, "_project_root", lambda: tmp_path)
    s = summarize_day(day)
    assert s.total_events == 2
    assert s.signal_evals == 1
    assert s.would_fire == 1
    assert s.fill_closes == 1
    assert s.fill_pnl_gbp == 50.0


def test_event_utc_day_from_ts() -> None:
    row = {"ts": "2026-06-08T01:20:03Z", "event_type": "signal_eval"}
    assert event_utc_day(row) == "2026-06-08"


def test_utc_today_format() -> None:
    assert len(utc_today()) == 10
    assert utc_today()[4] == "-"
