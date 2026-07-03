"""
Atomic execution gateway — Phase 2 IG dumb-pipe routing & risk shield.

Enforces radio silence on passive IG REST during monitoring, hard-locked £10k/£350/£750
risk registers, whole-lot integer truncation, and single-shot market dispatch with
trailing stop attachment.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any

from apex.hardening import (
    BASELINE_EQUITY_GBP,
    PER_ASSET_RISK_CAP_GBP,
    PORTFOLIO_RISK_CEILING_GBP,
    floor_contract_size,
    under_min_lot_detail,
)
from signals.signal_engine import SignalResult
from system.engine_log import log_engine

_ORDER_LANE_DEPTH = threading.local()
_MONITORING_LOCK = threading.RLock()
_MONITORING_MODE = True

LATENCY_FLOOR_MS = 200.0


def locked_session_equity_gbp() -> float:
    """Uncompromised baseline — never inflate from broker balance drift."""
    return float(BASELINE_EQUITY_GBP)


def locked_per_asset_cap_gbp() -> float:
    return float(PER_ASSET_RISK_CAP_GBP)


def locked_portfolio_ceiling_gbp() -> float:
    return float(PORTFOLIO_RISK_CEILING_GBP)


def is_order_dispatch_route(method: str, path: str) -> bool:
    """IG REST paths permitted during radio-silence monitoring."""
    m = str(method or "").upper()
    p = str(path or "").lower()
    if "/positions" in p and m in ("POST", "PUT", "DELETE"):
        return True
    if "/confirms" in p:
        return True
    if "/workingorders" in p and m in ("POST", "DELETE"):
        return True
    return False


def _boot_hydration_rest_allowed() -> bool:
    """Gate 1–5 and array warmup need passive IG REST (accounts, positions, OHLC)."""
    try:
        from system.system_state import BootPhase, get_system_state

        phase = get_system_state().snapshot_model().phase
        if phase in (
            BootPhase.BOOTING,
            BootPhase.G1,
            BootPhase.G2,
            BootPhase.G3_STREAMING,
            BootPhase.G4,
            BootPhase.G5,
            BootPhase.WARMING,
        ):
            return True
    except Exception:
        pass
    try:
        from apex.warmup_progress import is_warmup_active

        if is_warmup_active():
            return True
    except Exception:
        pass
    return False


def ig_radio_silence_blocks_rest(method: str, path: str) -> bool:
    """
    True when passive IG REST must not run (monitoring radio silence).

    Order dispatch routes remain open on the dumb-pipe lane.
    """
    try:
        from system.soak_live_fire import soak_mode_enabled

        if soak_mode_enabled() and str(method or "").upper() == "GET" and "/positions" in str(path or "").lower():
            return False
    except Exception:
        pass
    if is_order_dispatch_route(method, path):
        return False
    if _boot_hydration_rest_allowed():
        return False
    with _MONITORING_LOCK:
        if not _MONITORING_MODE:
            return False
    depth = int(getattr(_ORDER_LANE_DEPTH, "n", 0) or 0)
    return depth <= 0


def set_monitoring_mode(active: bool) -> None:
    with _MONITORING_LOCK:
        global _MONITORING_MODE
        _MONITORING_MODE = bool(active)


@contextmanager
def order_dispatch_lane():
    """Open the IG order lane — sole exception to monitoring radio silence."""
    prev = int(getattr(_ORDER_LANE_DEPTH, "n", 0) or 0)
    _ORDER_LANE_DEPTH.n = prev + 1
    try:
        yield
    finally:
        _ORDER_LANE_DEPTH.n = max(0, prev)


def _demo_execution_warmup_bypass() -> bool:
    """Demo soak / throughput — do not block orders on microkernel ring warmup."""
    try:
        from system.soak_live_fire import soak_mode_enabled

        if soak_mode_enabled():
            return True
    except Exception:
        pass
    try:
        from system.demo_execution_plane import demo_throughput_active

        if demo_throughput_active():
            return True
    except Exception:
        pass
    try:
        from system.gate_relaxation import demo_soak_enabled

        if demo_soak_enabled():
            return True
    except Exception:
        pass
    return False


def warmup_blocks_execution() -> bool:
    """Project Apex Monolith Core Circuit Breaker."""
    if _demo_execution_warmup_bypass():
        return False
    try:
        from apex import microkernel
        from system.system_state import BootPhase, get_system_state

        phase = get_system_state().snapshot_model().phase
        if not microkernel.is_warmup_complete() or phase == BootPhase.WARMING:
            return True
    except Exception:
        return False
    return False


def assert_execution_allowed() -> str | None:
    if _demo_execution_warmup_bypass():
        return None
    if warmup_blocks_execution():
        return "HOLD: WARMING_CIRCUIT_BREAKER"
    return None


def promote_and_validate_lot(
    sig: SignalResult,
    threshold: float,
    raw_size: float,
) -> tuple[SignalResult, int, str | None]:
    """
  Promote high-confidence WAIT→BUY/SELL and enforce int(size // 1) whole lots.
    """
    from trading.trading_loop import promote_high_confidence_signal

    promoted = promote_high_confidence_signal(sig, threshold)
    try:
        from execution.ig_size_validator import resolve_executable_lot_size
        from system.config_loader import get_config

        lot = resolve_executable_lot_size(
            str(getattr(sig, "epic", "") or ""),
            float(raw_size),
            str(getattr(sig, "signal", "") or ""),
            get_config(),
            None,
        )
        if not lot.ok:
            return promoted, 0, lot.rejection_reason or "HOLD: UNDER_MIN_LOT"
        size_out = float(lot.size)
        if size_out != float(int(size_out)):
            return promoted, int(size_out) if size_out >= 1 else 0, None
        return promoted, int(size_out), None
    except Exception:
        size_int, under_min = floor_contract_size(raw_size)
        if under_min:
            reason = under_min_lot_detail(size_int)
            log_engine(reason)
            return promoted, 0, "HOLD: UNDER_MIN_LOT"
        return promoted, size_int, None


def validate_risk_envelope(*, risk_gbp: float, concurrent_gbp: float = 0.0) -> str | None:
    if risk_gbp > locked_per_asset_cap_gbp():
        return f"HOLD: PER_ASSET_CAP — £{risk_gbp:.0f} > £{locked_per_asset_cap_gbp():.0f}"
    if (concurrent_gbp + risk_gbp) > locked_portfolio_ceiling_gbp():
        return (
            f"HOLD: PORTFOLIO_CEILING — £{concurrent_gbp + risk_gbp:.0f} "
            f"> £{locked_portfolio_ceiling_gbp():.0f}"
        )
    return None


def dispatch_atomic_market_order(
    client: Any,
    *,
    epic: str,
    direction: str,
    size: float,
    stop_distance: float,
    limit_distance: float | None = None,
    currency_code: str = "GBP",
    trailing_distance_points: float | None = None,
) -> dict[str, Any]:
    """
    Single atomic REST market entry with trailing stop on the dumb-pipe lane.

    Applies integer lot floor before broker dispatch.
    """
    hold = assert_execution_allowed()
    if hold:
        return {"status": "REJECTED", "rejection_reason": hold, "shadow": False}

    size_int, under_min = floor_contract_size(size)
    if under_min:
        reason = under_min_lot_detail(size_int)
        log_engine(reason)
        return {"status": "REJECTED", "rejection_reason": reason, "shadow": False}

    risk_block = validate_risk_envelope(
        risk_gbp=float(stop_distance) * max(size_int, 1),
    )
    if risk_block:
        log_engine(risk_block)
        return {"status": "REJECTED", "rejection_reason": risk_block, "shadow": False}

    stop = float(stop_distance)
    if trailing_distance_points is not None and float(trailing_distance_points) > 0:
        stop = max(stop, float(trailing_distance_points))

    t0 = time.perf_counter()
    with order_dispatch_lane():
        result = client.place_market_order(
            epic=epic,
            direction=direction,
            size=float(size_int),
            stop_distance=stop,
            limit_distance=limit_distance,
            currency_code=currency_code,
        )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    if isinstance(result, dict):
        result["gateway_latency_ms"] = round(elapsed_ms, 3)
        result["dispatch_size_int"] = size_int
        if elapsed_ms > LATENCY_FLOOR_MS:
            log_engine(
                f"AtomicGateway: dispatch latency {elapsed_ms:.1f}ms "
                f"exceeds {LATENCY_FLOOR_MS:.0f}ms floor epic={epic}"
            )
    return result


def reset_gateway_for_tests() -> None:
    set_monitoring_mode(True)
    _ORDER_LANE_DEPTH.n = 0
