"""Tests for morning roadmap Telegram/markdown formatting."""

from __future__ import annotations


def test_format_telegram_summary_delta():
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    from roadmap_morning_report import format_telegram_summary

    payload = {
        "day": "2026-06-11",
        "overall_pct": 50,
        "milestone": "M0",
        "sections": [
            {"id": "certification", "pct": 60},
            {"id": "edge", "pct": 40},
            {"id": "coverage", "pct": 55},
            {"id": "flow", "pct": 45},
        ],
        "profitability_14d": {"net_gbp": 33.83, "wr_pct": 30.8, "trades": 27},
        "feeder_today": {"trade_ready": 2, "order_intents": 1},
        "gate_blockers_7d": {"top": [{"gate": "session_open", "pct": 72}]},
        "relaxation": {"demo_soak_enabled": True},
        "history": [{"day": "2026-06-10", "overall_pct": 48}],
    }
    prev = {"day": "2026-06-10", "overall_pct": 48, "sections": []}
    text = format_telegram_summary(payload, prev)
    assert "50%" in text
    assert "+2" in text
    assert "M0" in text
    assert "Soak ON" in text
