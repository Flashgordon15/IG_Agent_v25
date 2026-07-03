"""Market rotation matrix + multi-feed ingest telemetry — compliance scan suite."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_exploration():
    from runtime.portfolio_exploration_engine import reset_portfolio_exploration_for_tests

    reset_portfolio_exploration_for_tests()
    yield
    reset_portfolio_exploration_for_tests()


def test_rotation_drop_reason_session_block():
    from runtime.portfolio_exploration_engine import _rotation_drop_reason

    with patch("runtime.portfolio_exploration_engine.get_market_data_hub") as mock_hub, patch(
        "runtime.dual_core_execution.ticks_per_window",
        return_value=12,
    ), patch(
        "system.calendar_gate.news_proximity_features",
        return_value={"in_block_window": True},
    ):
        hub = MagicMock()
        hub.is_in_maintenance.return_value = False
        snap = MagicMock()
        snap.age_seconds.return_value = 1.0
        hub.get_snapshot.return_value = snap
        mock_hub.return_value = hub
        reason = _rotation_drop_reason("IX.D.DAX.IFM.IP")

    assert reason == "session_close_block"


def test_execute_market_rotation_sweep_european_close_to_us_fx():
    from runtime import portfolio_exploration_engine as pee

    pee.inject_exploration_rankings_for_tests(
        [
            {"epic": "IX.D.DAX.IFM.IP", "score": 0.62, "asset_class": "indices"},
            {"epic": "CS.D.EURUSD.CFD.IP", "score": 0.68, "asset_class": "fx"},
        ]
    )

    def _drop_reason(epic: str) -> str:
        if epic == "IX.D.DAX.IFM.IP":
            return "session_close_block"
        return ""

    with patch.object(pee, "_rotation_drop_reason", side_effect=_drop_reason), patch(
        "runtime.dual_core_execution.get_active_stack_epics",
        return_value=["IX.D.DAX.IFM.IP"],
    ), patch.object(
        pee,
        "scan_universe",
        return_value=[
            {"epic": "CS.D.EURUSD.CFD.IP", "score": 0.68, "asset_class": "fx"},
        ],
    ), patch.object(pee, "_five_gate_preflight", return_value=(True, "gates_ok")), patch(
        "runtime.dual_core_execution.evaluate_multi_source_rotation_sweep",
        return_value={"ok": True},
    ), patch(
        "runtime.dual_core_execution._evict_epic_from_active_memory"
    ):
        body = pee.execute_market_rotation_sweep()

    assert body["dropped"]
    assert body["promoted"]
    assert body["promoted"][0]["epic"] == "CS.D.EURUSD.CFD.IP"
    counsel = pee.get_last_rotation_counsel()
    assert "ROTATION" in counsel
    assert "EUR" in counsel.upper() or "EURUSD" in counsel


def test_cognitive_reasoning_surfaces_rotation_counsel():
    from runtime.portfolio_exploration_engine import _record_rotation_counsel
    from trading.probability_engine import compile_cognitive_reasoning

    _record_rotation_counsel(
        from_epic="IX.D.DOW.IFM.IP",
        to_epic="CS.D.EURUSD.CFD.IP",
        drop_reason="spread_exceeds_limit",
        ml_score=0.68,
        trade_ready=True,
    )
    bundle = compile_cognitive_reasoning()
    assert bundle.get("rotation_active") is True
    assert "ROTATION" in bundle["text"]
    assert bundle["severity"] == "execution_window"


def test_external_api_health_matrix_shape():
    from system.market_data_hub import get_external_api_health_matrix

    with patch(
        "feeder.yahoo_quote_poller.yahoo_poller_active",
        return_value=True,
    ), patch(
        "feeder.yahoo_quote_poller.yahoo_rate_limited",
        return_value=False,
    ), patch(
        "system.feeds.multi_feed_hub.feed_hub_telemetry",
        return_value={
            "providers": {
                "finnhub": {
                    "label": "Finnhub",
                    "connected": True,
                    "alive": True,
                    "last_frame_mono": 0.0,
                    "wins": 3,
                }
            }
        },
    ), patch(
        "system.calendar_gate.news_proximity_features",
        return_value={"seconds_to_next": 3600, "countdown_norm": 0.2},
    ), patch(
        "trading.sentiment_momentum.sentiment_momentum_features",
        return_value={"delta_5m": 0.01, "delta_30m": 0.0, "long_pct": 52.0},
    ), patch(
        "signals.feature_state.compile_current_feature_state",
        return_value={"vector": [0.0] * 98 + [0.5] * 14},
    ):
        matrix = get_external_api_health_matrix()

    assert isinstance(matrix.get("feeds"), list)
    assert matrix["feeds"]
    states = {row["state"] for row in matrix["feeds"]}
    assert "active" in states or "warming" in states


def test_telegram_coalescer_markdown_after_rotation_burst(monkeypatch):
    import time

    from system import alert_reporting_matrix as arm

    arm.reset_alert_reporting_for_tests()
    sent: list[str] = []
    monkeypatch.setattr(arm, "_telegram_send", lambda m, **kw: sent.append(m) or True)
    monkeypatch.setattr(arm, "_discord_send", lambda m: True)
    arm.set_coalesce_window_for_tests(0.15)
    arm.start_alert_reporting_matrix()

    for i in range(12):
        arm.notify_scalper_trade_event(
            ticker="CS.D.EURUSD.CFD.IP",
            title=f"Rotation promoted sweep={i}",
            body=f"Capital rotated to EUR/USD ml=0.68 sweep={i}",
            slippage_pts=0.1,
        )

    deadline = time.time() + 2.0
    while time.time() < deadline and not sent:
        time.sleep(0.05)
    arm.flush_coalesce_buffer_for_tests()
    assert sent
    combined = "\n".join(sent)
    assert "Rotation" in combined or "EUR" in combined or "Batch" in combined


def test_cockpit_ingest_grid_contract():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    html = (root / "cockpit-web" / "index.html").read_text(encoding="utf-8")
    app_js = (root / "cockpit-web" / "app.js").read_text(encoding="utf-8")
    css = (root / "cockpit-web" / "styles.css").read_text(encoding="utf-8")
    assert "api-ingest-grid" in html
    assert "renderApiIngestGrid" in app_js
    assert ".ingest-active" in css
    assert ".ingest-broken" in css
