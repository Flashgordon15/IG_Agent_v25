"""Health snapshot rules — rest_poll staleness and CLOSED market bypass."""

from __future__ import annotations

from unittest.mock import patch

from api.agent_health import (
    _health_quote_max_age_sec,
    evaluate_trading_health,
)


def test_health_quote_max_age_default_for_lightstreamer() -> None:
    with patch("api.agent_health._is_rest_poll_transport", return_value=False):
        assert _health_quote_max_age_sec(epic_count=6) == 45.0


def test_health_quote_max_age_wider_for_rest_poll() -> None:
    with (
        patch("api.agent_health._is_rest_poll_transport", return_value=True),
        patch("system.config_loader.get_config") as mock_cfg,
    ):
        cfg = mock_cfg.return_value
        cfg._data = {}
        cfg.refresh_seconds = 5.0
        assert _health_quote_max_age_sec(epic_count=6) == 120.0


def test_health_quote_max_age_rest_poll_config_override() -> None:
    with (
        patch("api.agent_health._is_rest_poll_transport", return_value=True),
        patch("system.config_loader.get_config") as mock_cfg,
    ):
        cfg = mock_cfg.return_value
        cfg._data = {"health_quote_max_age_rest_poll_sec": 180}
        cfg.refresh_seconds = 5.0
        assert _health_quote_max_age_sec(epic_count=6) == 180.0


def test_evaluate_trading_health_snapshot_closed_exempts_stale_quotes() -> None:
    epic = "IX.D.NIKKEI.IFM.IP"
    with (
        patch("api.agent_health._markets_open_count", return_value=1),
        patch("api.agent_health._snapshot_market_state", return_value="CLOSED"),
    ):
        health = evaluate_trading_health(
            loops_running=True,
            paused=False,
            gate_age=8.0,
            epics=[epic],
            quote_fresh={epic: False},
        )
    assert health["trading_healthy"] is True
    assert health["quotes_required_for_health"] is False
    assert not any(i.startswith("quotes_stale:") for i in health["issues"])


def test_evaluate_trading_health_all_snapshot_closed_skips_quotes() -> None:
    epics = ["IX.D.NIKKEI.IFM.IP", "CS.D.EURUSD.CFD.IP"]
    with patch(
        "api.agent_health._snapshot_market_state",
        side_effect=lambda e: "CLOSED",
    ):
        health = evaluate_trading_health(
            loops_running=True,
            paused=False,
            gate_age=8.0,
            epics=epics,
            quote_fresh={e: False for e in epics},
        )
    assert health["markets_open_count"] == 0
    assert health["trading_healthy"] is True
    assert not any("quotes_stale" in i for i in health["issues"])
