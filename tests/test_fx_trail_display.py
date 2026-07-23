"""FX trail price conversion — pips not index points."""

from __future__ import annotations

from system.memory_context import RuntimeContext


def test_eurusd_trail_price_delta_is_pips_not_points():
    ctx = RuntimeContext()
    # 2 pip trail → 0.0002 price units on EURUSD (point_multiplier=10000)
    delta = ctx.trail_price_delta("CS.D.EURUSD.CFD.IP", 2.0)
    assert abs(delta - 0.0002) < 1e-9
    # Index: 2pt trail × multiplier 1 = 2.0
    dow = ctx.trail_price_delta("IX.D.DOW.IFM.IP", 2.0)
    assert abs(dow - 2.0) < 1e-9
