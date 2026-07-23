#!/usr/bin/env python3
"""
Multi-Asset & Latency Fuzz Stress Harness.

Expands property-based testing across 5 asset-class liquidity profiles,
5 market scenarios, and 5 synthetic latency injection modes.

All tests run against the pure-math volatility_risk_bracket module — no live
agent interaction, no I/O, no network.

Usage:
    PYTHONPATH=src python3 scripts/multi_asset_stress_harness.py
"""

from __future__ import annotations

import math
import random
import sys
import tracemalloc
import time
from dataclasses import dataclass, field
from typing import Iterator

from execution.volatility_risk_bracket import (
    BracketConfig,
    BracketQuote,
    BracketState,
    BracketUpdate,
    simulate_bracket_path,
    update_bracket,
    volatility_ratio,
    dynamic_trail_atr_multiple,
)


@dataclass(frozen=True)
class PropertyResult:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class AssetProfile:
    name: str
    epic: str
    base_mid: float
    atr: float
    spread_half: float
    decimals: int


PROFILES = [
    AssetProfile("FX_EURUSD", "CS.D.EURUSD.CFD.IP", 1.16000, 0.00080, 0.00005, 5),
    AssetProfile("EQUITY_DOW", "IX.D.DOW.IFM.IP", 39500.0, 120.0, 2.5, 1),
    AssetProfile("COMMODITY_GOLD", "CS.D.CFPGOLD.CFP.IP", 2340.0, 18.0, 0.30, 2),
    AssetProfile("CRYPTO_BTC", "CRYPTO.BTC.USD", 62000.0, 1500.0, 25.0, 2),
    AssetProfile("FX_GBPJPY", "CS.D.GBPJPY.CFD.IP", 198.500, 0.650, 0.015, 3),
]


# ---------------------------------------------------------------------------
# Scenario generators
# ---------------------------------------------------------------------------

def _gen_flash_crash(p: AssetProfile, ticks: int = 120) -> list[float]:
    """Deep adverse move at tick 40, partial recovery."""
    crash_start, crash_len = 40, 10
    pre_step = p.atr * 0.005
    crash_step = p.atr * 0.35
    recovery_step = p.atr * 0.02

    mids = [p.base_mid]
    for i in range(1, ticks):
        if i < crash_start:
            mids.append(mids[-1] + pre_step)
        elif i < crash_start + crash_len:
            mids.append(mids[-1] - crash_step)
        else:
            mids.append(mids[-1] + recovery_step)
    return mids


def _gen_macro_gap(p: AssetProfile, ticks: int = 120) -> list[float]:
    """Instantaneous 4x ATR gap at tick 50 with no intermediate prices."""
    gap_tick = 50
    mids = [p.base_mid]
    for i in range(1, ticks):
        if i == gap_tick:
            mids.append(mids[-1] - p.atr * 4.0)
        elif i < gap_tick:
            mids.append(mids[-1] + p.atr * 0.003)
        else:
            mids.append(mids[-1] + p.atr * 0.01)
    return mids


def _gen_liquidity_vacuum(p: AssetProfile, ticks: int = 120) -> list[float]:
    """Spread widens 10x for 15 ticks, simulated by mid noise."""
    vacuum_start, vacuum_len = 35, 15
    mids = [p.base_mid]
    rng = random.Random(42)
    for i in range(1, ticks):
        if vacuum_start <= i < vacuum_start + vacuum_len:
            mids.append(mids[-1] + rng.uniform(-p.atr * 0.3, p.atr * 0.3))
        else:
            mids.append(mids[-1] + p.atr * 0.002)
    return mids


def _gen_momentum_cascade(p: AssetProfile, ticks: int = 120) -> list[float]:
    """Sustained directional 25-tick sell-off."""
    cascade_start, cascade_len = 30, 25
    mids = [p.base_mid]
    for i in range(1, ticks):
        if cascade_start <= i < cascade_start + cascade_len:
            mids.append(mids[-1] - p.atr * 0.15)
        else:
            mids.append(mids[-1] + p.atr * 0.004)
    return mids


def _gen_chop_whipsaw(p: AssetProfile, ticks: int = 120) -> list[float]:
    """Rapid reversals within 1x ATR band."""
    rng = random.Random(99)
    mids = [p.base_mid]
    for _ in range(1, ticks):
        mids.append(mids[-1] + rng.uniform(-p.atr * 0.12, p.atr * 0.12))
    return mids


SCENARIOS = {
    "flash_crash": _gen_flash_crash,
    "macro_gap": _gen_macro_gap,
    "liquidity_vacuum": _gen_liquidity_vacuum,
    "momentum_cascade": _gen_momentum_cascade,
    "chop_whipsaw": _gen_chop_whipsaw,
}


# ---------------------------------------------------------------------------
# Latency injection wrappers
# ---------------------------------------------------------------------------

def _quotes_from_mids(
    mids: list[float], p: AssetProfile, *, atr_override: float | None = None
) -> list[BracketQuote]:
    atr = atr_override or p.atr
    return [
        BracketQuote(
            bid=round(m - p.spread_half, p.decimals),
            offer=round(m + p.spread_half, p.decimals),
            live_atr=atr,
        )
        for m in mids
    ]


def _latency_normal(quotes: list[BracketQuote]) -> list[BracketQuote]:
    return list(quotes)


def _latency_congested(quotes: list[BracketQuote]) -> list[BracketQuote]:
    """Duplicate every 5th quote to simulate stale-quote delivery."""
    out: list[BracketQuote] = []
    for i, q in enumerate(quotes):
        out.append(q)
        if i % 5 == 0:
            out.append(q)
    return out


def _latency_dropout(quotes: list[BracketQuote]) -> list[BracketQuote]:
    """Drop 3 consecutive ticks at indices 45-47 to simulate network gap."""
    return [q for i, q in enumerate(quotes) if not (45 <= i <= 47)]


def _latency_burst_replay(quotes: list[BracketQuote]) -> list[BracketQuote]:
    """At tick 60, deliver 5 previously seen ticks as a burst replay."""
    out = list(quotes[:60])
    out.extend(quotes[55:60])
    out.extend(quotes[60:])
    return out


def _latency_extended_stall(quotes: list[BracketQuote]) -> list[BracketQuote]:
    """Repeat the tick-30 quote 8 times (simulating 2500ms stall), then resume."""
    out = list(quotes[:30])
    for _ in range(8):
        out.append(quotes[30])
    out.extend(quotes[31:])
    return out


LATENCY_MODES = {
    "normal": _latency_normal,
    "congested": _latency_congested,
    "dropout": _latency_dropout,
    "burst_replay": _latency_burst_replay,
    "extended_stall": _latency_extended_stall,
}


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------

def prop_stop_fires_on_deep_crash(
    profile: AssetProfile, scenario: str, latency: str
) -> PropertyResult:
    """Stop must fire when a deep adverse move exceeds 5x ATR."""
    gen = SCENARIOS[scenario]
    mids = gen(profile)
    quotes = LATENCY_MODES[latency](_quotes_from_mids(mids, profile))

    state = BracketState.open_long(entry=profile.base_mid, entry_atr=profile.atr)
    sim = simulate_bracket_path(state, quotes)

    name = f"stop_fires|{profile.name}|{scenario}|{latency}"
    if scenario in ("flash_crash", "macro_gap", "momentum_cascade"):
        if sim.stopped:
            return PropertyResult(name, True, f"stopped tick={sim.stop_tick}")
        return PropertyResult(name, False, "stop never fired on deep adverse move")
    return PropertyResult(name, True, f"non-crash scenario, stopped={sim.stopped}")


def prop_deterministic_replay(
    profile: AssetProfile, scenario: str, latency: str
) -> PropertyResult:
    """Same inputs must produce identical outputs."""
    gen = SCENARIOS[scenario]
    mids = gen(profile)
    quotes = LATENCY_MODES[latency](_quotes_from_mids(mids, profile))

    s1 = BracketState.open_long(entry=profile.base_mid, entry_atr=profile.atr)
    sim1 = simulate_bracket_path(s1, quotes)

    s2 = BracketState.open_long(entry=profile.base_mid, entry_atr=profile.atr)
    sim2 = simulate_bracket_path(s2, quotes)

    name = f"deterministic|{profile.name}|{scenario}|{latency}"
    fp1 = (sim1.stopped, sim1.stop_tick, round(sim1.final_stop, 8), sim1.ticks_processed)
    fp2 = (sim2.stopped, sim2.stop_tick, round(sim2.final_stop, 8), sim2.ticks_processed)
    if fp1 == fp2:
        return PropertyResult(name, True, "fingerprints match")
    return PropertyResult(name, False, f"mismatch: {fp1} vs {fp2}")


def prop_no_exceptions(
    profile: AssetProfile, scenario: str, latency: str
) -> PropertyResult:
    """Zero exceptions across both BUY and SELL sides."""
    name = f"no_exceptions|{profile.name}|{scenario}|{latency}"
    gen = SCENARIOS[scenario]
    mids = gen(profile)
    quotes = LATENCY_MODES[latency](_quotes_from_mids(mids, profile))

    try:
        for side_fn in (BracketState.open_long, BracketState.open_short):
            state = side_fn(entry=profile.base_mid, entry_atr=profile.atr)
            simulate_bracket_path(state, quotes)
    except Exception as exc:
        return PropertyResult(name, False, f"{type(exc).__name__}: {exc}")
    return PropertyResult(name, True, "no exceptions")


def prop_monotonic_ratchet(
    profile: AssetProfile, scenario: str, latency: str
) -> PropertyResult:
    """BUY stop must never decrease; SELL stop must never increase."""
    name = f"monotonic_ratchet|{profile.name}|{scenario}|{latency}"
    gen = SCENARIOS[scenario]
    mids = gen(profile)
    quotes = LATENCY_MODES[latency](_quotes_from_mids(mids, profile))

    violations = 0
    for side_fn, label in ((BracketState.open_long, "BUY"), (BracketState.open_short, "SELL")):
        state = side_fn(entry=profile.base_mid, entry_atr=profile.atr)
        prev_stop = state.stop
        for q in quotes:
            upd = update_bracket(state, q)
            if label == "BUY" and upd.stop < prev_stop - 1e-10:
                violations += 1
            elif label == "SELL" and upd.stop > prev_stop + 1e-10:
                violations += 1
            prev_stop = upd.stop
            if upd.stop_hit:
                break

    if violations == 0:
        return PropertyResult(name, True, "monotonic across both sides")
    return PropertyResult(name, False, f"{violations} ratchet violations")


def prop_memory_bounded(profile: AssetProfile) -> PropertyResult:
    """Peak tracemalloc <= 512KB across 200 simulations per profile."""
    name = f"memory_bounded|{profile.name}"
    tracemalloc.start()
    try:
        for i in range(200):
            seed = 1000 + i
            rng = random.Random(seed)
            mids = [profile.base_mid]
            for _ in range(100):
                mids.append(mids[-1] + rng.uniform(-profile.atr * 0.1, profile.atr * 0.1))
            quotes = _quotes_from_mids(mids, profile)
            state = BracketState.open_long(entry=profile.base_mid, entry_atr=profile.atr)
            simulate_bracket_path(state, quotes)

        _, peak = tracemalloc.get_traced_memory()
        peak_kb = peak / 1024.0
    finally:
        tracemalloc.stop()

    if peak_kb <= 512.0:
        return PropertyResult(name, True, f"peak={peak_kb:.1f}KB <= 512KB")
    return PropertyResult(name, False, f"peak={peak_kb:.1f}KB > 512KB")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_all() -> list[PropertyResult]:
    results: list[PropertyResult] = []

    for profile in PROFILES:
        for scenario in SCENARIOS:
            for latency in LATENCY_MODES:
                results.append(prop_stop_fires_on_deep_crash(profile, scenario, latency))
                results.append(prop_deterministic_replay(profile, scenario, latency))
                results.append(prop_no_exceptions(profile, scenario, latency))
                results.append(prop_monotonic_ratchet(profile, scenario, latency))

        results.append(prop_memory_bounded(profile))

    return results


def main() -> int:
    t0 = time.monotonic()
    results = run_all()
    elapsed = time.monotonic() - t0

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)

    print("=" * 72)
    print("  Multi-Asset & Latency Fuzz Stress Harness")
    print("=" * 72)
    print(f"  Profiles:  {len(PROFILES)}")
    print(f"  Scenarios: {len(SCENARIOS)}")
    print(f"  Latency:   {len(LATENCY_MODES)}")
    print(f"  Total:     {len(results)} property checks")
    print(f"  Elapsed:   {elapsed:.2f}s")
    print()

    failures = [r for r in results if not r.passed]
    if failures:
        print("--- FAILURES ---")
        for r in failures:
            print(f"  [FAIL] {r.name}: {r.detail}")
        print()

    print(f"  Summary: {passed}/{len(results)} passed, {failed} failed")
    print()

    if failed == 0:
        print("  VERDICT: ALL PROPERTY TESTS PASSED")
    else:
        print("  VERDICT: FAILURES DETECTED — review above")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
