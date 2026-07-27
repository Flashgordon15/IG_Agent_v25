"""Tests for shadow loss loop splitter + ML exclusion rules."""

from __future__ import annotations

from diagnostics.shadow_loss_loop import (
    effective_ml_class,
    excluded_from_ml_set,
    is_ml_shadow_eligible,
    ml_shadow_train_score_set,
    normalize_loss_class,
    recommend_next_one_step,
    shadow_counterfactual_for_row,
    split_losers_by_class,
    ClassBucket,
)


def _loser(**kwargs):
    base = {
        "deal_id": "DIAAAAXTEST",
        "pnl_gbp": -5.0,
        "loss_class": "LOGIC",
        "epic": "IX.D.DOW.IFM.IP",
        "ml_score_at_entry": 0.71,
        "hold_sec": 200.0,
        "engine_origin": "MACRO_SENTINEL",
        "exit_reason": "soft_loss",
        "style": "macro",
    }
    base.update(kwargs)
    return base


def test_normalize_and_unknown_as_app():
    assert normalize_loss_class("logic") == "LOGIC"
    assert normalize_loss_class(None) == "UNKNOWN"
    assert effective_ml_class("UNKNOWN") == "APP"
    assert effective_ml_class("APP") == "APP"
    assert effective_ml_class("LOGIC") == "LOGIC"


def test_splitter_counts_and_pnl():
    losers = [
        _loser(deal_id="A", loss_class="APP", pnl_gbp=-10.0),
        _loser(deal_id="B", loss_class="LOGIC", pnl_gbp=-4.5),
        _loser(deal_id="C", loss_class="LOGIC", pnl_gbp=-1.5),
        _loser(deal_id="D", loss_class="UNKNOWN", pnl_gbp=-2.0),
        _loser(deal_id="E", loss_class="", pnl_gbp=-3.0),  # → UNKNOWN
    ]
    buckets = split_losers_by_class(losers)
    assert buckets["APP"].count == 1
    assert buckets["APP"].pnl_gbp == -10.0
    assert buckets["LOGIC"].count == 2
    assert buckets["LOGIC"].pnl_gbp == -6.0
    assert buckets["UNKNOWN"].count == 2
    assert buckets["UNKNOWN"].pnl_gbp == -5.0


def test_app_and_unknown_excluded_from_ml_shadow_set():
    losers = [
        _loser(deal_id="app1", loss_class="APP", pnl_gbp=-9.0),
        _loser(deal_id="unk1", loss_class="UNKNOWN", pnl_gbp=-2.0),
        _loser(deal_id="logic1", loss_class="LOGIC", pnl_gbp=-4.0, ml_score_at_entry=0.48),
        _loser(deal_id="logic2", loss_class="LOGIC", pnl_gbp=-3.0, ml_score_at_entry=0.70),
    ]
    assert is_ml_shadow_eligible(losers[0]) is False
    assert is_ml_shadow_eligible(losers[1]) is False
    assert is_ml_shadow_eligible(losers[2]) is True

    eligible = ml_shadow_train_score_set(losers)
    excluded = excluded_from_ml_set(losers)
    assert [r["deal_id"] for r in eligible] == ["logic1", "logic2"]
    assert {r["deal_id"] for r in excluded} == {"app1", "unk1"}
    # Hard rule: no APP/UNKNOWN rows in the ML shadow train/score set.
    assert all(r["loss_class"] == "LOGIC" for r in eligible)
    assert all(r["loss_class"] != "LOGIC" for r in excluded)


def test_shadow_counterfactual_veto_on_stamp_below_floor():
    row = _loser(ml_score_at_entry=0.41, loss_class="LOGIC")
    cf = shadow_counterfactual_for_row(row, min_ml_probability=0.52)
    assert cf["would_veto"] is True
    assert cf["score_source"] == "stamp"
    assert "0.410" in cf["reason"] or "0.41" in cf["reason"]


def test_shadow_counterfactual_pass_above_floor():
    row = _loser(ml_score_at_entry=0.72, loss_class="LOGIC")
    cf = shadow_counterfactual_for_row(row, min_ml_probability=0.52)
    assert cf["would_veto"] is False
    assert cf["score_used"] == 0.72


def test_recommend_app_when_app_dominates():
    buckets = {
        "APP": ClassBucket("APP", count=6, pnl_gbp=-40.0),
        "LOGIC": ClassBucket("LOGIC", count=1, pnl_gbp=-4.0),
        "UNKNOWN": ClassBucket("UNKNOWN", count=1, pnl_gbp=-2.0),
    }
    nxt = recommend_next_one_step(buckets, {"would_veto_rate": 0.0})
    assert nxt["lane"] == "APP"
    assert "fix_app" in nxt["action"]


def test_recommend_logic_ml_veto_when_veto_rate_high():
    buckets = {
        "APP": ClassBucket("APP", count=1, pnl_gbp=-2.0),
        "LOGIC": ClassBucket("LOGIC", count=8, pnl_gbp=-30.0),
        "UNKNOWN": ClassBucket("UNKNOWN", count=0, pnl_gbp=0.0),
    }
    nxt = recommend_next_one_step(buckets, {"would_veto_rate": 0.75})
    assert nxt["lane"] == "LOGIC"
    assert nxt["action"] == "ml_veto_learning"
