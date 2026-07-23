#!/usr/bin/env python3
"""
Property-based flash-crash harness for volatility_risk_bracket.

Simulates highly volatile market events and asserts stop-loss triggers
deterministically without exceptions or unbounded memory growth.

Run:
  PYTHONPATH=src .venv/bin/python3 scripts/flash_crash_risk_bracket_property_test.py
"""

from __future__ import annotations

import random
import sys
import tracemalloc
from dataclasses import dataclass

from execution.volatility_risk_bracket import (
    BracketConfig,
    BracketQuote,
    BracketState,
    simulate_bracket_path,
)
from stress.historical_feed import HistoricalScenario, ScenarioKind


@dataclass(frozen=True)
class PropertyResult:
    name: str
    passed: bool
    detail: str


def _flash_crash_mids(
    *,
    base_mid: float = 1.16000,
    crash_start: int = 40,
    crash_len: int = 10,
    crash_step: float = 0.00120,
    recovery_step: float = 0.00015,
    pre_step: float = 0.00002,
    total: int = 120,
) -> list[float]:
    mid = float(base_mid)
    out: list[float] = []
    for i in range(total):
        if crash_start <= i < crash_start + crash_len:
            mid = round(mid - crash_step, 5)
        elif i >= crash_start + crash_len:
            mid = round(mid + recovery_step, 5)
        else:
            mid = round(mid + pre_step, 5)
        out.append(mid)
    return out


def _quotes_from_mids(mids: list[float], spread_half: float = 0.00005) -> list[BracketQuote]:
    sh = float(spread_half)
    return [
        BracketQuote(bid=round(m - sh, 5), offer=round(m + sh, 5))
        for m in mids
    ]


def prop_historical_flash_crash_long() -> PropertyResult:
    scenario = HistoricalScenario(
        kind=ScenarioKind.FLASH_CRASH,
        epic="CS.D.EURUSD.CFD.IP",
        base_mid=1.16000,
    )
    quotes = [
        BracketQuote(bid=t.bid, offer=t.offer)
        for t in scenario.ticks()
    ]
    state = BracketState.open_long(entry=1.16000, entry_atr=0.00040)
    sim = simulate_bracket_path(state, quotes)
    ok = sim.stopped and sim.stop_tick >= 40
    return PropertyResult(
        "historical_flash_crash_long",
        ok,
        f"stopped={sim.stopped} tick={sim.stop_tick} exit={sim.exit_px:.5f}",
    )


def prop_historical_flash_crash_short() -> PropertyResult:
    scenario = HistoricalScenario(
        kind=ScenarioKind.FLASH_CRASH,
        epic="CS.D.EURUSD.CFD.IP",
        base_mid=1.16000,
    )
    # Invert crash → spike against short
    quotes: list[BracketQuote] = []
    for tick in scenario.ticks():
        inv_mid = round(2 * 1.16000 - (tick.bid + tick.offer) * 0.5, 5)
        sh = 0.00005
        quotes.append(BracketQuote(bid=round(inv_mid - sh, 5), offer=round(inv_mid + sh, 5)))
    state = BracketState.open_short(entry=1.16000, entry_atr=0.00040)
    sim = simulate_bracket_path(state, quotes)
    ok = sim.stopped and sim.stop_tick >= 40
    return PropertyResult(
        "historical_flash_crash_short",
        ok,
        f"stopped={sim.stopped} tick={sim.stop_tick} exit={sim.exit_px:.5f}",
    )


def prop_deterministic_replay(seed: int = 42, trials: int = 64) -> PropertyResult:
    rng = random.Random(seed)
    fingerprints: list[tuple[int, float, float]] = []
    for _ in range(trials):
        crash_step = rng.uniform(0.00080, 0.00200)
        entry_atr = rng.uniform(0.00025, 0.00060)
        mids = _flash_crash_mids(crash_step=crash_step)
        quotes = _quotes_from_mids(mids)
        state_a = BracketState.open_long(entry=1.16000, entry_atr=entry_atr)
        sim_a = simulate_bracket_path(state_a, quotes)
        state_b = BracketState.open_long(entry=1.16000, entry_atr=entry_atr)
        sim_b = simulate_bracket_path(state_b, quotes)
        if sim_a != sim_b:
            return PropertyResult(
                "deterministic_replay",
                False,
                f"mismatch crash_step={crash_step:.6f} atr={entry_atr:.6f}",
            )
        fingerprints.append((sim_a.stop_tick, sim_a.final_stop, sim_a.exit_px))
    return PropertyResult(
        "deterministic_replay",
        True,
        f"{trials} replays identical, sample={fingerprints[0]}",
    )


def prop_no_exceptions_invariant(trials: int = 512) -> PropertyResult:
    rng = random.Random(20260703)
    errors = 0
    for n in range(trials):
        side = "BUY" if n % 2 == 0 else "SELL"
        crash_step = rng.uniform(0.00050, 0.00250)
        entry_atr = rng.uniform(0.00015, 0.00080)
        mids = _flash_crash_mids(
            crash_start=rng.randint(20, 60),
            crash_len=rng.randint(5, 15),
            crash_step=crash_step,
            total=rng.randint(80, 160),
        )
        quotes = _quotes_from_mids(mids)
        try:
            if side == "BUY":
                state = BracketState.open_long(entry=1.16000, entry_atr=entry_atr)
            else:
                state = BracketState.open_short(entry=1.16000, entry_atr=entry_atr)
            simulate_bracket_path(state, quotes)
        except Exception as exc:
            errors += 1
            if errors == 1:
                return PropertyResult(
                    "no_exceptions",
                    False,
                    f"trial={n} {type(exc).__name__}: {exc}",
                )
    return PropertyResult("no_exceptions", True, f"{trials} trials, 0 exceptions")


def prop_memory_bounded(trials: int = 400, max_peak_kb: float = 512.0) -> PropertyResult:
    rng = random.Random(99)
    tracemalloc.start()
    try:
        for n in range(trials):
            crash_step = rng.uniform(0.00080, 0.00200)
            mids = _flash_crash_mids(crash_step=crash_step, total=120)
            quotes = _quotes_from_mids(mids)
            state = BracketState.open_long(entry=1.16000, entry_atr=0.00040)
            simulate_bracket_path(state, quotes)
        _current, peak = tracemalloc.get_traced_memory()
        peak_kb = peak / 1024.0
        ok = peak_kb <= max_peak_kb
        return PropertyResult(
            "memory_bounded",
            ok,
            f"peak={peak_kb:.1f}KB limit={max_peak_kb:.0f}KB over {trials} sims",
        )
    finally:
        tracemalloc.stop()


def prop_stop_always_fires_on_deep_crash(trials: int = 128) -> PropertyResult:
    rng = random.Random(7)
    for n in range(trials):
        crash_step = rng.uniform(0.00100, 0.00300)
        entry_atr = rng.uniform(0.00020, 0.00050)
        mids = _flash_crash_mids(crash_step=crash_step)
        quotes = _quotes_from_mids(mids)
        state = BracketState.open_long(entry=1.16000, entry_atr=entry_atr)
        sim = simulate_bracket_path(state, quotes, cfg=BracketConfig())
        if not sim.stopped:
            return PropertyResult(
                "deep_crash_stop",
                False,
                f"trial={n} crash_step={crash_step:.5f} not stopped",
            )
    return PropertyResult("deep_crash_stop", True, f"{trials} deep crashes all stopped")


def main() -> int:
    props = [
        prop_historical_flash_crash_long(),
        prop_historical_flash_crash_short(),
        prop_deterministic_replay(),
        prop_no_exceptions_invariant(),
        prop_memory_bounded(),
        prop_stop_always_fires_on_deep_crash(),
    ]
    failed = [p for p in props if not p.passed]
    print("=== Volatility Risk Bracket — Property Tests ===")
    for p in props:
        mark = "PASS" if p.passed else "FAIL"
        print(f"  [{mark}] {p.name}: {p.detail}")
    print(f"\nSummary: {len(props) - len(failed)}/{len(props)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
