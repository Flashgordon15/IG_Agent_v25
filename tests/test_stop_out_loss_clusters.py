"""Stop-out P&L clusters — documents repeated £7.6x / £9.1x loss arithmetic."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from execution.micro_risk_profile import resolve_micro_tp_sl_for_epic


@pytest.mark.parametrize(
    "stop_pts,size,point_value_gbp,expected_loss",
    [
        # Nikkei canary: 10pt broker stop × 0.5 lot × ~£1.52/pt (IG spreadbet empirical)
        (10.0, 0.5, 1.52, 7.60),
        # Nikkei size 1.0, ~6pt stop (broker floor variant)
        (6.0, 1.0, 1.52, 9.12),
        # Gold micro scalp: ~1pt effective stop × size 10 × £0.79/pt
        (1.0, 10.0, 0.79, 7.90),
        (1.15, 10.0, 0.79, 9.09),
    ],
)
def test_stop_out_loss_formula(stop_pts, size, point_value_gbp, expected_loss):
    """Repeated closed-trade losses match stop_pts × size × point_value_gbp."""
    loss = stop_pts * size * point_value_gbp
    assert abs(loss - expected_loss) < 0.15


def test_micro_risk_nikkei_uses_correct_point_value_not_inflated_config():
    """Micro SL sizing must not assume 5.13 £/pt (legacy config bug)."""
    with patch("trading.open_position_view.point_value_gbp_for_epic", return_value=1.0):
        _tp, sl, profile = resolve_micro_tp_sl_for_epic(
            "IX.D.NIKKEI.IFM.IP",
            0.5,
            {"micro_risk": {"risk_per_trade_gbp": 5.0, "max_loss_cap_pts": 4.0}},
        )
    assert profile.risk_per_trade_gbp == 5.0
    # 5 / (0.5 × 1.0) = 10pt before cap — not 1.95pt from wrong 5.13 pv
    assert sl >= 4.0
