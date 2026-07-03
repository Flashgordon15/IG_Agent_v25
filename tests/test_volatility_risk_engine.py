"""Volatility risk engine tests."""

from __future__ import annotations

from unittest.mock import patch

from system.volatility_risk_engine import (
    apply_volatility_risk,
    circuit_breaker_blocks_entry,
    reset_volatility_risk_for_tests,
    update_drawdown_state,
)


def test_l1_circuit_breaker_blocks_entry():
    reset_volatility_risk_for_tests()
    import time
    from system.paths import data_dir

    state_path = data_dir() / "state" / "volatility_risk_engine.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        '{"circuit_breaker_level": 1, "halt_until_ts": '
        + str(time.time() + 600)
        + ', "peak_equity_gbp": 10000, "current_equity_gbp": 9800}',
        encoding="utf-8",
    )
    blocked, reason = circuit_breaker_blocks_entry()
    assert blocked is True
    assert "circuit_breaker_l1" in reason


def test_apply_volatility_risk_scales_size_in_trend():
    reset_volatility_risk_for_tests()
    with patch("runtime.regime_switch_engine.get_regime_gate", return_value={
        "allow_entries": True,
        "size_factor": 1.1,
        "stop_factor": 1.2,
        "limit_factor": 1.3,
        "mode": "momentum",
    }):
        with patch("runtime.regime_switch_engine.regime_allows_entry", return_value=(True, "")):
            with patch("system.volatility_risk_engine.circuit_breaker_blocks_entry", return_value=(False, "")):
                result = apply_volatility_risk(
                    epic="IX.D.DOW.IFM.IP",
                    size=1.0,
                    stop_distance=50.0,
                    limit_distance=60.0,
                )
    assert result.approved is True
    assert result.size_factor > 0
    assert result.stop_distance >= 50.0
