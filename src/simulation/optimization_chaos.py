"""
Optimization chaos middleware — spread widening + fill latency under matrix search.

Active when ``IG_MATRIX_OPTIMIZATION=1`` or HARDENED_TESTBED + optimization flag.
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from typing import Any

_SPREAD_MIN_PIPS = 0.5
_SPREAD_MAX_PIPS = 1.5
_LATENCY_MIN_MS = 20.0
_LATENCY_MAX_MS = 250.0


def chaos_enabled() -> bool:
    if os.environ.get("IG_MATRIX_OPTIMIZATION", "").strip() in ("1", "true", "yes"):
        return True
    try:
        from system.apex_runtime_mode import ApexRuntimeMode, get_apex_runtime_mode

        if get_apex_runtime_mode() is ApexRuntimeMode.HARDENED_TESTBED:
            return os.environ.get("IG_OPTIMIZATION_CHAOS", "1").strip() not in (
                "0",
                "false",
                "no",
            )
    except Exception:
        pass
    return False


def _pip_size(epic: str) -> float:
    key = str(epic or "").upper()
    if "EURUSD" in key:
        return 0.0001
    if "CFPGOLD" in key or "GOLD" in key:
        return 0.01
    return 0.01


@dataclass(frozen=True)
class ChaosSample:
    spread_pips: float
    latency_ms: float


def sample_chaos(*, epic: str = "") -> ChaosSample:
    """Draw one spread widen + latency pair for this fill/tick."""
    return ChaosSample(
        spread_pips=random.uniform(_SPREAD_MIN_PIPS, _SPREAD_MAX_PIPS),
        latency_ms=random.uniform(_LATENCY_MIN_MS, _LATENCY_MAX_MS),
    )


def widen_spread(
    bid: float,
    offer: float,
    *,
    epic: str = "",
    spread_pips: float | None = None,
) -> tuple[float, float, float]:
    """Widen bid/offer by random half-spread on each side. Returns (bid, offer, pips)."""
    if bid <= 0 or offer <= 0:
        return bid, offer, 0.0
    pips = spread_pips if spread_pips is not None else sample_chaos(epic=epic).spread_pips
    half = _pip_size(epic) * pips * 0.5
    return max(1e-12, bid - half), offer + half, pips


def apply_fill_latency(*, latency_ms: float | None = None) -> float:
    """Block caller for simulated transport latency; returns applied ms."""
    if not chaos_enabled():
        return 0.0
    ms = latency_ms if latency_ms is not None else sample_chaos().latency_ms
    if ms > 0:
        time.sleep(ms / 1000.0)
    return ms


def slippage_cost_gbp(
    epic: str,
    *,
    spread_pips: float | None = None,
    legs: int = 2,
    pip_scale: float | None = None,
) -> float:
    """Round-trip slippage debit in price*scale units (matches matrix backtest PnL)."""
    pips = spread_pips if spread_pips is not None else sample_chaos(epic=epic).spread_pips
    scale = pip_scale if pip_scale is not None else (10000.0 if "EURUSD" in epic else 1.0)
    return _pip_size(epic) * pips * legs * scale


def chaos_audit_row(epic: str, sample: ChaosSample) -> dict[str, Any]:
    return {
        "chaos_spread_pips": round(sample.spread_pips, 3),
        "chaos_latency_ms": round(sample.latency_ms, 2),
        "epic": epic,
    }
