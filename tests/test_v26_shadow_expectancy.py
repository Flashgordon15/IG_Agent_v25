"""Tests for shadow expectancy near-miss analysis."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "v26"))

from research.shadow_expectancy import analyze_near_miss, near_miss_to_dict


def test_near_miss_counts_blocked_band(tmp_path: Path, monkeypatch) -> None:
    events = tmp_path / "events"
    events.mkdir()
    day = "2026-06-08"
    (events / f"{day}.jsonl").write_text(
        json.dumps(
            {
                "event_type": "signal_eval",
                "ts": "2026-06-08T14:00:00Z",
                "epic": "IX.D.NASDAQ.IFM.IP",
                "payload": {
                    "adjusted_score": 72.0,
                    "direction": "BUY",
                    "would_fire": False,
                    "setup_key": "BUY|bull|x",
                    "gates_passed": ["session_open"],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    import ingest.lake_reader as lr
    import research.shadow_expectancy as se

    ts = datetime(2026, 6, 8, 14, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(lr, "events_dir", lambda: events)
    monkeypatch.setattr(se, "_load_setup_e_gbp", lambda: {"BUY|bull|x": 12.5})
    monkeypatch.setattr(
        se,
        "_load_shadow_would_trade_index",
        lambda _day: {"IX.D.NASDAQ.IFM.IP": [ts]},
    )

    analysis = analyze_near_miss(day=day)
    d = near_miss_to_dict(analysis)
    assert d["near_miss_evals"] == 1
    assert d["shadow_would_trade_same_epic"] == 1
    assert d["estimated_counterfactual_e_gbp"] == 12.5
