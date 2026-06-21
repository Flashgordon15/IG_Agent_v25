"""
Zero-latency Pre-Baked Alpha Matrix lookup bridge — Live Vanguard (:8080).

Attaches to the POSIX shared memory segment compiled on the shadow track and
performs direct pointer lookups on every live tick (no gate recompute, no file I/O).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception


@dataclass(frozen=True)
class AlphaLookupResult:
    hit: bool
    approved: bool
    cell_index: int
    signal_floor: float
    fitness_floor: float
    ml_floor: float
    win_probability: float
    samples: float
    rsi_q: int
    atr_q: int
    mom_q: int
    latency_us: float
    reason: str = ""


def prebaked_alpha_matrix_live_active() -> bool:
    """Live path uses ring-buffer / matrix lookup when unified or live track."""
    try:
        from system.ipc.ring_buffer import unified_engine_active

        if unified_engine_active():
            return True
    except Exception:
        pass
    try:
        from intelligence.shadow_brain_loop import shadow_brain_active

        if shadow_brain_active():
            return False
    except Exception:
        pass
    try:
        from system.identity.shared_memory_bridge import resolve_parallel_track_key

        track = resolve_parallel_track_key()
        if track in ("live", "unified"):
            pass
        else:
            return False
    except Exception:
        port = os.environ.get("IG_API_PORT", "").strip()
        if port == "9199":
            return False
    flag = os.environ.get("IG_PREBAKED_ALPHA_MATRIX", "1").strip().lower()
    return flag not in ("0", "false", "no", "off")


def lookup_alpha_decision(
    *,
    epic: str,
    direction: str,
    rsi: float,
    atr: float,
    momentum: float,
) -> AlphaLookupResult:
    """Direct shared-memory pointer lookup — sub-microsecond hot path."""
    t0 = time.perf_counter()
    side = str(direction or "").upper()
    if side not in ("BUY", "SELL"):
        return AlphaLookupResult(
            hit=False,
            approved=False,
            cell_index=-1,
            signal_floor=0.0,
            fitness_floor=0.0,
            ml_floor=0.0,
            win_probability=0.0,
            samples=0.0,
            rsi_q=0,
            atr_q=0,
            mom_q=0,
            latency_us=0.0,
            reason="invalid_direction",
        )

    try:
        from intelligence.matrix_prebaker import (
            COL_APPROVED,
            COL_FITNESS_FLOOR,
            COL_ML_FLOOR,
            COL_SAMPLES,
            COL_SIGNAL_FLOOR,
            COL_WIN_PROB,
            epic_slot,
            get_alpha_matrix_segment,
            matrix_cell_index,
            quantize_atr,
            quantize_momentum,
            quantize_rsi,
        )

        segment = get_alpha_matrix_segment(create=False)
        header = segment.read_header()
        if not header.get("ready"):
            return AlphaLookupResult(
                hit=False,
                approved=False,
                cell_index=-1,
                signal_floor=0.0,
                fitness_floor=0.0,
                ml_floor=0.0,
                win_probability=0.0,
                samples=0.0,
                rsi_q=0,
                atr_q=0,
                mom_q=0,
                latency_us=(time.perf_counter() - t0) * 1_000_000.0,
                reason="matrix_not_ready",
            )

        rsi_q = quantize_rsi(rsi)
        atr_q = quantize_atr(atr, epic=epic)
        mom_q = quantize_momentum(momentum)
        cell_idx = matrix_cell_index(
            epic_id=epic_slot(epic),
            direction=side,
            rsi_q=rsi_q,
            atr_q=atr_q,
            mom_q=mom_q,
        )
        row = segment.matrix[cell_idx]
        samples = float(row[COL_SAMPLES])
        approved_flag = float(row[COL_APPROVED]) >= 0.5
        hit = samples > 0.0
        latency_us = (time.perf_counter() - t0) * 1_000_000.0

        if hit and approved_flag:
            try:
                segment.increment_lookup_hits()
            except Exception as exc:
                log_guarded_exception("alpha_matrix_lookup_hits", exc)

        return AlphaLookupResult(
            hit=hit,
            approved=hit and approved_flag,
            cell_index=int(cell_idx),
            signal_floor=float(row[COL_SIGNAL_FLOOR]),
            fitness_floor=float(row[COL_FITNESS_FLOOR]),
            ml_floor=float(row[COL_ML_FLOOR]),
            win_probability=float(row[COL_WIN_PROB]),
            samples=samples,
            rsi_q=rsi_q,
            atr_q=atr_q,
            mom_q=mom_q,
            latency_us=latency_us,
            reason="" if hit else "cell_empty",
        )
    except FileNotFoundError:
        return AlphaLookupResult(
            hit=False,
            approved=False,
            cell_index=-1,
            signal_floor=0.0,
            fitness_floor=0.0,
            ml_floor=0.0,
            win_probability=0.0,
            samples=0.0,
            rsi_q=0,
            atr_q=0,
            mom_q=0,
            latency_us=(time.perf_counter() - t0) * 1_000_000.0,
            reason="shm_missing",
        )
    except Exception as exc:
        log_guarded_exception("alpha_matrix_lookup", exc)
        return AlphaLookupResult(
            hit=False,
            approved=False,
            cell_index=-1,
            signal_floor=0.0,
            fitness_floor=0.0,
            ml_floor=0.0,
            win_probability=0.0,
            samples=0.0,
            rsi_q=0,
            atr_q=0,
            mom_q=0,
            latency_us=(time.perf_counter() - t0) * 1_000_000.0,
            reason=f"lookup_error:{type(exc).__name__}",
        )


def structural_metrics_from_quote(
    *,
    market: str,
    epic: str,
    quote: Any,
    signal_engine: Any,
    indicator_snapshot_fn: Any,
    clock_fn: Any | None = None,
) -> tuple[float, float, float, str]:
    """Structural RSI / ATR / Momentum key — no clock subtraction on hot path."""
    _ = (epic, quote, clock_fn)
    ind = indicator_snapshot_fn(quote)
    rsi = float(ind.get("rsi", 0) or 0)
    atr = float(ind.get("atr", 0) or 0)
    momentum = 0.0
    try:
        buf = getattr(signal_engine, "quotes_by_market", {}).get(market) or []
        if len(buf) >= 2:
            q0 = buf[-2]
            q1 = buf[-1]
            m0 = (float(q0.bid) + float(q0.offer)) / 2.0
            m1 = (float(q1.bid) + float(q1.offer)) / 2.0
            if m0 > 0:
                momentum = (m1 - m0) / m0
    except Exception as exc:
        log_guarded_exception("alpha_matrix_structural_metrics", exc)

    if momentum == 0.0 and rsi > 0:
        if rsi >= 55.0:
            momentum = 0.0008
        elif rsi <= 45.0:
            momentum = -0.0008

    direction = "BUY" if momentum >= 0 else "SELL"
    return rsi, atr, momentum, direction


def log_lookup_telemetry(
    *,
    epic: str,
    market: str,
    lookup: AlphaLookupResult,
    direction: str,
) -> None:
    if not lookup.approved:
        return
    log_engine(
        "ALPHA_MATRIX_HIT "
        f"epic={epic} market={market} dir={direction} "
        f"cell={lookup.cell_index} win_p={lookup.win_probability:.3f} "
        f"latency_us={lookup.latency_us:.1f}"
    )
