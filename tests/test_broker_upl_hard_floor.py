"""B1 — air-gapped broker UPL hard floor (null-UPL must not spam flatten)."""

from __future__ import annotations

from execution.broker_upl_hard_floor import (
    evaluate_hard_floor_for_item,
    hard_floor_actions_from_ig_items,
    raw_broker_upl_gbp_from_ig_item,
)


CFG = {
    "broker_upl_hard_floor": {
        "enabled": True,
        "floor_gbp": -100.0,
        "null_upl_fail_safe": False,
        "exception_fail_safe": True,
        "allow_quote_derived_pnl": True,
    }
}


def _item(deal_id: str, upl, *, entry: float = 0.0, ccy: str = "GBP"):
    return {
        "position": {
            "dealId": deal_id,
            "direction": "BUY",
            "size": 0.5,
            "level": entry,
            "profitAndLoss": upl,
            "currency": ccy,
        },
        "market": {"epic": "IX.D.DOW.IFM.IP", "currency": ccy},
    }


def test_raw_upl_ignores_zero_entry():
    item = _item("D1", -122.35, entry=0.0)
    assert raw_broker_upl_gbp_from_ig_item(item) == -122.35


def test_hard_floor_triggers_below_threshold():
    should, reason, upl = evaluate_hard_floor_for_item(
        _item("D1", -122.35, entry=0.0), cfg=CFG
    )
    assert should is True
    assert upl == -122.35
    assert "hard_floor" in reason


def test_within_floor_no_trigger():
    should, reason, upl = evaluate_hard_floor_for_item(
        _item("D1", -12.0, entry=52000.0), cfg=CFG
    )
    assert should is False
    assert "within_floor" in reason
    assert upl == -12.0


def test_null_upl_no_flatten_by_default():
    """Missing UPL must NOT spam fail-safe flatten (default null_upl_fail_safe=false)."""
    item = {
        "position": {
            "dealId": "D2",
            "direction": "BUY",
            "size": 0.5,
            "level": 0.0,
        },
        "market": {"epic": "IX.D.DOW.IFM.IP"},
    }
    should, reason, upl = evaluate_hard_floor_for_item(item, cfg=CFG)
    assert should is False
    assert reason == "broker_upl_null_no_flatten"
    assert upl is None


def test_null_upl_legacy_fail_safe_opt_in():
    cfg = {
        "broker_upl_hard_floor": {
            "enabled": True,
            "floor_gbp": -100.0,
            "null_upl_fail_safe": True,
            "allow_quote_derived_pnl": False,
        }
    }
    item = {
        "position": {"dealId": "D3", "direction": "BUY", "size": 0.5, "level": 0.0},
        "market": {"epic": "IX.D.DOW.IFM.IP"},
    }
    should, reason, upl = evaluate_hard_floor_for_item(item, cfg=cfg)
    assert should is True
    assert reason == "broker_upl_null_fail_safe"


def test_actions_from_items():
    acts = hard_floor_actions_from_ig_items(
        [_item("D1", -122.0), _item("D2", -5.0)],
        cfg=CFG,
    )
    assert len(acts) == 1
    assert acts[0].deal_id == "D1"
    assert acts[0].action == "flatten"


def test_null_upl_no_actions_in_bulk():
    acts = hard_floor_actions_from_ig_items(
        [
            {
                "position": {"dealId": "N1", "direction": "BUY", "size": 0.5, "level": 0},
                "market": {"epic": "IX.D.DOW.IFM.IP"},
            }
        ],
        cfg=CFG,
    )
    assert acts == []
