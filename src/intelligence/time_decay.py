"""
Time-decay trailing stop telemetry — stall counter and ATR compression ratio.

Pure in-memory lookups for cockpit telemetry; no I/O on the hot execution path.
Compresses ATR stop boundaries by 15% every 10s once a position stalls >45s.
"""

from __future__ import annotations

import threading
from typing import Any

STALL_ACTIVATION_SEC = 45
DECAY_INTERVAL_SEC = 10
DECAY_STEP_PCT = 15
DECAY_MAX_PCT = 75

_lock = threading.Lock()
_stall_by_deal: dict[str, float] = {}


def reset_time_decay_state_for_tests() -> None:
    with _lock:
        _stall_by_deal.clear()


def note_stall_seconds(deal_id: str, stall_seconds: float) -> None:
    """Optional hot-path hook — records per-deal stall without blocking callers."""
    key = str(deal_id or "").strip()
    if not key:
        return
    with _lock:
        _stall_by_deal[key] = max(0.0, float(stall_seconds))


def _position_stall_seconds(row: dict[str, Any]) -> float:
    deal_id = str(row.get("dealId") or row.get("deal_id") or "").strip()
    if deal_id:
        with _lock:
            cached = _stall_by_deal.get(deal_id)
        if cached is not None:
            return max(0.0, float(cached))

    for key in ("stall_seconds", "time_open_sec"):
        raw = row.get(key)
        if raw is not None:
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                pass
    for key in ("open_mins", "time_open_mins"):
        raw = row.get(key)
        if raw is not None:
            try:
                return max(0.0, float(raw) * 60.0)
            except (TypeError, ValueError):
                pass
    return 0.0


def max_stall_seconds(position_map: dict[str, dict[str, Any]]) -> float:
    if not position_map:
        return 0.0
    return max(_position_stall_seconds(row) for row in position_map.values())


def compression_ratio(stall_seconds: float) -> float:
    """Active ATR trailing compression ratio (0.0–1.0)."""
    stall = max(0.0, float(stall_seconds))
    if stall < STALL_ACTIVATION_SEC:
        return 0.0
    steps = int((stall - STALL_ACTIVATION_SEC) // DECAY_INTERVAL_SEC) + 1
    pct = min(DECAY_MAX_PCT, steps * DECAY_STEP_PCT)
    return round(pct / 100.0, 4)


def scalping_time_decay_telemetry(
    position_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    stall = max_stall_seconds(position_map)
    active = stall >= STALL_ACTIVATION_SEC
    compress_pct = int(round(compression_ratio(stall) * 100.0))
    return {
        "active": active,
        "stall_seconds": int(round(stall)),
        "atr_compress_pct": compress_pct,
        "compression_ratio": compression_ratio(stall),
        "interval_sec": DECAY_INTERVAL_SEC,
        "step_pct": DECAY_STEP_PCT,
        "activation_sec": STALL_ACTIVATION_SEC,
    }
