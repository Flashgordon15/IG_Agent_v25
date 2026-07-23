"""EDITS_ONLY close queue + untrusted entry guards."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from execution.edits_only_close_queue import (
    drain_when_tradeable,
    enqueue_close,
    load_queue,
    pending_count,
    remove_deal,
)
from execution.open_position_rules import OpenPositionRow, assess_open_positions
from execution.position_pnl_gbp import pnl_gbp_for_open_row
from types import SimpleNamespace


def test_enqueue_only_edits_only(tmp_path, monkeypatch):
    monkeypatch.setenv("IG_AGENT_DATA_DIR", str(tmp_path))
    assert enqueue_close(
        deal_id="D1",
        epic="IX.D.DOW.IFM.IP",
        error="IGOrderError: status=EDITS_ONLY",
        reason="soft_loss",
        pnl_gbp=-10.0,
    )
    assert pending_count() == 1
    assert not enqueue_close(
        deal_id="D2",
        epic="IX.D.DOW.IFM.IP",
        error="HTTP 500 boom",
        reason="soft_loss",
    )
    assert pending_count() == 1
    remove_deal("D1")
    assert pending_count() == 0


def test_drain_closes_when_tradeable(tmp_path, monkeypatch):
    monkeypatch.setenv("IG_AGENT_DATA_DIR", str(tmp_path))
    enqueue_close(
        deal_id="DX",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        error="EDITS_ONLY",
        reason="soft_loss",
        pnl_gbp=-5.0,
    )
    rest = MagicMock()
    rest.open_positions.return_value = [
        {
            "position": {"dealId": "DX", "direction": "BUY", "size": 0.5},
            "market": {"epic": "IX.D.DOW.IFM.IP"},
        }
    ]
    with patch(
        "execution.broker_tradeability.broker_market_status",
        return_value="TRADEABLE",
    ):
        out = drain_when_tradeable(rest, cfg=None)
    assert out["closed"] == 1
    assert pending_count() == 0
    rest.close_position.assert_called_once()


def test_pnl_rejects_zero_entry_even_with_upl():
    assert (
        pnl_gbp_for_open_row(
            epic="IX.D.DOW.IFM.IP",
            direction="BUY",
            entry_level=0.0,
            size=0.5,
            upl=-122.0,
            bid=40000.0,
            offer=40001.0,
            currency="USD",
        )
        is None
    )


def test_assess_skips_untrusted_entry():
    """Untrusted entry skips soft/trail; B1 absolute UPL floor still flattens."""
    rows = [
        OpenPositionRow(
            deal_id="D0",
            epic="IX.D.DOW.IFM.IP",
            direction="BUY",
            size=0.5,
            entry=0.0,
            pnl_gbp=-122.0,
            loss_cap_gbp=4.0,
            soft_loss_gbp=2.2,
            target_gbp=8.5,
            trail_trigger_gbp=1.0,
            trail_floor_gbp=0.0,
            peak_profit_gbp=0.0,
            open_mins=1.0,
        )
    ]
    cfg = SimpleNamespace(
        max_open_positions=6,
        position_management={"enforce_cap_breach": False},
    )
    report = assess_open_positions(rows, cfg, gbp_tracks={}, agent_up=False)
    # B1 air-gap fires before soft/hard; untrusted soft path is skipped via continue.
    assert len(report.actions) == 1
    assert report.actions[0].action == "flatten"
    assert "broker_upl_hard_floor" in report.actions[0].reason
    assert not any("soft_loss" in (a.reason or "") for a in report.actions)
