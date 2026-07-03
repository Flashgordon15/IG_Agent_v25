"""
Iron Clad Risk Engine — non-configurable transmission envelope.

Enforced at LiveExecutor immediately before IG REST dispatch.
Stricter than CapitalGuard where overlap exists (1.5% vs 2% drawdown).
"""

from __future__ import annotations

import math
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception

MAX_ORDER_SIZE = 1.0
V2_MAX_ORDER_SIZE = 4.0
MANDATORY_STOP_POINTS = 10.0
MANDATORY_LIMIT_POINTS = 20.0
MAX_DAILY_DRAWDOWN_PCT = 0.015
MAX_SLIPPAGE_POINTS = 1.5
ENTRY_SPREAD_BUFFER_MIN = 1.5
ENTRY_SPREAD_BUFFER_MAX = 45.0
_ROLLING_WINDOW_SEC = 86400.0
_HARD_EXIT = 99


@dataclass(frozen=True)
class OrderEnvelope:
    epic: str
    direction: str
    size: float
    stop_distance: float
    limit_distance: float
    bid: float
    offer: float
    expected_mid: float
    spread_points: float


class IronCladRiskEngine:
    """Immutable risk envelope — bypasses ML and strategy overrides."""

    _lock = threading.Lock()
    _session_equity: float | None = None
    _session_start_mono: float = 0.0
    _tripped: bool = False

    @staticmethod
    def effective_max_order_size() -> float:
        try:
            from system.demo_execution_plane import demo_throughput_active

            if demo_throughput_active():
                # Spreadbet gold demo floor — must not cap below IG minimum.
                return 10.0
        except Exception:
            pass
        try:
            from platform_v2 import platform_v2_enabled
            from platform_v2.compound_profit_escalation import v2_max_order_size

            if platform_v2_enabled():
                return float(v2_max_order_size())
        except Exception as exc:
            log_guarded_exception("iron_clad_v2_size", exc)
        return MAX_ORDER_SIZE

    @staticmethod
    def entry_spread_tolerance_points(
        epic: str,
        *,
        atr: float = 0.0,
        bid: float = 0.0,
        offer: float = 0.0,
    ) -> float:
        """Volatility-adjusted entry spread buffer — stop distance unchanged."""
        try:
            from platform_v2 import platform_v2_enabled

            if platform_v2_enabled():
                from platform_v2.adaptive_volatility_scalping import dynamic_slip_tolerance

                spread = abs(float(offer) - float(bid)) if bid and offer else 0.0
                gateway = dynamic_slip_tolerance(
                    epic=str(epic or ""),
                    atr_live=float(atr or 0),
                    bid=float(bid),
                    offer=float(offer),
                    spread=spread,
                )
                return float(gateway.slip_tolerance)
        except Exception as exc:
            log_guarded_exception("iron_clad_v2_slip", exc)
        try:
            from intelligence.matrix_backtuner import DEFAULT_EPIC_STOP

            stop_ref = float(DEFAULT_EPIC_STOP.get(epic, 10.0) or 10.0)
        except Exception:
            stop_ref = 10.0
        mid = (float(bid) + float(offer)) / 2.0 if bid and offer else 0.0
        atr_pts = max(float(atr or 0), stop_ref * 0.05)
        epic_scale = max(stop_ref * 0.15, atr_pts * 0.25)
        if mid > 1000:
            epic_scale = max(epic_scale, 2.0)
        tol = max(
            ENTRY_SPREAD_BUFFER_MIN,
            min(ENTRY_SPREAD_BUFFER_MAX, epic_scale),
        )
        try:
            from feeder.mock_feed_engine import mock_feed_active
            from trading.live_production_probe import live_probe_enabled

            if mock_feed_active() or live_probe_enabled():
                tol = max(tol, 12.0)
        except Exception:
            pass
        return tol

    @classmethod
    def validate_order(
        cls,
        *,
        epic: str,
        direction: str,
        size: float,
        stop_distance: float,
        limit_distance: float,
        bid: float,
        offer: float,
        rest_client: Any,
        atr: float = 0.0,
    ) -> tuple[bool, str, dict[str, float]]:
        """
        Returns (allowed, reason, normalized_params).
        normalized_params always contains enforced stop/limit/size when allowed.
        """
        spread = abs(float(offer) - float(bid))
        if not math.isfinite(spread) or spread < 0:
            return False, "IronClad: invalid bid/offer spread", {}

        slip_tol = cls.entry_spread_tolerance_points(
            epic, atr=atr, bid=bid, offer=offer
        )
        if spread > slip_tol:
            return (
                False,
                f"IronClad: spread {spread:.2f}pts exceeds slip tolerance "
                f"{slip_tol:.1f}pts (entry buffer; stop={MANDATORY_STOP_POINTS:.0f}pt)",
                {},
            )

        lot = float(size)
        max_lot = cls.effective_max_order_size()
        if lot <= 0 or lot > max_lot:
            return (
                False,
                f"IronClad: size {lot:.4f} outside (0, {max_lot:.1f}]",
                {},
            )

        stop = max(float(stop_distance), MANDATORY_STOP_POINTS)
        limit = max(float(limit_distance or 0), MANDATORY_LIMIT_POINTS)
        if limit < stop:
            limit = MANDATORY_LIMIT_POINTS

        breached, dd_detail = cls._evaluate_rolling_drawdown(rest_client)
        if breached:
            cls._hard_abort(rest_client, dd_detail)
            return False, dd_detail, {}

        mid = (float(bid) + float(offer)) / 2.0
        return True, "ok", {
            "size": min(lot, max_lot),
            "stop_distance": stop,
            "limit_distance": limit,
            "spread_points": spread,
            "expected_mid": mid,
        }

    @classmethod
    def _evaluate_rolling_drawdown(cls, rest_client: Any) -> tuple[bool, str]:
        equity = cls._resolve_equity(rest_client)
        if equity is None or equity <= 0:
            return False, ""

        now = time.monotonic()
        with cls._lock:
            if cls._session_equity is None or (now - cls._session_start_mono) > _ROLLING_WINDOW_SEC:
                cls._session_equity = equity
                cls._session_start_mono = now
                return False, ""
            start = float(cls._session_equity)

        if start <= 0:
            return False, ""

        drop = (start - equity) / start
        if drop > MAX_DAILY_DRAWDOWN_PCT:
            return True, (
                f"IronClad: rolling 24h drawdown {drop * 100:.2f}% > "
                f"{MAX_DAILY_DRAWDOWN_PCT * 100:.1f}% "
                f"(start={start:.2f} equity={equity:.2f})"
            )
        return False, ""

    @classmethod
    def _resolve_equity(cls, rest_client: Any) -> float | None:
        try:
            from execution.capital_guard import CapitalGuard

            return CapitalGuard._resolve_account_equity(rest_client)
        except Exception as exc:
            log_guarded_exception("iron_clad_equity", exc)
            return None

    @classmethod
    def _hard_abort(cls, rest_client: Any, reason: str) -> None:
        with cls._lock:
            if cls._tripped:
                sys.exit(_HARD_EXIT)
            cls._tripped = True
        log_engine(f"IronClad HARD ABORT: {reason}")
        try:
            from execution.capital_guard import CapitalGuard

            CapitalGuard._cancel_all_open_orders_and_positions(rest_client)
        except Exception as exc:
            log_guarded_exception("iron_clad_flatten", exc)
        sys.exit(_HARD_EXIT)

    @classmethod
    def reset_for_tests(cls) -> None:
        with cls._lock:
            cls._session_equity = None
            cls._session_start_mono = 0.0
            cls._tripped = False


def enforce_slippage_on_fill(
    *,
    direction: str,
    expected_mid: float,
    fill_price: float,
) -> tuple[bool, str]:
    """Post-fill slippage check — reject attribution if slip exceeds tolerance."""
    slip = abs(float(fill_price) - float(expected_mid))
    if slip > MAX_SLIPPAGE_POINTS:
        return (
            False,
            f"IronClad post-fill slip {slip:.2f}pts > {MAX_SLIPPAGE_POINTS:.1f}pts "
            f"dir={direction}",
        )
    return True, "ok"
