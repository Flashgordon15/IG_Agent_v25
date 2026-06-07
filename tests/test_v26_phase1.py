"""v26 Phase 1 — S1 shadow, feature store, expectancy."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from shadow.runner import process_day_events, reset_shadow_state, shadow_dir
from strategies.s1_rules_v25 import S1RulesV25


@pytest.fixture
def lake_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    events = tmp_path / "data_lake" / "events"
    events.mkdir(parents=True)
    monkeypatch.setattr("ingest.lake_reader._project_root", lambda: tmp_path)
    monkeypatch.setattr("shadow.runner._project_root", lambda: tmp_path)
    import research.feature_store as fs

    monkeypatch.setattr(fs, "_project_root", lambda: tmp_path)
    reset_shadow_state()
    yield tmp_path, events
    reset_shadow_state()


def test_s1_mirrors_signal_eval_would_fire() -> None:
    s1 = S1RulesV25()
    row = {
        "event_type": "signal_eval",
        "ts": "2026-06-08T10:00:00Z",
        "epic": "IX.D.NASDAQ.IFM.IP",
        "market": "US Tech 100",
        "session": "us_afternoon",
        "payload": {
            "direction": "BUY",
            "adjusted_score": 88.0,
            "would_fire": True,
            "setup_key": "BUY|bull",
        },
    }
    intent = s1.evaluate_feeder_event(row)
    assert intent is not None
    assert intent.would_trade is True
    assert intent.strategy_id == "S1_rules_v25"


def test_shadow_runner_writes_jsonl(lake_layout) -> None:
    tmp_path, events_dir = lake_layout
    day = "2026-06-08"
    (events_dir / f"{day}.jsonl").write_text(
        json.dumps(
            {
                "event_type": "signal_eval",
                "ts": "2026-06-08T10:00:00Z",
                "epic": "IX.D.NIKKEI.IFM.IP",
                "payload": {
                    "direction": "SELL",
                    "adjusted_score": 90.0,
                    "would_fire": True,
                    "setup_key": "SELL|bear",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    from ingest.lake_reader import iter_events

    evs = list(iter_events(day=day))
    n = process_day_events(evs, day=day, clear_seen=True)
    assert n == 1
    shadow_path = shadow_dir() / f"{day}.jsonl"
    assert shadow_path.is_file()
    row = json.loads(shadow_path.read_text().strip())
    assert row["event_type"] == "shadow_intent"
    assert row["strategy_id"] == "S1_rules_v25"


def test_feature_store_build(lake_layout) -> None:
    tmp_path, events_dir = lake_layout
    day = "2026-06-08"
    lines = [
        {
            "event_type": "signal_eval",
            "ts": "t1",
            "epic": "E1",
            "market": "M",
            "session": "s",
            "payload": {"direction": "BUY", "would_fire": True, "setup_key": "k"},
        },
        {
            "event_type": "fill_close",
            "ts": "t2",
            "epic": "E1",
            "payload": {"setup_key": "k", "pnl_gbp": 25.0, "result": "WIN"},
        },
    ]
    (events_dir / f"{day}.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n",
        encoding="utf-8",
    )
    from research.feature_store import build_day

    written = build_day(day)
    assert written["signals"].is_file()
    assert written["fills"].is_file()
    meta = json.loads(written["meta"].read_text())
    assert meta["signals"] == 1
    assert meta["fills"] == 1


def test_expectancy_from_fills(lake_layout) -> None:
    from datetime import datetime, timezone

    tmp_path, events_dir = lake_layout
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fills = []
    for i, pnl in enumerate([10.0, -5.0, 15.0, -8.0]):
        fills.append(
            {
                "event_type": "fill_close",
                "ts": f"t{i}",
                "epic": "E1",
                "payload": {
                    "setup_key": "BUY|test",
                    "pnl_gbp": pnl,
                    "result": "WIN" if pnl > 0 else "LOSS",
                },
            }
        )
    (events_dir / f"{day}.jsonl").write_text(
        "\n".join(json.dumps(x) for x in fills) + "\n",
        encoding="utf-8",
    )
    from expectancy.engine import collect_fills, compute_setup_stats, portfolio_summary

    rows = collect_fills(days=7)
    assert len(rows) == 4
    pf = portfolio_summary(rows)
    assert pf["n"] == 4
    assert pf["total_pnl_gbp"] == 12.0
    setups = compute_setup_stats(rows)
    assert setups[0].setup_key == "BUY|test"
    assert setups[0].n == 4
