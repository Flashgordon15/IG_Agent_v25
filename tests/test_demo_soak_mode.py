"""Tests for demo_soak_mode gate relaxations."""

from __future__ import annotations

import pytest

from system.gate_relaxation import (
    demo_soak_enabled,
    effective_fitness_min,
    effective_trade_confidence_threshold,
    reset_gate_relaxation_cache_for_tests,
    rotation_filter_bypassed,
    soak_ml_veto_bypassed,
    soak_spread_to_atr_max,
)


@pytest.fixture(autouse=True)
def _clear_relax_cache():
    reset_gate_relaxation_cache_for_tests()
    yield
    reset_gate_relaxation_cache_for_tests()


def test_soak_disabled_by_default(monkeypatch):
    monkeypatch.setattr(
        "system.gate_relaxation._soak_block",
        lambda: {"enabled": False},
    )
    assert not demo_soak_enabled()
    assert not rotation_filter_bypassed()
    assert effective_fitness_min("IX.D.NIKKEI.IFM.IP", points_state="HEALTHY") == 55.0


def test_soak_lowers_fitness_floor_all_epics(monkeypatch):
    monkeypatch.setattr(
        "system.gate_relaxation._soak_block",
        lambda: {
            "enabled": True,
            "fitness_min": 50,
            "relax_all_epics": True,
            "require_points_healthy": False,
        },
    )
    assert demo_soak_enabled()
    assert effective_fitness_min("IX.D.NIKKEI.IFM.IP", points_state="WARNING") == 50.0
    assert effective_fitness_min("CS.D.CFPGOLD.CFP.IP", points_state="CAUTION") == 50.0


def test_soak_warning_confidence_cap(monkeypatch):
    monkeypatch.setattr(
        "system.gate_relaxation._soak_block",
        lambda: {
            "enabled": True,
            "warning_confidence_cap": 85,
            "warning_use_instrument_threshold": True,
        },
    )
    capped = effective_trade_confidence_threshold(
        92.0,
        points_state="WARNING",
        instrument_threshold=55.0,
        epic="IX.D.NIKKEI.IFM.IP",
    )
    assert capped == 55.0


def test_soak_bypasses_rotation(monkeypatch):
    monkeypatch.setattr(
        "system.gate_relaxation._soak_block",
        lambda: {"enabled": True, "disable_rotation_filter": True},
    )
    assert rotation_filter_bypassed()


def test_soak_bypasses_ml_veto(monkeypatch):
    monkeypatch.setattr(
        "system.gate_relaxation._soak_block",
        lambda: {"enabled": True, "bypass_ml_veto": True},
    )
    assert soak_ml_veto_bypassed()


def test_soak_raises_spread_atr_max(monkeypatch):
    monkeypatch.setattr(
        "system.gate_relaxation._soak_block",
        lambda: {"enabled": True, "spread_to_atr_circuit_max": 0.45},
    )
    assert soak_spread_to_atr_max(0.30) == 0.45
