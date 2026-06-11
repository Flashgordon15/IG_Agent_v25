"""OHLC bootstrap readiness and health exemption."""

from __future__ import annotations

from unittest.mock import patch

from trading.ohlc_readiness import (
    epic_quote_health_exempt,
    finalize_bootstrap_state,
    record_bootstrap,
    reset_bootstrap_state_for_tests,
)


def test_epic_quote_health_exempt_when_bootstrap_failed() -> None:
    reset_bootstrap_state_for_tests()
    record_bootstrap("CS.D.CRUDE.CFD.IP", "US Oil WTI", 0)
    finalize_bootstrap_state()
    assert epic_quote_health_exempt("CS.D.CRUDE.CFD.IP") is True


def test_epic_quote_health_ready_after_warm_bootstrap() -> None:
    reset_bootstrap_state_for_tests()
    record_bootstrap("CS.D.EURUSD.CFD.IP", "EUR/USD", 100)
    finalize_bootstrap_state()
    assert epic_quote_health_exempt("CS.D.EURUSD.CFD.IP") is False


def test_evaluate_trading_health_ohlc_not_ready_exempts_stale_quotes() -> None:
    from api.agent_health import evaluate_trading_health

    reset_bootstrap_state_for_tests()
    record_bootstrap("CS.D.CRUDE.CFD.IP", "US Oil WTI", 0)
    finalize_bootstrap_state()

    with patch("api.agent_health._markets_open_count", return_value=1):
        health = evaluate_trading_health(
            loops_running=True,
            paused=False,
            gate_age=8.0,
            epics=["CS.D.CRUDE.CFD.IP"],
            quote_fresh={"CS.D.CRUDE.CFD.IP": False},
        )
    assert health["trading_healthy"] is True
    assert not any("quotes_stale" in issue for issue in health["issues"])
