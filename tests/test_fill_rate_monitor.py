"""Fill-rate telemetry harness — sync mode for deterministic unit tests."""

from __future__ import annotations

from diagnostics.fill_rate_monitor import (
    BASE_SLIP_MULT,
    RELAXED_SLIP_MULT,
    FillRateMonitor,
    get_fill_rate_monitor,
    is_high_conviction_obi,
    reset_fill_rate_monitor_for_tests,
)
from execution.asymmetric_ioc_router import (
    compute_max_slippage,
    dispatch_asymmetric_ioc_limit,
    reset_asymmetric_router_state_for_tests,
    resolve_slip_multiplier,
)


def setup_function():
    reset_asymmetric_router_state_for_tests()
    reset_fill_rate_monitor_for_tests()


def teardown_function():
    reset_fill_rate_monitor_for_tests()


def test_high_conviction_obi_threshold():
    assert is_high_conviction_obi("BUY", 0.40) is True
    assert is_high_conviction_obi("BUY", -0.40) is True  # alternate-signed feed
    assert is_high_conviction_obi("BUY", 0.39) is False
    assert is_high_conviction_obi("SELL", None) is False


def test_counters_attempts_fills_slippage_auth():
    mon = get_fill_rate_monitor(sync_mode=True)
    mon.reset()
    mon.record_attempt()
    mon.record_fill()
    mon.record_attempt()
    mon.record_slippage_reject("AMENDMENT_95")
    mon.record_auth_veto("auth_lane_not_ready")
    snap = mon.snapshot()
    assert snap["attempts"] == 2
    assert snap["fills"] == 1
    assert snap["slippage_rejects"] == 1
    assert snap["auth_vetoes"] == 1
    assert snap["fill_rate_pct"] == 50.0  # 1 fill / 2 outcomes (auth not in window)


def test_fill_rate_below_40_relaxes_multiplier():
    mon = get_fill_rate_monitor(sync_mode=True)
    mon.reset()
    # 20 outcomes: 7 fills + 13 rejects = 35% < 40%
    for _ in range(7):
        mon.record_fill()
    for _ in range(13):
        mon.record_slippage_reject("pricing")
    assert mon.current_slip_multiplier() == RELAXED_SLIP_MULT
    assert (mon.rolling_fill_rate(20) or 1) < 0.40


def test_fill_rate_recovery_drops_multiplier():
    mon = get_fill_rate_monitor(sync_mode=True)
    mon.reset()
    for _ in range(7):
        mon.record_fill()
    for _ in range(13):
        mon.record_slippage_reject("slippage")
    assert mon.current_slip_multiplier() == RELAXED_SLIP_MULT
    # Stabilize: push fills until short window >= 40%
    for _ in range(20):
        mon.record_fill()
    assert mon.current_slip_multiplier() == BASE_SLIP_MULT


def test_perf_line_format():
    mon = FillRateMonitor(sync_mode=True)
    mon.record_attempt()
    mon.record_fill()
    mon.record_slippage_reject("x")
    mon.record_auth_veto("y")
    line = mon.format_perf_line()
    assert line.startswith("[PERF_DIAGNOSTICS] Fill Rate:")
    assert "Slippage Rejects: 1" in line
    assert "Auth Vetoes: 1" in line
    assert "Current Slip Multiplier:" in line


def test_resolve_slip_requires_high_conviction():
    mon = get_fill_rate_monitor(sync_mode=True)
    mon.reset()
    for _ in range(7):
        mon.record_fill()
    for _ in range(13):
        mon.record_slippage_reject("slip")
    assert mon.current_slip_multiplier() == RELAXED_SLIP_MULT
    # Without strong OBI — stay at 0.5
    assert resolve_slip_multiplier(direction="BUY", obi=0.1) == BASE_SLIP_MULT
    # With strong OBI — allow 1.0
    assert resolve_slip_multiplier(direction="BUY", obi=-0.40) == RELAXED_SLIP_MULT


def test_dispatch_expands_max_slippage_when_relaxed():
    reset_asymmetric_router_state_for_tests()
    mon = get_fill_rate_monitor(sync_mode=True)
    mon.reset()
    for _ in range(7):
        mon.record_fill()
    for _ in range(13):
        mon.record_slippage_reject("AMENDMENT_95")

    class Rest:
        def __init__(self):
            self.calls = []

        def auth_ready_for_hot_path(self):
            return True

        def place_otc_market_payload(self, payload):
            self.calls.append(payload)
            return {"dealReference": "R1"}

    rest = Rest()
    # spread=4 → 0.5x=2, 1.0x=4
    assert compute_max_slippage(100.0, 104.0, slip_mult=0.5) == 2
    assert compute_max_slippage(100.0, 104.0, slip_mult=1.0) == 4

    out = dispatch_asymmetric_ioc_limit(
        rest,
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        bid=100.0,
        offer=104.0,
        stop_distance=4.0,
        obi=-0.45,
    )
    assert out["dealReference"] == "R1"
    assert rest.calls[0]["maxSlippage"] == 4
    assert rest.calls[0]["orderType"] == "MARKET"


def test_auth_veto_increments_counter_not_fill_window():
    class Rest:
        def auth_ready_for_hot_path(self):
            return False

    mon = get_fill_rate_monitor(sync_mode=True)
    mon.reset()
    out = dispatch_asymmetric_ioc_limit(
        Rest(),
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        bid=100.0,
        offer=102.0,
        stop_distance=4.0,
    )
    assert out["vetoed"] is True
    snap = mon.snapshot()
    assert snap["auth_vetoes"] >= 1
    assert snap["window"] == 0  # auth veto excluded from fill-rate outcomes
