"""v26 Stage 2 — S3 FX, regime router, calendar guard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from expectancy.shadow_attribution import attribute_fills, summarize_strategy_pnl
from regime.calendar_guard import (
    apply_calendar_guard,
    is_news_blocked,
    reset_calendar_guard_cache_for_tests,
)
from regime.router import (
    FX_EPICS,
    regime_blocks_strategy,
    reset_regime_cache_for_tests,
    route_strategies_for_event,
    update_regime_cache,
)
from shadow.runner import process_day_events, reset_shadow_state, shadow_dir
from strategies.s1_rules_v25 import S1RulesV25
from strategies.s2_momentum import S2Momentum
from strategies.s3_session_fx import S3SessionFx


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    reset_regime_cache_for_tests()
    reset_calendar_guard_cache_for_tests()
    reset_shadow_state()


def test_s3_fade_strength_on_eurusd() -> None:
    s3 = S3SessionFx()
    row = {
        "event_type": "bar_close",
        "ts": "2026-06-08T09:30:00Z",
        "epic": "CS.D.EURUSD.CFD.IP",
        "market": "EUR/USD",
        "session": "london_morning",
        "payload": {
            "open": 1.0850,
            "high": 1.0862,
            "low": 1.0848,
            "close": 1.0861,
        },
    }
    intent = s3.evaluate_feeder_event(row)
    assert intent is not None
    assert intent.strategy_id == "S3_session_fx"
    assert intent.direction == "SELL"
    assert intent.would_trade is True


def test_s3_ignores_non_fx() -> None:
    s3 = S3SessionFx()
    row = {
        "event_type": "bar_close",
        "ts": "2026-06-08T14:00:00Z",
        "epic": "IX.D.NASDAQ.IFM.IP",
        "session": "london_morning",
        "payload": {"open": 1, "high": 2, "low": 0.5, "close": 1.9},
    }
    assert s3.evaluate_feeder_event(row) is None


def test_router_sends_s3_only_to_fx_bar_close() -> None:
    strats = [S1RulesV25(), S2Momentum(), S3SessionFx()]
    fx_bar = {
        "event_type": "bar_close",
        "epic": "CS.D.EURUSD.CFD.IP",
        "session": "london_morning",
    }
    routed = route_strategies_for_event(fx_bar, strats)
    assert [s.strategy_id for s in routed] == ["S3_session_fx"]

    idx_bar = {
        "event_type": "bar_close",
        "epic": "IX.D.NASDAQ.IFM.IP",
        "session": "us_afternoon",
    }
    routed_idx = route_strategies_for_event(idx_bar, strats)
    assert [s.strategy_id for s in routed_idx] == ["S2_momentum"]


def test_calendar_guard_blocks_during_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "regime.calendar_guard._project_root",
        lambda: tmp_path,
    )
    reset_calendar_guard_cache_for_tests()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "calendar.json").write_text(
        json.dumps(
            {
                "block_minutes_before": 30,
                "block_minutes_after": 30,
                "events": [
                    {
                        "time": "2026-06-08T13:30:00Z",
                        "title": "US NFP",
                        "impact": "high",
                        "markets": list(FX_EPICS),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    reset_calendar_guard_cache_for_tests()
    blocked, reason = is_news_blocked("CS.D.EURUSD.CFD.IP", "2026-06-08T13:35:00Z")
    assert blocked is True
    assert "NFP" in reason or "calendar" in reason.lower()
    clear, _ = is_news_blocked("CS.D.EURUSD.CFD.IP", "2026-06-08T10:00:00Z")
    assert clear is False


def test_regime_blocks_s2_in_high_vol() -> None:
    update_regime_cache(
        {
            "event_type": "regime_snapshot",
            "epic": "IX.D.NASDAQ.IFM.IP",
            "payload": {"vol_regime": "high", "fitness": 50},
        }
    )
    blocked, reason = regime_blocks_strategy("IX.D.NASDAQ.IFM.IP", "S2_momentum")
    assert blocked is True
    assert "momentum" in reason


def test_shadow_pnl_attribution_matches_direction() -> None:
    shadows = [
        {
            "strategy_id": "S1_rules_v25",
            "epic": "CS.D.CFPGOLD.CFP.IP",
            "ts": "2026-06-08T10:00:00Z",
            "payload": {"direction": "BUY", "would_trade": True},
        }
    ]
    fills = [
        {
            "epic": "CS.D.CFPGOLD.CFP.IP",
            "ts": "2026-06-08T10:30:00Z",
            "payload": {"direction": "BUY", "pnl_gbp": 25.0, "result": "WIN"},
        }
    ]
    attributed = attribute_fills(fills, shadows)
    assert len(attributed) == 1
    assert attributed[0].strategy_id == "S1_rules_v25"
    summary = summarize_strategy_pnl(attributed)
    assert summary["S1_rules_v25"]["n"] == 1
    assert summary["S1_rules_v25"]["total_pnl_gbp"] == 25.0


def test_regime_cache_from_snapshot() -> None:
    update_regime_cache(
        {
            "event_type": "regime_snapshot",
            "epic": "CS.D.EURUSD.CFD.IP",
            "ts": "t1",
            "payload": {"vol_regime": "high", "fitness": 42.0},
        }
    )
    from regime.router import regime_for_epic

    reg = regime_for_epic("CS.D.EURUSD.CFD.IP")
    assert reg.get("vol_regime") == "high"
    assert reg.get("fitness") == 42.0


def test_shadow_runner_routes_s3_for_fx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events_dir = tmp_path / "data_lake" / "events"
    events_dir.mkdir(parents=True)
    monkeypatch.setattr("ingest.lake_reader._project_root", lambda: tmp_path)
    monkeypatch.setattr("shadow.runner._project_root", lambda: tmp_path)
    day = "2026-06-08"
    (events_dir / f"{day}.jsonl").write_text(
        json.dumps(
            {
                "event_type": "bar_close",
                "ts": "2026-06-08T09:30:00Z",
                "epic": "CS.D.EURUSD.CFD.IP",
                "market": "EUR/USD",
                "session": "london_morning",
                "payload": {
                    "open": 1.0850,
                    "high": 1.0862,
                    "low": 1.0848,
                    "close": 1.0861,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    from ingest.lake_reader import iter_events

    n = process_day_events(list(iter_events(day=day)), day=day, clear_seen=True)
    assert n == 1
    row = json.loads((shadow_dir() / f"{day}.jsonl").read_text().strip())
    assert row["strategy_id"] == "S3_session_fx"
