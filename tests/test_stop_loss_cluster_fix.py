"""Repeated -£7.59 / -£9.11 losses — broker min stop vs virtual stop alignment."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from execution.micro_risk_profile import (
    clamp_size_for_stop_risk,
    loss_gbp_at_stop,
    resolve_virtual_ceiling_pts,
)
from execution.post_fill_risk_controls import arm_post_fill_risk_controls


def test_dow_ten_point_loss_matches_observed_cluster():
    """10 IG pts × $2/pt × 0.5 size ≈ £7.59 at typical USD/GBP."""
    with patch("trading.open_position_view.pnl_currency_amount_to_gbp", side_effect=lambda amt, ccy: amt * 0.759):
        gbp = loss_gbp_at_stop("IX.D.DOW.IFM.IP", size=0.5, stop_pts=10.0)
    assert gbp == pytest.approx(7.59, abs=0.05)


def test_dow_twelve_point_loss_matches_observed_cluster():
    with patch("trading.open_position_view.pnl_currency_amount_to_gbp", side_effect=lambda amt, ccy: amt * 0.759):
        gbp = loss_gbp_at_stop("IX.D.DOW.IFM.IP", size=0.5, stop_pts=12.0)
    assert gbp == pytest.approx(9.11, abs=0.05)


def test_clamp_size_reduces_lot_when_mandatory_stop_too_wide():
    cfg = MagicMock()
    cfg.get = lambda k, d=None: {
        "micro_risk": {
            "risk_per_trade_gbp": 5.0,
            "max_loss_cap_pts": 4.0,
            "virtual_stop_ceiling_pts": 4.0,
        }
    }.get(k, d)
    with patch("trading.open_position_view.pnl_currency_amount_to_gbp", side_effect=lambda amt, ccy: amt * 0.759):
        capped = clamp_size_for_stop_risk("IX.D.DOW.IFM.IP", 0.5, 10.0, cfg)
    assert capped < 0.5
    with patch("trading.open_position_view.pnl_currency_amount_to_gbp", side_effect=lambda amt, ccy: amt * 0.759):
        loss = loss_gbp_at_stop("IX.D.DOW.IFM.IP", size=capped, stop_pts=10.0)
    assert loss <= 5.0 + 0.05


def test_virtual_ceiling_inside_broker_stop():
    from execution.micro_risk_profile import MicroRiskProfile

    prof = MicroRiskProfile(5.0, 1.5, 1.0, 4.0, 4.0)
    ceiling = resolve_virtual_ceiling_pts(
        epic="IX.D.DOW.IFM.IP", broker_stop_pts=10.0, profile=prof
    )
    assert ceiling < 10.0
    assert ceiling <= 4.0


def test_virtual_ceiling_prefers_virtual_stop_over_max_loss_cap():
    """Soak: ceiling=12 must not be silently clamped by leftover max_loss_cap=6."""
    from execution.micro_risk_profile import MicroRiskProfile

    prof = MicroRiskProfile(4.0, 2.5, 1.5, 6.0, 12.0)
    ceiling = resolve_virtual_ceiling_pts(
        epic="IX.D.DOW.IFM.IP", broker_stop_pts=12.0, profile=prof
    )
    assert ceiling == pytest.approx(12.0 * 0.85, abs=0.01)
    assert ceiling > 6.0


def test_virtual_ceiling_ignores_short_ig_min_broker_stop():
    """P0: IG min ~4pt must NOT collapse configured 12 → 3.4; expect ~10.2."""
    from execution.micro_risk_profile import MicroRiskProfile

    prof = MicroRiskProfile(4.0, 2.5, 6.0, 12.0, 12.0)
    ceiling = resolve_virtual_ceiling_pts(
        epic="IX.D.DOW.IFM.IP", broker_stop_pts=4.0, profile=prof
    )
    assert ceiling == pytest.approx(10.2, abs=0.05)
    assert ceiling >= 10.0
    assert ceiling != pytest.approx(3.4, abs=0.05)


def test_arm_post_fill_registers_virtual_stop():
    with patch("execution.position_risk_stack.arm_position_risk_stack") as arm:
        arm.return_value = {"ok": True, "virtual_ceiling_pts": 3.0}
        out = arm_post_fill_risk_controls(
            epic="IX.D.DOW.IFM.IP",
            direction="BUY",
            size=0.5,
            entry_level=52000.0,
            deal_id="DIATEST",
            stop_distance_pts=10.0,
            limit_distance_pts=20.0,
        )
    assert out["ok"] is True
    arm.assert_called_once()
