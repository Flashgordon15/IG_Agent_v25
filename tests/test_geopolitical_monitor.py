"""Geopolitical oil/VIX cooling monitor — pure eval + disk state."""

from __future__ import annotations

import json
from pathlib import Path

from alpha.geopolitical_monitor import (
    evaluate_macro_ticks,
    read_macro_cooling_state,
    reset_geopolitical_monitor_for_tests,
    safe_for_veto_lift,
    _apply_and_maybe_alert,
)


def test_cooling_criteria_brent_and_vix(tmp_path: Path, monkeypatch) -> None:
    reset_geopolitical_monitor_for_tests()
    monkeypatch.setattr(
        "alpha.geopolitical_monitor.state_path",
        lambda: tmp_path / "geopolitical_macro.json",
    )
    row = evaluate_macro_ticks(brent=83.5, wti=82.0, vix=16.0, source="test")
    assert row["volatility_premium_cooling"] is True
    assert row["safe_for_veto_lift"] is True
    _apply_and_maybe_alert(row)
    st = read_macro_cooling_state(max_age_sec=None)
    assert st["volatility_premium_cooling"] is True
    assert safe_for_veto_lift() is True
    disk = json.loads((tmp_path / "geopolitical_macro.json").read_text())
    assert disk["brent"] == 83.5


def test_not_cooling_when_vix_elevated() -> None:
    row = evaluate_macro_ticks(brent=80.0, wti=None, vix=18.0, source="test")
    assert row["volatility_premium_cooling"] is False


def test_wti_proxy_when_brent_missing() -> None:
    row = evaluate_macro_ticks(brent=None, wti=83.0, vix=15.0, source="test")
    assert row["oil_ref"] == 83.0
    assert row["volatility_premium_cooling"] is True
