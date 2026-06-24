"""
Adaptive Dynamic Volatility Scalping Engine — Platform V2.

Rolling 30-period ATR statistics from pre-baked alpha matrix cells per epic.
Expands entry slip / spread envelope on breakout surges; tightens in liquidity lulls.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np

from platform_v2 import platform_v2_settings

_ATR_WINDOW = 30
_SPREAD_HISTORY = 30
_lock = threading.Lock()
_spread_buffers: dict[str, deque[float]] = {}


@dataclass(frozen=True)
class VolatilityGatewayResult:
    spread_cap: float
    slip_tolerance: float
    regime: str
    atr_mean: float
    atr_std: float
    breakout: bool
    lull: bool


def _settings() -> dict[str, Any]:
    base = platform_v2_settings()
    vol = base.get("adaptive_volatility")
    return dict(vol) if isinstance(vol, dict) else {}


def _record_spread(epic: str, spread: float) -> None:
    if spread <= 0:
        return
    key = str(epic or "").strip()
    with _lock:
        buf = _spread_buffers.setdefault(key, deque(maxlen=_SPREAD_HISTORY))
        buf.append(float(spread))


def epic_matrix_atr_stats(
    epic: str,
    *,
    window: int = _ATR_WINDOW,
    matrix: np.ndarray | None = None,
) -> tuple[float, float, int]:
    """
    Rolling mean/std of COL_ATR_ANCHOR across populated alpha cells for *epic*.
    """
    try:
        from intelligence.matrix_prebaker import (
            ATR_BINS,
            COL_ATR_ANCHOR,
            COL_SAMPLES,
            CELLS_PER_EPIC,
            DIR_SLOTS,
            MOM_BINS,
            RSI_BINS,
            epic_slot,
        )
    except Exception:
        return 1.5, 0.5, 0

    if matrix is None:
        try:
            from intelligence.matrix_prebaker import get_alpha_matrix_segment

            segment = get_alpha_matrix_segment(create=False)
            matrix = segment.matrix
        except Exception:
            return 1.5, 0.5, 0

    slot = epic_slot(epic)
    start = slot * CELLS_PER_EPIC
    end = start + CELLS_PER_EPIC
    slice_rows = matrix[start:end]
    populated = slice_rows[slice_rows[:, COL_SAMPLES] > 0.0]
    if populated.size == 0:
        return 1.5, 0.5, 0

    anchors = populated[:, COL_ATR_ANCHOR].astype(np.float64)
    anchors = anchors[np.isfinite(anchors) & (anchors > 0)]
    if anchors.size == 0:
        return 1.5, 0.5, 0

    tail = anchors[-window:] if anchors.size > window else anchors
    mean = float(np.mean(tail))
    std = float(np.std(tail)) if tail.size > 1 else max(mean * 0.1, 0.1)
    _ = (RSI_BINS, ATR_BINS, MOM_BINS, DIR_SLOTS)
    return mean, std, int(tail.size)


def dynamic_slip_tolerance(
    *,
    epic: str,
    atr_live: float,
    bid: float = 0.0,
    offer: float = 0.0,
    spread: float = 0.0,
) -> VolatilityGatewayResult:
    """Volatility-scaled entry slip tolerance — iron-clad stop remains fixed."""
    cfg = _settings()
    window = int(cfg.get("atr_window", _ATR_WINDOW))
    breakout_mult = float(cfg.get("breakout_slip_mult", 2.5))
    lull_mult = float(cfg.get("lull_slip_mult", 0.65))
    min_tol = float(cfg.get("slip_min_points", 1.5))
    max_tol = float(cfg.get("slip_max_points", 45.0))

    atr_mean, atr_std, _ = epic_matrix_atr_stats(epic, window=window)
    atr_live = max(float(atr_live or 0), atr_mean * 0.05)

    z = (atr_live - atr_mean) / max(atr_std, 1e-6)
    breakout = z >= float(cfg.get("breakout_z", 1.25))
    lull = z <= float(cfg.get("lull_z", -0.5))

    try:
        from intelligence.matrix_backtuner import DEFAULT_EPIC_STOP

        stop_ref = float(DEFAULT_EPIC_STOP.get(epic, 10.0) or 10.0)
    except Exception:
        stop_ref = 10.0

    base_scale = max(stop_ref * 0.15, atr_mean * 0.25, atr_live * 0.25)
    mid = (float(bid) + float(offer)) / 2.0 if bid and offer else 0.0
    if mid > 1000:
        base_scale = max(base_scale, 2.0)

    if breakout:
        tol = base_scale * breakout_mult
        regime = "breakout"
    elif lull:
        tol = base_scale * lull_mult
        regime = "liquidity_lull"
    else:
        tol = base_scale
        regime = "normal"

    # Volume surge proxy — spread spike vs rolling MA unlocks crude/ftse/dax breakouts
    _record_spread(epic, spread)
    with _lock:
        buf = _spread_buffers.get(str(epic or "").strip())
        samples = list(buf) if buf else []
    if len(samples) >= 5:
        ma = sum(samples) / len(samples)
        if spread > ma * float(cfg.get("volume_surge_spread_mult", 1.8)) and breakout:
            tol = max(tol, base_scale * breakout_mult * 1.15)
            regime = "volume_surge"

    tol = max(min_tol, min(max_tol, tol))
    return VolatilityGatewayResult(
        spread_cap=tol,
        slip_tolerance=tol,
        regime=regime,
        atr_mean=atr_mean,
        atr_std=atr_std,
        breakout=breakout,
        lull=lull,
    )


def dynamic_spread_cap_v2(
    *,
    epic: str,
    normal_spread: float,
    spread_multiplier: float,
    atr: float = 0.0,
    live_spread: float = 0.0,
    bid: float = 0.0,
    offer: float = 0.0,
) -> tuple[float, dict[str, Any]]:
    """
    V2 entry spread cap — wraps harmonization baseline with matrix ATR envelope.
    """
    from harmonization.volatility_gate import dynamic_entry_spread_cap

    base_cap = dynamic_entry_spread_cap(
        epic=epic,
        normal_spread=float(normal_spread),
        spread_multiplier=float(spread_multiplier),
        atr=float(atr),
    )
    gateway = dynamic_slip_tolerance(
        epic=epic,
        atr_live=float(atr),
        bid=bid,
        offer=offer,
        spread=float(live_spread),
    )
    if gateway.breakout:
        cap = max(base_cap, gateway.spread_cap)
    elif gateway.lull:
        cap = min(base_cap, max(base_cap * 0.85, gateway.spread_cap))
    else:
        cap = max(base_cap, gateway.spread_cap * 0.9)

    meta = {
        "regime": gateway.regime,
        "atr_mean": round(gateway.atr_mean, 4),
        "atr_std": round(gateway.atr_std, 4),
        "slip_tolerance": round(gateway.slip_tolerance, 3),
        "breakout": gateway.breakout,
        "lull": gateway.lull,
    }
    return float(cap), meta


def apply_v2_entry_gateway(
    *,
    epic: str,
    normal_spread: float,
    spread_multiplier: float,
    atr: float,
    live_spread: float,
    bid: float = 0.0,
    offer: float = 0.0,
) -> tuple[float, dict[str, Any]]:
    """Public hook for trading_loop risk gate."""
    return dynamic_spread_cap_v2(
        epic=epic,
        normal_spread=normal_spread,
        spread_multiplier=spread_multiplier,
        atr=atr,
        live_spread=live_spread,
        bid=bid,
        offer=offer,
    )


def reset_adaptive_volatility_for_tests() -> None:
    with _lock:
        _spread_buffers.clear()
