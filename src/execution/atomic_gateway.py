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
    try:
        from system.agent_execution_mode import demo_sandbox_unblock_active

        if demo_sandbox_unblock_active():
            return False
    except Exception:
        pass
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
    from execution.maintenance_detachment import is_core_detached, suppress_order_dispatch
    from execution.order_in_flight_mutex import (
        hard_cap_blocks_entry,
        mutex_veto_payload,
        release_order_mutex,
        try_acquire_order_mutex,
    )

    if is_core_detached():
        return suppress_order_dispatch(
            source="atomic_gateway",
            epic=str(epic),
            direction=str(direction),
            action="entry",
            shadow=False,
            dispatch_size_int=int(size),
        )

    hold = assert_execution_allowed()
    if hold:
        return {"status": "REJECTED", "rejection_reason": hold, "shadow": False}

    account_id = str(getattr(client, "account_id", "") or "").strip().upper()
    try:
        from execution.streak_protection import check_streak_entry_allowed

        streak_ok, streak_reason = check_streak_entry_allowed(
            account_id,
            epic=str(epic),
            direction=str(direction or ""),
        )
        if not streak_ok:
            log_engine(f"AtomicGateway: {streak_reason}")
            return {
                "status": "REJECTED",
                "rejection_reason": streak_reason or "streak_protection",
                "shadow": False,
            }
    except Exception as streak_exc:
        reason = f"streak_protection_fail_closed:{type(streak_exc).__name__}"
        log_engine(f"AtomicGateway: {reason}")
        return {"status": "REJECTED", "rejection_reason": reason, "shadow": False}

    cap_blocked, cap_reason = hard_cap_blocks_entry(account_id, rest=client)
    if cap_blocked:
        log_engine(cap_reason)
        return {
            "status": "REJECTED",
            "rejection_reason": cap_reason,
            "shadow": False,
            "account_hard_cap": True,
        }

    if not try_acquire_order_mutex(
        account_id, epic=str(epic), source="atomic_gateway"
    ):
        return mutex_veto_payload(account_id=account_id, source="atomic_gateway")

    size_int, under_min = floor_contract_size(size)
    if under_min:
        reason = under_min_lot_detail(size_int)
        log_engine(reason)
        release_order_mutex(account_id, reason="under_min_lot", filled=False)
        return {"status": "REJECTED", "rejection_reason": reason, "shadow": False}

    risk_block = validate_risk_envelope(
        risk_gbp=float(stop_distance) * max(size_int, 1),
    )
    if risk_block:
        log_engine(risk_block)
        release_order_mutex(account_id, reason="risk_envelope", filled=False)
        return {"status": "REJECTED", "rejection_reason": risk_block, "shadow": False}

    stop = float(stop_distance)
    if trailing_distance_points is not None and float(trailing_distance_points) > 0:
        stop = max(stop, float(trailing_distance_points))

    terminal = False
    filled = False
    t0 = time.perf_counter()
    try:
        with order_dispatch_lane():
            result = client.place_market_order(
                epic=epic,
                direction=direction,
                size=float(size_int),
                stop_distance=stop,
                limit_distance=limit_distance,
                currency_code=currency_code,
            )
        terminal = True
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if isinstance(result, dict):
            result["gateway_latency_ms"] = round(elapsed_ms, 3)
            result["dispatch_size_int"] = size_int
            if elapsed_ms > LATENCY_FLOOR_MS:
                log_engine(
                    f"AtomicGateway: dispatch latency {elapsed_ms:.1f}ms "
                    f"exceeds {LATENCY_FLOOR_MS:.0f}ms floor epic={epic}"
                )
            # Hard-cap accounts already reserved a ledger slot in try_acquire;
            # only note opens for uncapped accounts (legacy path).
            if result.get("dealReference"):
                filled = True
                from execution.order_in_flight_mutex import (
                    resolve_account_hard_open_cap,
                    note_account_open,
                )

                if resolve_account_hard_open_cap(account_id) is None:
                    try:
                        note_account_open(account_id, delta=1)
                    except Exception:
                        pass
        return result
    except (ConnectionError, TimeoutError, BrokenPipeError, OSError):
        terminal = False
        raise
    except Exception:
        terminal = True
        raise
    finally:
        if terminal:
            release_order_mutex(
                account_id, reason="broker_confirm_or_reject", filled=filled
            )


def reset_gateway_for_tests() -> None:
    set_monitoring_mode(True)
    _ORDER_LANE_DEPTH.n = 0
    try:
        from execution.order_in_flight_mutex import reset_order_mutex_for_tests

        reset_order_mutex_for_tests()
    except Exception:
        pass
