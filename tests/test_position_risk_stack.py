"""Position risk stack reconcile tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from execution.position_risk_stack import reconcile_open_positions_risk_stack
from runtime.dynamic_limit_engine import reset_dynamic_limit_for_tests, snapshot as dyn_snap
from runtime.micro_gbp_exit import register_gbp_exit, reset_micro_gbp_exit_for_tests, snapshot as gbp_snap
from runtime.virtual_stop_loss import reset_virtual_stop_for_tests, virtual_stop_snapshot


def setup_function() -> None:
    reset_micro_gbp_exit_for_tests()
    reset_dynamic_limit_for_tests()
    reset_virtual_stop_for_tests()


def test_reconcile_arms_all_layers_for_open_position():
    rest = MagicMock()
    rest.open_positions.return_value = [
        {
            "position": {
                "dealId": "D100",
                "epic": "IX.D.DOW.IFM.IP",
                "direction": "BUY",
                "size": 0.5,
                "level": 52800.0,
                "stopLevel": 52788.0,
            },
            "market": {
                "epic": "IX.D.DOW.IFM.IP",
                "bid": 52801.0,
                "offer": 52802.0,
            },
        }
    ]
    counts = reconcile_open_positions_risk_stack(rest, cfg=None, force=True)
    assert counts["gbp"] == 1
    assert counts["armed"] >= 1
    assert "D100" in gbp_snap()["tracks"]
    assert any(
        p.get("deal_id") == "D100"
        for p in virtual_stop_snapshot().get("positions", [])
    )
    assert "D100" in dyn_snap()["tracks"]


def test_reconcile_prunes_stale_gbp_track():
    register_gbp_exit(
        deal_id="STALE",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        entry_level=52800.0,
        loss_cap_gbp=5.0,
        target_profit_gbp=12.5,
        trail_trigger_gbp=1.5,
        trail_lock_ratio=0.65,
        min_bank_win_gbp=1.0,
    )
    rest = MagicMock()
    rest.open_positions.return_value = []
    counts = reconcile_open_positions_risk_stack(rest, cfg=None, force=True)
    assert counts["pruned"] == 1
    assert "STALE" not in gbp_snap()["tracks"]


def test_ensure_coverage_forces_on_track_mismatch():
    rest = MagicMock()
    rest.open_positions.return_value = [
        {
            "position": {
                "dealId": "D200",
                "epic": "IX.D.DOW.IFM.IP",
                "direction": "BUY",
                "size": 0.5,
                "level": 52800.0,
                "stopLevel": 52788.0,
            },
            "market": {"epic": "IX.D.DOW.IFM.IP", "bid": 52801.0, "offer": 52802.0},
        }
    ]
    from execution.position_risk_stack import ensure_risk_stack_coverage

    counts = ensure_risk_stack_coverage(rest, cfg=None, force=True)
    assert counts.get("gbp", 0) >= 1 or counts.get("armed", 0) >= 1


def test_prune_orphaned_when_broker_flat():
    from execution.position_risk_stack import prune_orphaned_risk_tracks
    from runtime.virtual_stop_loss import register_virtual_stop

    register_gbp_exit(
        deal_id="ORPHAN",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        entry_level=52800.0,
        loss_cap_gbp=5.0,
        target_profit_gbp=12.5,
        trail_trigger_gbp=1.5,
        trail_lock_ratio=0.65,
        min_bank_win_gbp=1.0,
    )
    register_virtual_stop(
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        entry_level=52800.0,
        size=0.5,
        deal_id="ORPHAN",
    )
    pruned = prune_orphaned_risk_tracks()
    assert pruned >= 1
    assert "ORPHAN" not in gbp_snap()["tracks"]
