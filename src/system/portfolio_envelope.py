"""v26 portfolio envelope — concurrent/daily risk caps for live gate."""

from __future__ import annotations

import copy
import json
import threading
import time
from datetime import date
from functools import lru_cache
from typing import Any

from system.paths import project_root

_DAILY_LOSS_GATE_CACHE_TTL_SEC = 2.0


@lru_cache(maxsize=1)
def _envelope_config() -> dict[str, Any]:
    path = project_root() / "config" / "config_v26.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw.get("capital_envelope") or {}
    except (json.JSONDecodeError, OSError):
        return {}


@lru_cache(maxsize=1)
def _gate_config() -> dict[str, Any]:
    path = project_root() / "config" / "config_v26.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw.get("portfolio_gate") or {}
    except (json.JSONDecodeError, OSError):
        return {}


def portfolio_gate_enabled() -> bool:
    return bool(_gate_config().get("enabled", False))


class PortfolioEnvelope:
    """Process-wide portfolio risk budget with atomic check-and-reserve."""

    def __init__(self) -> None:
        self._allocation_lock = threading.RLock()
        self._concurrent_risk_gbp: float = 0.0
        self._daily_deployed_gbp: float = 0.0
        self._daily_pnl_gbp: float = 0.0
        self._envelope_utc_day: str = ""
        # Process-wide daily-loss gate snapshot (shared across epic loops).
        self._daily_loss_gate_anchor_mono: float = 0.0
        self._daily_loss_gate_cache_key: tuple[int, str] = (-1, "")
        self._daily_loss_gate_result: (
            tuple[bool, str, dict[str, Any]] | None
        ) = None
        self._daily_loss_gate_refresh_inflight: bool = False

    def reset_for_tests(self) -> None:
        with self._allocation_lock:
            self._concurrent_risk_gbp = 0.0
            self._daily_deployed_gbp = 0.0
            self._daily_pnl_gbp = 0.0
            self._envelope_utc_day = ""
            self._daily_loss_gate_anchor_mono = 0.0
            self._daily_loss_gate_cache_key = (-1, "")
            self._daily_loss_gate_result = None
            self._daily_loss_gate_refresh_inflight = False
        _envelope_config.cache_clear()
        _gate_config.cache_clear()

    def read_daily_loss_gate_snapshot(
        self,
        cache_key: tuple[int, str],
        *,
        ttl_sec: float = _DAILY_LOSS_GATE_CACHE_TTL_SEC,
    ) -> tuple[bool, str, dict[str, Any]] | None:
        with self._allocation_lock:
            if self._daily_loss_gate_result is None:
                return None
            if self._daily_loss_gate_cache_key != cache_key:
                return None
            if (time.monotonic() - self._daily_loss_gate_anchor_mono) >= ttl_sec:
                return None
            ok, detail, meta = self._daily_loss_gate_result
            return ok, detail, copy.deepcopy(meta)

    def read_daily_loss_gate_stale(
        self, cache_key: tuple[int, str]
    ) -> tuple[bool, str, dict[str, Any]] | None:
        with self._allocation_lock:
            if (
                self._daily_loss_gate_result is None
                or self._daily_loss_gate_cache_key != cache_key
            ):
                return None
            ok, detail, meta = self._daily_loss_gate_result
            return ok, detail, copy.deepcopy(meta)

    def write_daily_loss_gate_snapshot(
        self,
        cache_key: tuple[int, str],
        result: tuple[bool, str, dict[str, Any]],
        *,
        anchor_mono: float | None = None,
    ) -> None:
        with self._allocation_lock:
            self._daily_loss_gate_cache_key = cache_key
            self._daily_loss_gate_result = result
            self._daily_loss_gate_anchor_mono = (
                float(anchor_mono)
                if anchor_mono is not None
                else time.monotonic()
            )
            self._daily_loss_gate_refresh_inflight = False

    def consume_expired_daily_loss_gate(
        self,
        cache_key: tuple[int, str],
    ) -> tuple[tuple[bool, str, dict[str, Any]] | None, bool]:
        """
        Single-flight stale serve when the TTL window has expired.

        Returns ``(stale_snapshot, schedule_refresh)``. ``schedule_refresh`` is
        True for exactly one caller that set ``_daily_loss_gate_refresh_inflight``
        while still holding ``_allocation_lock``.
        """
        with self._allocation_lock:
            if (
                self._daily_loss_gate_result is None
                or self._daily_loss_gate_cache_key != cache_key
            ):
                return None, False
            ok, detail, meta = self._daily_loss_gate_result
            stale = (ok, detail, copy.deepcopy(meta))
            if self._daily_loss_gate_refresh_inflight:
                return stale, False
            self._daily_loss_gate_refresh_inflight = True
            return stale, True

    def end_daily_loss_gate_refresh(self) -> None:
        with self._allocation_lock:
            self._daily_loss_gate_refresh_inflight = False

    def _maybe_roll_utc_day_unlocked(self) -> None:
        today = date.today().isoformat()
        rolled = bool(self._envelope_utc_day and self._envelope_utc_day != today)
        if rolled:
            self._daily_deployed_gbp = 0.0
            self._daily_pnl_gbp = 0.0
        self._envelope_utc_day = today
        if rolled:
            try:
                from data.learning_store import LearningStore
                from system.config_loader import get_config
                from system.daily_loss_policy import effective_daily_pnl

                store = LearningStore(str(get_config().learning_db))
                self._daily_pnl_gbp = float(effective_daily_pnl(store, day=today))
            except Exception:
                pass

    def rehydrate(
        self,
        *,
        concurrent_risk_gbp: float = 0.0,
        daily_deployed_gbp: float = 0.0,
        daily_pnl_gbp: float = 0.0,
    ) -> None:
        with self._allocation_lock:
            self._concurrent_risk_gbp = max(0.0, float(concurrent_risk_gbp))
            self._daily_deployed_gbp = max(0.0, float(daily_deployed_gbp))
            self._daily_pnl_gbp = float(daily_pnl_gbp)

    def record_entry(self, risk_gbp: float) -> None:
        """Increment deployed risk (legacy direct path / tests)."""
        risk = float(risk_gbp)
        with self._allocation_lock:
            self._concurrent_risk_gbp += risk
            self._daily_deployed_gbp += risk

    def record_exit(self, risk_gbp: float, *, pnl_gbp: float = 0.0) -> None:
        with self._allocation_lock:
            self._concurrent_risk_gbp = max(
                0.0, self._concurrent_risk_gbp - float(risk_gbp)
            )
            self._daily_pnl_gbp += float(pnl_gbp)

    def release_allocation(self, risk_gbp: float) -> None:
        """Undo a gate-time reservation when execution does not proceed."""
        risk = max(0.0, float(risk_gbp))
        if risk <= 0:
            return
        with self._allocation_lock:
            self._concurrent_risk_gbp = max(0.0, self._concurrent_risk_gbp - risk)
            self._daily_deployed_gbp = max(0.0, self._daily_deployed_gbp - risk)

    def can_allocate(
        self, risk_gbp: float, *, reserve: bool = True
    ) -> tuple[bool, str]:
        """
        Atomically evaluate envelope caps and optionally claim the risk budget.

        When reserve=True (default), a passing check immediately increments
        concurrent and daily-deploy counters under the allocation lock.
        """
        env = _envelope_config()
        max_concurrent = float(env.get("max_concurrent_risk_gbp") or 1200)
        max_daily = float(env.get("max_daily_risk_deployed_gbp") or 2500)
        min_avail = float(env.get("min_available_gbp") or 100)
        balance = float(env.get("account_balance_gbp") or 10000)
        reserve_pct = float(env.get("reserve_pct") or 0.10)
        risk = max(0.0, float(risk_gbp))

        from system.daily_loss_policy import (
            hard_daily_loss_limit_gbp,
            soft_pause_threshold_gbp,
        )

        hard = hard_daily_loss_limit_gbp()
        soft = soft_pause_threshold_gbp()

        with self._allocation_lock:
            self._maybe_roll_utc_day_unlocked()
            concurrent = self._concurrent_risk_gbp
            daily_dep = self._daily_deployed_gbp
            daily_pnl = self._daily_pnl_gbp

            loss_gbp = max(0.0, -daily_pnl)
            if loss_gbp >= hard:
                return False, f"daily loss £{loss_gbp:.2f} >= £{hard:.0f} (hard stop)"
            if loss_gbp >= soft:
                return (
                    False,
                    f"soft pause — daily loss £{loss_gbp:.2f} >= £{soft:.0f} "
                    "(entries blocked)",
                )
            if concurrent + risk > max_concurrent:
                return (
                    False,
                    f"concurrent £{concurrent:.0f}+£{risk:.0f} > £{max_concurrent:.0f}",
                )
            if daily_dep + risk > max_daily:
                return (
                    False,
                    f"daily deploy £{daily_dep:.0f}+£{risk:.0f} > £{max_daily:.0f}",
                )
            available = balance * (1.0 - reserve_pct) - concurrent
            if available - risk < min_avail:
                return False, f"available £{available:.0f} below min £{min_avail:.0f}"

            if reserve and risk > 0:
                self._concurrent_risk_gbp += risk
                self._daily_deployed_gbp += risk
            return True, "ok"

    def snapshot(self) -> dict[str, Any]:
        env = _envelope_config()
        max_concurrent = float(env.get("max_concurrent_risk_gbp") or 1200)
        with self._allocation_lock:
            concurrent = self._concurrent_risk_gbp
            daily_dep = self._daily_deployed_gbp
            daily_pnl = self._daily_pnl_gbp
        return {
            "concurrent_risk_gbp": round(concurrent, 2),
            "daily_deployed_gbp": round(daily_dep, 2),
            "daily_pnl_gbp": round(daily_pnl, 2),
            "max_concurrent_risk_gbp": max_concurrent,
            "gate_enabled": portfolio_gate_enabled(),
        }


_ENVELOPE = PortfolioEnvelope()


def _envelope() -> PortfolioEnvelope:
    return _ENVELOPE


def reset_portfolio_envelope_for_tests() -> None:
    _envelope().reset_for_tests()


def rehydrate(
    *,
    concurrent_risk_gbp: float = 0.0,
    daily_deployed_gbp: float = 0.0,
    daily_pnl_gbp: float = 0.0,
) -> None:
    _envelope().rehydrate(
        concurrent_risk_gbp=concurrent_risk_gbp,
        daily_deployed_gbp=daily_deployed_gbp,
        daily_pnl_gbp=daily_pnl_gbp,
    )


def record_entry(risk_gbp: float) -> None:
    _envelope().record_entry(risk_gbp)


def record_exit(risk_gbp: float, *, pnl_gbp: float = 0.0) -> None:
    _envelope().record_exit(risk_gbp, pnl_gbp=pnl_gbp)


def release_allocation(risk_gbp: float) -> None:
    _envelope().release_allocation(risk_gbp)


def can_allocate(risk_gbp: float, *, reserve: bool = True) -> tuple[bool, str]:
    return _envelope().can_allocate(risk_gbp, reserve=reserve)


def snapshot() -> dict[str, Any]:
    return _envelope().snapshot()
