"""Yahoo/hub basis must not trip virtual stop or false DynamicLimit TP."""

from __future__ import annotations

from trading.open_position_view import mark_within_ig_basis, _quote_mark_trustworthy
from runtime.virtual_stop_loss import (
    register_virtual_stop,
    on_streaming_mid_tick,
    reset_virtual_stop_for_tests,
    virtual_stop_snapshot,
)
import runtime.dynamic_limit_engine as dle


def setup_function() -> None:
    reset_virtual_stop_for_tests()
    dle.reset_dynamic_limit_for_tests()


def teardown_function() -> None:
    reset_virtual_stop_for_tests()
    dle.reset_dynamic_limit_for_tests()


def test_loose_scale_trust_allows_yahoo_basis_but_basis_gate_rejects():
    entry = 51656.9
    yahoo_mid = entry + 66.19  # observed live false-breach mid
    assert _quote_mark_trustworthy(entry, yahoo_mid, "IX.D.DOW.IFM.IP") is True
    assert mark_within_ig_basis(entry, yahoo_mid, "IX.D.DOW.IFM.IP", max_ig_pts=25.0) is False
    assert mark_within_ig_basis(entry, entry + 4.0, "IX.D.DOW.IFM.IP", max_ig_pts=25.0) is True


def test_virtual_stop_ignores_yahoo_basis_tick():
    register_virtual_stop(
        deal_id="DIAAAA_TEST_BASIS",
        epic="IX.D.DOW.IFM.IP",
        direction="SELL",
        entry_level=51656.9,
        size=0.5,
        ceiling_pts=6.0,
    )
    # Would be adverse=66 ≥ 6 without basis gate
    on_streaming_mid_tick("IX.D.DOW.IFM.IP", 51656.9 + 66.19)
    snap = virtual_stop_snapshot()
    assert snap["count"] == 1
    assert "DIAAAA_TEST_BASIS" in {p["deal_id"] for p in snap["positions"]}


def test_virtual_stop_arm_grace_skips_spread_adverse(monkeypatch):
    """Fresh SELL fill vs mid+spread must not trip ceiling inside grace window."""
    import runtime.virtual_stop_loss as vsl
    import time as _t

    register_virtual_stop(
        deal_id="DIAAAA_TEST_GRACE",
        epic="IX.D.DOW.IFM.IP",
        direction="SELL",
        entry_level=51706.4,
        size=0.5,
        ceiling_pts=6.0,
    )
    # Observed live: adverse=8.80 same second as arm
    on_streaming_mid_tick("IX.D.DOW.IFM.IP", 51706.4 + 8.80)
    assert virtual_stop_snapshot()["count"] == 1

    # After grace, same mark may flatten — simulate by rewinding armed_at
    with vsl._lock:
        track = vsl._positions["DIAAAA_TEST_GRACE"]
        track.armed_at = _t.time() - (vsl.VIRTUAL_STOP_ARM_GRACE_SEC + 1.0)
    triggered = []

    def _capture(track, *, adverse_pts):
        triggered.append(adverse_pts)

    monkeypatch.setattr(vsl, "_trigger_virtual_flatten", _capture)
    on_streaming_mid_tick("IX.D.DOW.IFM.IP", 51706.4 + 8.80)
    assert triggered and triggered[0] >= 6.0


def test_dynamic_limit_ignores_yahoo_basis_false_tp():
    dle.start_dynamic_limit_engine()
    dle.register_dynamic_limit(
        deal_id="D1",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        entry_level=51678.2,
        limit_pts=12.0,
        size=0.5,
        trail_trigger_ig_pts=1.0,
    )
    # Yahoo mid above TP without peak ratchet — must not flatten
    dle.on_streaming_mid_tick("IX.D.DOW.IFM.IP", 51678.2 + 66.0)
    assert "D1" in dle._tracks
    assert dle._tracks["D1"].peak_profit_ig_pts == 0.0
