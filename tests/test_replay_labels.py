"""Replay forward-label stop scaling."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from replay_signals import _effective_spread, _label_direction, _stop_price_delta


class _Cfg:
    max_spread_points = 12


def test_replay_spread_clamps_placeholder() -> None:
    bar = {"spread": 15.0, "c": 50000, "h": 50010, "l": 49990}
    assert _effective_spread("IX.D.DOW.IFM.IP", bar, _Cfg()) == 3.0
    bar_ok = {"spread": 8.0, "c": 50000, "h": 50010, "l": 49990}
    assert _effective_spread("IX.D.DOW.IFM.IP", bar_ok, _Cfg()) == 8.0


def test_fx_stop_uses_pip_scale() -> None:
    assert _stop_price_delta("CS.D.EURUSD.CFD.IP", 5) == 0.0005
    assert _stop_price_delta("CS.D.GBPUSD.CFD.IP", 5) == 0.0005


def test_index_stop_uses_points() -> None:
    assert _stop_price_delta("IX.D.DOW.IFM.IP", 80) == 80.0


def test_fx_label_can_win() -> None:
    entry = 1.1440
    delta = _stop_price_delta("CS.D.EURUSD.CFD.IP", 5)
    label = _label_direction(
        "SELL",
        entry,
        fwd_high=entry + delta * 0.5,
        fwd_low=entry - delta * 1.2,
        stop_pts=delta,
    )
    assert label == "WIN"
