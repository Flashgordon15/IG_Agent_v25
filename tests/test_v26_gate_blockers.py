"""Tests for v26 gate blocker analysis and L1 replay."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "v26"))

from research.gate_blockers import build_gate_blocker_report, report_to_dict
from research.l1_replay import replay_day_signals


def test_gate_blocker_report_counts_confidence_buckets(
    tmp_path: Path, monkeypatch
) -> None:
    events = tmp_path / "events"
    events.mkdir()
    day = "2026-06-08"
    lines = [
        {
            "event_type": "gate_result",
            "epic": "IX.D.NASDAQ.IFM.IP",
            "payload": {"gate_name": "signal_confidence", "passed": False},
        },
        {
            "event_type": "signal_eval",
            "epic": "IX.D.NASDAQ.IFM.IP",
            "payload": {
                "adjusted_score": 72.0,
                "setup_key": "BUY|bull|london_us_overlap|atr0-30|rsihigh|volnormal",
                "would_fire": False,
            },
        },
        {
            "event_type": "signal_eval",
            "epic": "IX.D.NASDAQ.IFM.IP",
            "payload": {
                "adjusted_score": 77.0,
                "setup_key": "SELL|bear|x",
                "would_fire": False,
            },
        },
    ]
    (events / f"{day}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in lines) + "\n",
        encoding="utf-8",
    )

    import ingest.lake_reader as lr

    monkeypatch.setattr(lr, "events_dir", lambda: events)
    report = build_gate_blocker_report(day=day)
    d = report_to_dict(report)
    assert d["failed_gates"]["signal_confidence"] == 1
    assert d["confidence_buckets"]["70-74"] == 1
    assert d["confidence_buckets"]["75-79"] == 1
    assert d["near_miss"]["70_74_pct"] == 1
    assert d["near_miss"]["75_79_pct"] == 1


def test_l1_replay_threshold_counts(tmp_path: Path, monkeypatch) -> None:
    day = "2026-06-08"
    feat = tmp_path / "data_lake" / "features" / day
    feat.mkdir(parents=True)
    feat.joinpath("signals.csv").write_text(
        "epic,adjusted_score,would_fire\n"
        "IX.D.NASDAQ.IFM.IP,72,false\n"
        "IX.D.NASDAQ.IFM.IP,78,false\n"
        "CS.D.CFPGOLD.CFP.IP,81,true\n",
        encoding="utf-8",
    )

    import research.l1_replay as l1

    monkeypatch.setattr(l1, "_project_root", lambda: tmp_path)
    result = replay_day_signals(day)
    assert result["ok"] is True
    assert result["evals"] == 3
    assert result["by_threshold"][">=75"] == 2
    assert result["by_threshold"][">=70"] == 3
