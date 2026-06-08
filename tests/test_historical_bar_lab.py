"""Tests for OHLC historical bar lab (S2/S3 replay)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "v26"))

from research.historical_bar_lab import run_historical_bar_lab


def test_historical_bar_lab_counts_s2_would_trade(tmp_path: Path, monkeypatch) -> None:
    cache = tmp_path / "test_5m.jsonl"
    bars = [
        {"t": "2026-06-08T14:00:00", "o": 100.0, "h": 100.5, "l": 99.5, "c": 100.45},
        {"t": "2026-06-08T14:05:00", "o": 100.45, "h": 100.6, "l": 99.4, "c": 99.42},
    ]
    cache.write_text("\n".join(json.dumps(b) for b in bars) + "\n", encoding="utf-8")

    import research.historical_bar_lab as hbl

    monkeypatch.setattr(
        hbl,
        "_load_enabled_markets",
        lambda: [("nikkei", "IX.D.NIKKEI.IFM.IP", "Nikkei")],
    )
    monkeypatch.setattr(
        "trading.ohlc_cache_paths.ohlc_cache_path",
        lambda epic, market="": cache,
    )

    report = run_historical_bar_lab()
    assert report["ok"] is True
    assert report["total_bars"] == 2
    s2 = report["by_strategy"]["S2_momentum"]
    assert s2["intents"] >= 1
    assert s2["would_trade"] >= 1


def test_historical_bar_lab_empty_cache(monkeypatch) -> None:
    import research.historical_bar_lab as hbl

    monkeypatch.setattr(
        hbl,
        "_load_enabled_markets",
        lambda: [("nikkei", "IX.D.NIKKEI.IFM.IP", "Nikkei")],
    )
    monkeypatch.setattr(
        "trading.ohlc_cache_paths.ohlc_cache_path",
        lambda epic, market="": Path("/nonexistent/cache.jsonl"),
    )

    report = run_historical_bar_lab()
    assert report["ok"] is False
    assert report["total_bars"] == 0


def test_session_for_bar_parses_iso_timestamp() -> None:
    import research.historical_bar_lab as hbl

    assert hbl._session_for_bar("2026-06-08T10:00:00") == "london_morning"
    assert hbl._session_for_bar("2026-06-08T14:00:00") == "london_us_overlap"
