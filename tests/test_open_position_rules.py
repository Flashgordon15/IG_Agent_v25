"""Tests for unified open-position assessment rules."""

from __future__ import annotations

from types import SimpleNamespace

from execution.open_position_rules import (
    OpenPositionRow,
    assess_open_positions,
    _cap_breach_actions,
)


def _cfg(**overrides):
    base = {
        "max_open_positions": 6,
        "max_positions_per_epic": 2,
        "position_management": {"enforce_cap_breach": True},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_target_hit_queues_flatten():
    rows = [
        OpenPositionRow(
            deal_id="D1",
            epic="IX.D.NIKKEI.IFM.IP",
            direction="BUY",
            size=1.0,
            entry=40000.0,
            pnl_gbp=12.0,
            loss_cap_gbp=10.0,
            soft_loss_gbp=5.5,
            target_gbp=8.0,
            trail_trigger_gbp=6.0,
        )
    ]
    report = assess_open_positions(rows, _cfg(), agent_up=True)
    assert len(report.actions) == 1
    assert report.actions[0].action == "flatten"
    assert "target hit" in report.actions[0].reason


def test_soft_loss_before_target():
    rows = [
        OpenPositionRow(
            deal_id="D2",
            epic="CS.D.CFPGOLD.CFP.IP",
            direction="SELL",
            size=0.5,
            entry=3300.0,
            pnl_gbp=-6.0,
            loss_cap_gbp=10.0,
            soft_loss_gbp=5.5,
            target_gbp=8.0,
            trail_trigger_gbp=6.0,
        )
    ]
    report = assess_open_positions(rows, _cfg(), agent_up=True)
    assert report.actions[0].reason.startswith("soft_loss")


def test_trail_floor_breach():
    rows = [
        OpenPositionRow(
            deal_id="D3",
            epic="IX.D.DOW.IFM.IP",
            direction="BUY",
            size=1.0,
            entry=42000.0,
            pnl_gbp=5.0,
            loss_cap_gbp=10.0,
            soft_loss_gbp=5.5,
            target_gbp=8.0,
            trail_trigger_gbp=6.0,
            peak_profit_gbp=10.0,
            trail_floor_gbp=6.5,
        )
    ]
    gbp = {
        "D3": {
            "peak_profit_gbp": 10.0,
            "trail_floor_gbp": 6.5,
            "trail_trigger_gbp": 6.0,
        }
    }
    report = assess_open_positions(rows, _cfg(), gbp_tracks=gbp, agent_up=True)
    assert any("trail_floor" in a.reason for a in report.actions)


def test_epic_cap_breach_closes_worst():
    rows = [
        OpenPositionRow(
            deal_id=f"D{i}",
            epic="IX.D.NIKKEI.IFM.IP",
            direction="BUY",
            size=1.0,
            entry=40000.0,
            pnl_gbp=float(i - 2),
            loss_cap_gbp=10.0,
            soft_loss_gbp=5.5,
            target_gbp=8.0,
        )
        for i in range(4)
    ]
    actions = _cap_breach_actions(rows, _cfg(max_positions_per_epic=2), enforce=True)
    assert len(actions) == 2
    closed = {a.deal_id for a in actions}
    assert closed == {"D0", "D1"}
