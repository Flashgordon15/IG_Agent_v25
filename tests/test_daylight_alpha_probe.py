"""Focused unit tests for daylight_alpha_probe pure helpers."""

from __future__ import annotations

import math

import pytest

from diagnostics.daylight_alpha_probe import (
    aggregate_funnel,
    classify_feature_degeneracy,
    classify_reject,
    cro_verdict_from_evidence,
    latency_summary,
    parse_matrix_block_line,
    parse_ports,
    percentile,
    summarize_journal_rows,
)


def test_parse_ports_basic() -> None:
    assert parse_ports("8080,8081") == [8080, 8081]
    assert parse_ports(" 8080 , 8081 ") == [8080, 8081]


def test_parse_ports_empty_raises() -> None:
    with pytest.raises(ValueError):
        parse_ports("")


def test_percentile_nearest_rank() -> None:
    xs = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(xs, 50) == 30.0
    assert percentile(xs, 100) == 50.0
    assert percentile([], 50) is None
    with pytest.raises(ValueError):
        percentile(xs, 101)


def test_latency_summary_labels_method() -> None:
    s = latency_summary([1.0, 2.0, 5.0, 8.0, 12.0], method="in-process scorer")
    assert s["n"] == 5
    assert s["p50_ms"] == 5.0
    assert s["method"] == "in-process scorer"
    assert s["under_20ms_p99"] is True
    empty = latency_summary([], method="x")
    assert empty["compliance_claim"].startswith("insufficient")


def test_feature_degeneracy_obi_elast() -> None:
    d = classify_feature_degeneracy(
        {"obi_velocity": 0.0, "spread_elasticity": 1.0, "atr_velocity": 0.0}
    )
    assert d["degenerate"] is True
    assert "obi_velocity_zero" in d["flags"]
    assert "spread_elasticity_stuck_1" in d["flags"]

    ok = classify_feature_degeneracy(
        {"obi_velocity": 0.4, "spread_elasticity": 1.2, "atr_velocity": 0.1}
    )
    assert ok["degenerate"] is False


def test_feature_degeneracy_fail_open() -> None:
    d = classify_feature_degeneracy({"features_unavailable_fail_open": True})
    assert d["degenerate"] is True
    assert d["fail_open"] is True


def test_parse_matrix_block_line() -> None:
    line = (
        "ParallelStrategySweep: strategy matrix blocked epic=IX.D.DOW.IFM.IP "
        "reason=sniper_ml_chop_isolation p=0.507<0.68"
    )
    parsed = parse_matrix_block_line(line)
    assert parsed is not None
    assert parsed["epic"] == "IX.D.DOW.IFM.IP"
    assert parsed["reason"] == "sniper_ml_chop_isolation"
    assert parsed["p_success"] == pytest.approx(0.507)
    assert parse_matrix_block_line("nope") is None


def test_classify_reject_chop_with_degenerate_is_fn() -> None:
    feats = {"obi_velocity": 0.0, "spread_elasticity": 1.0}
    assert (
        classify_reject("sniper_ml_chop_isolation p=0.5<0.68", features=feats)
        == "false_negative"
    )
    assert classify_reject("overnight_cfd_new_entries_blocked") == "correct"


def test_aggregate_funnel_and_journal() -> None:
    cands = [
        {
            "approved": False,
            "reason": "sniper_ml_chop_isolation",
            "reject_reason": "sniper_ml_chop_isolation",
            "p_success": 0.5,
            "desk": "CFD",
            "epic": "IX.D.DOW.IFM.IP",
            "reject_class": "correct",
        },
        {
            "approved": True,
            "reason": "sniper_ml_ok",
            "p_success": 0.8,
            "desk": "SB",
            "epic": "IX.D.DOW.IFM.IP",
            "reject_class": "correct",
        },
        {
            "approved": False,
            "reason": "x",
            "reject_reason": "x",
            "p_success": None,
            "desk": "CFD",
            "epic": "GOLD",
            "reject_class": "uncertain",
        },
    ]
    f = aggregate_funnel(cands)
    assert f["n"] == 3
    assert f["approved"] == 1
    assert f["rejected"] == 2
    assert f["ml_finite_p"] == 2
    assert f["ml_null_p"] == 1

    j = summarize_journal_rows(
        [
            {
                "RealizedPnL_GBP": "2.0",
                "MlScoreAtEntry": "0.8",
                "ExitReason": "micro_gbp_exit",
                "Style": "scalp",
                "AccountID": "Z6BAH4",
                "HoldSec": "12",
            },
            {
                "RealizedPnL_GBP": "-1.0",
                "MlScoreAtEntry": "",
                "ExitReason": "broker_attached",
                "Style": "macro",
                "AccountID": "Z6BAH3",
            },
        ]
    )
    assert j["n"] == 2
    assert j["wins"] == 1
    assert j["losses"] == 1
    assert j["net_gbp"] == 1.0
    assert j["ml_score_fill_rate"] == 0.5
    assert j["hold_sec_mean"] == 12.0


def test_cro_verdict_enums() -> None:
    assert (
        cro_verdict_from_evidence(
            {
                "funnel": {"ml_finite_rate": 0.0, "approved": 0, "rejected": 10},
                "feature_health": {},
                "latency": {},
                "daytime_pnl": {},
                "data_plane": {"feeds_ok": True},
            }
        )
        == "BLIND"
    )
    assert (
        cro_verdict_from_evidence(
            {
                "funnel": {"ml_finite_rate": 0.9, "approved": 0, "rejected": 20},
                "feature_health": {"degenerate_rate": 0.1},
                "latency": {},
                "daytime_pnl": {},
                "data_plane": {"feeds_ok": True, "rest_pressure_high": False},
            }
        )
        == "GATES OVER-FILTERING"
    )
    assert (
        cro_verdict_from_evidence(
            {
                "funnel": {"ml_finite_rate": 0.9, "approved": 5, "rejected": 5},
                "feature_health": {"degenerate_rate": 0.8},
                "latency": {"under_20ms_p99": True},
                "daytime_pnl": {"n": 0},
                "data_plane": {"feeds_ok": True, "http_429_hits": 0},
            }
        )
        == "WEAK"
    )


def test_percentile_single() -> None:
    assert percentile([7.5], 99) == 7.5
    assert math.isfinite(percentile([1.0, 2.0], 95) or 0)
