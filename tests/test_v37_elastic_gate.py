"""V37 Phase 3 — Volatility-Adaptive ElasticGate (never loosen P below 0.68)."""

from __future__ import annotations

import pytest

from runtime.overnight_entry_policy import (
    evaluate_elastic_gate,
    evaluate_selectivity_gates,
    resolve_elastic_min_p,
)

DOW = "IX.D.DOW.IFM.IP"

_CFG_ELASTIC = {
    "selectivity_gates": {
        "min_ml_p_success": 0.78,
        "min_abs_obi": 0.25,
        "require_15m_trend_ml_obi": True,
        "allow_non_dow": False,
        "elastic_gate_enabled": True,
    },
    "elastic_gate": {
        "enabled": True,
        "healthy_p_lo": 0.68,
        "healthy_p_hi": 0.72,
        "stressed_p_lo": 0.78,
        "stressed_p_hi": 0.82,
    },
    "micro_scalp_instant": {
        "min_ml_p_success": 0.78,
        "require_15m_trend_ml_obi": True,
    },
}


def test_healthy_plane_p_band_not_below_068() -> None:
    min_p, band = resolve_elastic_min_p(
        spread_elasticity=1.05,
        abs_obi=0.40,
        obi_available=True,
        depth_expanding=True,
        cfg=_CFG_ELASTIC,
    )
    assert band == "healthy"
    assert 0.68 <= min_p <= 0.72


def test_stressed_wide_spread_raises_p_floor() -> None:
    min_p, band = resolve_elastic_min_p(
        spread_elasticity=1.80,
        abs_obi=0.30,
        obi_available=True,
        cfg=_CFG_ELASTIC,
    )
    assert band == "stressed"
    assert 0.78 <= min_p <= 0.82


def test_obi_unavailable_rejects() -> None:
    eg = evaluate_elastic_gate(
        epic=DOW,
        direction="BUY",
        p_success=0.90,
        obi=None,
        obi_available=False,
        trend_15m="BULLISH",
        cfg=_CFG_ELASTIC,
        force_require=True,
    )
    assert eg.allow is False
    assert "obi_unavailable" in eg.reason
    assert eg.band == "obi_unavailable"


def test_elastic_never_loosens_to_064() -> None:
    min_p, _band = resolve_elastic_min_p(
        spread_elasticity=1.0,
        abs_obi=0.90,
        obi_available=True,
        depth_expanding=True,
        cfg={
            **_CFG_ELASTIC,
            "elastic_gate": {
                **_CFG_ELASTIC["elastic_gate"],
                "healthy_p_lo": 0.50,  # operator mistake — must clamp
            },
        },
    )
    assert min_p >= 0.68


def test_healthy_accepts_p_070_with_obi() -> None:
    eg = evaluate_elastic_gate(
        epic=DOW,
        direction="BUY",
        p_success=0.70,
        obi=0.55,
        obi_available=True,
        spread_elasticity=1.05,
        depth_expanding=True,
        trend_15m="BULLISH",
        cfg=_CFG_ELASTIC,
        force_require=True,
    )
    assert eg.allow is True
    assert eg.band == "healthy"
    assert 0.68 <= eg.min_p <= 0.72
    assert float(eg.p_success or 0) >= eg.min_p


def test_stressed_rejects_p_070() -> None:
    eg = evaluate_elastic_gate(
        epic=DOW,
        direction="BUY",
        p_success=0.70,
        obi=0.30,
        obi_available=True,
        spread_elasticity=1.90,
        trend_15m="BULLISH",
        cfg=_CFG_ELASTIC,
        force_require=True,
    )
    assert eg.allow is False
    assert "elastic_p_fail" in eg.reason
    assert eg.min_p >= 0.78


def test_selectivity_delegates_to_elastic_when_enabled() -> None:
    sel = evaluate_selectivity_gates(
        epic=DOW,
        direction="BUY",
        p_success=0.90,
        obi=None,
        obi_available=False,
        trend_15m="BULLISH",
        cfg=_CFG_ELASTIC,
        force_require=True,
    )
    assert sel.allow is False
    assert "obi_unavailable" in sel.reason


def test_abs_obi_gate_still_required_when_available() -> None:
    eg = evaluate_elastic_gate(
        epic=DOW,
        direction="BUY",
        p_success=0.85,
        obi=0.10,  # below 0.25 floor
        obi_available=True,
        spread_elasticity=1.05,
        depth_expanding=True,
        trend_15m="BULLISH",
        cfg=_CFG_ELASTIC,
        force_require=True,
    )
    assert eg.allow is False
    assert "elastic_obi_fail" in eg.reason
