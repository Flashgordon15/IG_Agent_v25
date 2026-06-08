"""v26 portfolio envelope — concurrent/daily risk caps for live gate."""

from __future__ import annotations

import json
import threading
from functools import lru_cache
from typing import Any

from system.paths import project_root

_lock = threading.RLock()
_concurrent_risk_gbp: float = 0.0
_daily_deployed_gbp: float = 0.0
_daily_pnl_gbp: float = 0.0


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


def reset_portfolio_envelope_for_tests() -> None:
    global _concurrent_risk_gbp, _daily_deployed_gbp, _daily_pnl_gbp
    with _lock:
        _concurrent_risk_gbp = 0.0
        _daily_deployed_gbp = 0.0
        _daily_pnl_gbp = 0.0
    _envelope_config.cache_clear()
    _gate_config.cache_clear()


def rehydrate(
    *,
    concurrent_risk_gbp: float = 0.0,
    daily_deployed_gbp: float = 0.0,
    daily_pnl_gbp: float = 0.0,
) -> None:
    """Restore in-memory envelope from open trades / today's ledger (agent restart)."""
    global _concurrent_risk_gbp, _daily_deployed_gbp, _daily_pnl_gbp
    with _lock:
        _concurrent_risk_gbp = max(0.0, float(concurrent_risk_gbp))
        _daily_deployed_gbp = max(0.0, float(daily_deployed_gbp))
        _daily_pnl_gbp = float(daily_pnl_gbp)


def record_entry(risk_gbp: float) -> None:
    global _concurrent_risk_gbp, _daily_deployed_gbp
    with _lock:
        _concurrent_risk_gbp += float(risk_gbp)
        _daily_deployed_gbp += float(risk_gbp)


def record_exit(risk_gbp: float, *, pnl_gbp: float = 0.0) -> None:
    global _concurrent_risk_gbp, _daily_pnl_gbp
    with _lock:
        _concurrent_risk_gbp = max(0.0, _concurrent_risk_gbp - float(risk_gbp))
        _daily_pnl_gbp += float(pnl_gbp)


def can_allocate(risk_gbp: float) -> tuple[bool, str]:
    env = _envelope_config()
    max_concurrent = float(env.get("max_concurrent_risk_gbp") or 1200)
    max_daily = float(env.get("max_daily_risk_deployed_gbp") or 2500)
    max_loss = float(env.get("max_daily_loss_gbp") or 500)
    min_avail = float(env.get("min_available_gbp") or 100)
    balance = float(env.get("account_balance_gbp") or 10000)
    reserve_pct = float(env.get("reserve_pct") or 0.10)
    risk = float(risk_gbp)

    with _lock:
        concurrent = _concurrent_risk_gbp
        daily_dep = _daily_deployed_gbp
        daily_pnl = _daily_pnl_gbp

    if daily_pnl <= -max_loss:
        return False, f"daily loss limit £{max_loss:.0f} reached"
    if concurrent + risk > max_concurrent:
        return (
            False,
            f"concurrent £{concurrent:.0f}+£{risk:.0f} > £{max_concurrent:.0f}",
        )
    if daily_dep + risk > max_daily:
        return False, f"daily deploy £{daily_dep:.0f}+£{risk:.0f} > £{max_daily:.0f}"
    available = balance * (1.0 - reserve_pct) - concurrent
    if available - risk < min_avail:
        return False, f"available £{available:.0f} below min £{min_avail:.0f}"
    return True, "ok"


def snapshot() -> dict[str, Any]:
    env = _envelope_config()
    max_concurrent = float(env.get("max_concurrent_risk_gbp") or 1200)
    with _lock:
        concurrent = _concurrent_risk_gbp
        daily_dep = _daily_deployed_gbp
        daily_pnl = _daily_pnl_gbp
    return {
        "concurrent_risk_gbp": round(concurrent, 2),
        "daily_deployed_gbp": round(daily_dep, 2),
        "daily_pnl_gbp": round(daily_pnl, 2),
        "max_concurrent_risk_gbp": max_concurrent,
        "gate_enabled": portfolio_gate_enabled(),
    }
