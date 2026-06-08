"""Tests for walk-forward threshold sweep from replay rows."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "v26"))

from research.walk_forward import ml_veto_hints, threshold_sweep


def _sample_rows() -> list[dict]:
    rows = []
    for i in range(20):
        rows.append(
            {
                "epic": "IX.D.NIKKEI.IFM.IP",
                "adjusted_confidence": 78 if i < 12 else 85,
                "label_3bar": "WIN" if i % 2 == 0 else "LOSS",
            }
        )
    for i in range(15):
        rows.append(
            {
                "epic": "CS.D.EURUSD.CFD.IP",
                "confidence": 80,
                "label_3": "WIN" if i < 8 else "LOSS",
            }
        )
    return rows


def test_threshold_sweep_finds_best_per_epic() -> None:
    sweep = threshold_sweep(_sample_rows())
    assert sweep["ok"] is True
    assert sweep["total_rows"] == 35
    nikkei = sweep["by_epic"]["IX.D.NIKKEI.IFM.IP"]
    assert nikkei["total_rows"] == 20
    assert nikkei["recommended_threshold"] in (70, 75, 80, 85, 90)
    assert nikkei["best_wr"] is not None


def test_ml_veto_hints_from_sweep() -> None:
    sweep = threshold_sweep(_sample_rows())
    hints = ml_veto_hints(sweep)
    assert len(hints) >= 1
    assert "IX.D.NIKKEI.IFM.IP" in hints[0]
    assert "replay WR" in hints[0]


def test_threshold_sweep_empty_rows() -> None:
    sweep = threshold_sweep([])
    assert sweep["ok"] is False
    assert sweep["total_rows"] == 0
    assert ml_veto_hints(sweep) == []


def test_threshold_sweep_uses_adjusted_score_field() -> None:
    rows = [
        {"epic": "CS.D.CFPGOLD.CFP.IP", "adjusted_score": 82, "label_3bar": "WIN"},
        {"epic": "CS.D.CFPGOLD.CFP.IP", "adjusted_score": 82, "label_3bar": "LOSS"},
        {"epic": "CS.D.CFPGOLD.CFP.IP", "adjusted_score": 82, "label_3bar": "WIN"},
    ] * 5
    sweep = threshold_sweep(rows)
    gold = sweep["by_epic"]["CS.D.CFPGOLD.CFP.IP"]
    assert gold["best_wr"] is not None
    assert gold["total_rows"] == 15


def test_load_replay_rows_reads_jsonl(tmp_path: Path, monkeypatch) -> None:
    import research.walk_forward as wf

    replay = tmp_path / "replay_results.jsonl"
    replay.write_text(
        json.dumps({"epic": "X", "adjusted_confidence": 75, "label_3bar": "WIN"})
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(wf, "_replay_results_path", lambda: replay)
    rows = wf.load_replay_rows()
    assert len(rows) == 1
    assert rows[0]["epic"] == "X"
