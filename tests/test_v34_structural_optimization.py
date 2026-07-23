"""v34 structural optimization layer — affinity, vol threshold, TWAP, expectancy."""

from __future__ import annotations

import os
import platform
from unittest.mock import MagicMock

import pytest

from alpha.micro_sniper_ml import (
    THRESHOLD_FX,
    THRESHOLD_GOLD,
    THRESHOLD_INDEX,
    THRESHOLD_LIQUIDITY_STRESS_CEILING,
    QuantumSniperMLCore,
    dynamic_sniper_threshold,
    observe_volatility_features,
    reset_sniper_ml_cache_for_tests,
)
from execution.asymmetric_ioc_router import (
    apply_wr_size_contraction,
    dispatch_asymmetric_ioc_limit,
    dispatch_asymmetric_ioc_twap_async,
    fractional_expectancy_multiplier,
    note_closed_trade_outcome,
    plan_twap_fragments,
    reset_asymmetric_router_state_for_tests,
    rolling_win_rate,
)
from system.core_affinity import pin_current_process_to_engine, resolve_engine_core
from system.engine_lane import ENGINE_ORIGIN_CFD, ENGINE_ORIGIN_SB


EPIC_GOLD = "CS.D.CFPGOLD.CFP.IP"
EPIC_EURUSD = "CS.D.EURUSD.CFD.IP"
EPIC_DOW = "IX.D.DOW.IFM.IP"


@pytest.fixture(autouse=True)
def _clean_state() -> None:
    reset_sniper_ml_cache_for_tests()
    reset_asymmetric_router_state_for_tests()
    old = os.environ.get("CORE_DETACHED")
    yield
    reset_sniper_ml_cache_for_tests()
    reset_asymmetric_router_state_for_tests()
    if old is None:
        os.environ.pop("CORE_DETACHED", None)
    else:
        os.environ["CORE_DETACHED"] = old


def test_core_affinity_no_crash_on_darwin() -> None:
    """macOS lacks sched_setaffinity — pin must no-op without raising."""
    for origin in (ENGINE_ORIGIN_CFD, ENGINE_ORIGIN_SB, None):
        result = pin_current_process_to_engine(origin)
        assert isinstance(result, dict)
        assert result.get("core") in (1, 2, None)
        if platform.system() == "Darwin":
            assert result.get("pinned") is False
            assert result.get("reason") == "sched_setaffinity_unavailable"
        assert resolve_engine_core(ENGINE_ORIGIN_CFD) == 1
        assert resolve_engine_core(ENGINE_ORIGIN_SB) == 2


def test_dynamic_threshold_tightens_gold_on_spread_expansion() -> None:
    """Gold/EURUSD liquidity withdrawal raises gate toward 0.82."""
    assert dynamic_sniper_threshold(EPIC_GOLD) == pytest.approx(THRESHOLD_GOLD)
    for i in range(12):
        observe_volatility_features(
            EPIC_GOLD,
            spread_elasticity=1.05 + i * 0.08,
            atr_velocity=0.4 + i * 0.05,
        )
    for i in range(18):
        observe_volatility_features(
            EPIC_GOLD,
            spread_elasticity=2.2 + i * 0.12,
            atr_velocity=1.2,
        )
    tightened = dynamic_sniper_threshold(EPIC_GOLD)
    assert tightened > THRESHOLD_GOLD
    assert tightened <= THRESHOLD_LIQUIDITY_STRESS_CEILING

    for i in range(30):
        observe_volatility_features(
            EPIC_EURUSD,
            spread_elasticity=1.1 + i * 0.06,
            atr_velocity=0.6,
        )
    fx_thr = dynamic_sniper_threshold(EPIC_EURUSD)
    assert fx_thr > THRESHOLD_FX
    assert fx_thr <= THRESHOLD_LIQUIDITY_STRESS_CEILING

    assert dynamic_sniper_threshold(EPIC_DOW) == pytest.approx(THRESHOLD_INDEX)

    core = QuantumSniperMLCore()
    for i in range(30):
        observe_volatility_features(
            EPIC_GOLD,
            spread_elasticity=1.2 + i * 0.1,
            atr_velocity=0.9,
        )
    result = core.evaluate_entry_probability(
        obi_velocity=0.9,
        spread_elasticity=3.5,
        tick_acceleration=0.5,
        grok_macro_bias="NEUTRAL",
        epic=EPIC_GOLD,
        direction="BUY",
        atr_velocity=1.1,
    )
    assert result.threshold > THRESHOLD_GOLD
    assert result.features.get("atr_velocity") == pytest.approx(1.1)
    assert result.features.get("liquidity_stress_blend", 0) > 0


def test_fractional_expectancy_sizing_curve() -> None:
    """Rolling 10-trade WR maps smoothly — not abrupt 0.5× cliff."""
    assert fractional_expectancy_multiplier() == pytest.approx(1.0)
    for pnl in (2.0, -1.0, -1.0, -1.0, -1.0):
        note_closed_trade_outcome(pnl)
    wr = rolling_win_rate()
    assert wr == pytest.approx(0.2)
    mult = fractional_expectancy_multiplier()
    assert 0.35 < mult < 1.0
    assert mult != pytest.approx(0.5)
    assert apply_wr_size_contraction(1.0) == pytest.approx(mult)
    assert apply_wr_size_contraction(1.0, epic=EPIC_DOW) == pytest.approx(max(0.5, mult))


def test_twap_fragment_planning_and_detach_skips_rest() -> None:
    """Large size shards into clips; CORE_DETACHED suppresses all IG REST."""
    frags = plan_twap_fragments(2.5, epic=EPIC_DOW, cfg=None)
    assert len(frags) >= 2
    assert sum(frags) == pytest.approx(2.5)
    assert all(f >= 0.5 for f in frags)

    os.environ["CORE_DETACHED"] = "TRUE"
    rest = MagicMock()
    rest.place_otc_market_payload = MagicMock()
    rest.place_market_order = MagicMock()

    out = dispatch_asymmetric_ioc_limit(
        rest,
        epic=EPIC_DOW,
        direction="BUY",
        size=2.5,
        bid=45000.0,
        offer=45002.0,
        stop_distance=6.0,
    )
    assert out.get("core_detached") is True
    rest.place_otc_market_payload.assert_not_called()
    rest.place_market_order.assert_not_called()


def test_twap_async_detach_mid_wheel_no_rest() -> None:
    """Async TWAP wheel re-checks detach per clip — zero REST when detached."""
    import asyncio

    os.environ["CORE_DETACHED"] = "TRUE"
    rest = MagicMock()
    rest.place_otc_market_payload = MagicMock()

    out = asyncio.run(
        dispatch_asymmetric_ioc_twap_async(
            rest,
            epic=EPIC_DOW,
            direction="BUY",
            size=2.0,
            bid=45000.0,
            offer=45002.0,
            stop_distance=6.0,
            twap_window_sec=0.01,
        )
    )
    assert out.get("core_detached") is True
    rest.place_otc_market_payload.assert_not_called()
