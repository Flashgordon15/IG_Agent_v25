"""Daily loss accounting — effective P&L after v29.1 baseline reset + soft pause."""

from __future__ import annotations

import copy
import threading
import time
from datetime import date
from typing import Any

RUNTIME_VERSION_KEY = "daily_loss_reset_version"
RUNTIME_BASELINE_KEY = "daily_loss_baseline_pnl"
RUNTIME_DAY_KEY = "daily_loss_reset_day"
RUNTIME_AT_KEY = "daily_loss_reset_at"

_DAILY_LOSS_GATE_CACHE_TTL_SEC = 2.0


def _today() -> str:
    return date.today().isoformat()


def invalidate_daily_loss_gate_cache() -> None:
    """Clear TTL snapshot (tests, baseline reset, manual refresh)."""
    from system.portfolio_envelope import _envelope

    env = _envelope()
    with env._allocation_lock:
        env._daily_loss_gate_anchor_mono = 0.0
        env._daily_loss_gate_cache_key = (-1, "")
        env._daily_loss_gate_result = None
        env._daily_loss_gate_refresh_inflight = False


def daily_loss_reset_snapshot(store: Any | None) -> dict[str, Any]:
    if store is None:
        return {}
    try:
        return {
            "version": store.get_runtime_state(RUNTIME_VERSION_KEY),
            "baseline_pnl": store.get_runtime_state(RUNTIME_BASELINE_KEY),
            "reset_day": store.get_runtime_state(RUNTIME_DAY_KEY),
            "reset_at": store.get_runtime_state(RUNTIME_AT_KEY),
        }
    except Exception:
        return {}


def _effective_daily_pnl_from_raw(
    store: Any | None, *, day: str, raw_pnl: float
) -> float:
    """Apply baseline adjustment without re-querying sum_daily_pnl."""
    if store is None:
        return raw_pnl
    reset_day = store.get_runtime_state(RUNTIME_DAY_KEY)
    if reset_day != day:
        return raw_pnl
    baseline_raw = store.get_runtime_state(RUNTIME_BASELINE_KEY)
    if baseline_raw is None:
        return raw_pnl
    try:
        baseline = float(baseline_raw)
    except (TypeError, ValueError):
        return raw_pnl
    return raw_pnl - baseline


def effective_daily_pnl(store: Any | None, *, day: str | None = None) -> float:
    """P&L for daily-loss gates — subtracts one-time baseline on reset day only."""
    if store is None:
        return 0.0
    d = day or _today()
    try:
        raw = float(store.sum_daily_pnl(d))
    except Exception:
        return 0.0
    return _effective_daily_pnl_from_raw(store, day=d, raw_pnl=raw)


def effective_daily_loss_gbp(store: Any | None, *, day: str | None = None) -> float:
    return max(0.0, -effective_daily_pnl(store, day=day))


def soft_pause_threshold_gbp(cfg: Any | None = None) -> float:
    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            return 400.0
    try:
        block = cfg.get("learning_demo_mode") or {}
        if isinstance(block, dict) and block.get("daily_loss_soft_pause_gbp") is not None:
            return float(block["daily_loss_soft_pause_gbp"])
    except (TypeError, ValueError, AttributeError):
        pass
    return 400.0


def hard_daily_loss_limit_gbp(cfg: Any | None = None) -> float:
    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            return 500.0
    try:
        return float(cfg.max_daily_loss_gbp)
    except (TypeError, ValueError, AttributeError):
        return 500.0


def _daily_loss_gate_status_uncached(
    store: Any | None,
    cfg: Any | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Returns (passed, detail, meta) for points_state / risk gates."""
    d = _today()
    raw_pnl = 0.0
    if store is not None:
        try:
            raw_pnl = float(store.sum_daily_pnl(d))
        except Exception:
            pass
    effective_pnl = _effective_daily_pnl_from_raw(store, day=d, raw_pnl=raw_pnl)
    loss = max(0.0, -effective_pnl)
    hard = hard_daily_loss_limit_gbp(cfg)
    soft = soft_pause_threshold_gbp(cfg)
    meta = {
        "effective_loss_gbp": round(loss, 2),
        "raw_daily_pnl": round(raw_pnl, 2),
        "soft_pause_gbp": soft,
        "hard_limit_gbp": hard,
        "reset": daily_loss_reset_snapshot(store),
    }
    if loss >= hard:
        return (
            False,
            f"daily loss £{loss:.2f} >= £{hard:.0f} (hard stop)",
            {**meta, "tier": "hard"},
        )
    if loss >= soft:
        return (
            False,
            f"soft pause — daily loss £{loss:.2f} >= £{soft:.0f} (entries blocked)",
            {**meta, "tier": "soft"},
        )
    return True, f"daily loss £{loss:.2f} < £{soft:.0f}", {**meta, "tier": "ok"}


def _schedule_daily_loss_gate_refresh(
    store: Any | None,
    cfg: Any | None,
    cache_key: tuple[int, str],
) -> None:
    def _worker() -> None:
        from system.portfolio_envelope import _envelope

        env = _envelope()
        try:
            result = _daily_loss_gate_status_uncached(store, cfg)
            env.write_daily_loss_gate_snapshot(cache_key, result)
        finally:
            env.end_daily_loss_gate_refresh()

    threading.Thread(
        target=_worker,
        name="daily-loss-gate-refresh",
        daemon=True,
    ).start()


def daily_loss_gate_status(
    store: Any | None,
    cfg: Any | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """
    Returns (passed, detail, meta) for points_state / risk gates.

    Process-wide 2s TTL snapshot anchored under ``PortfolioEnvelope._allocation_lock``.
    On expiry, returns the last-known snapshot immediately and refreshes SQLite in a
    background thread so concurrent epic loops never stampede the DB.
    """
    from system.portfolio_envelope import _envelope

    cache_key = (id(store) if store is not None else 0, _today())
    env = _envelope()

    fresh = env.read_daily_loss_gate_snapshot(
        cache_key, ttl_sec=_DAILY_LOSS_GATE_CACHE_TTL_SEC
    )
    if fresh is not None:
        try:
            from system.diagnostics.perf_metrics import record_daily_loss_cache

            record_daily_loss_cache(hit=True)
        except Exception:
            pass
        return fresh

    stale, schedule_refresh = env.consume_expired_daily_loss_gate(cache_key)
    if stale is not None:
        if schedule_refresh:
            _schedule_daily_loss_gate_refresh(store, cfg, cache_key)
        try:
            from system.diagnostics.perf_metrics import record_daily_loss_cache

            record_daily_loss_cache(hit=True)
        except Exception:
            pass
        return stale

    try:
        from system.diagnostics.perf_metrics import record_daily_loss_cache

        record_daily_loss_cache(hit=False)
    except Exception:
        pass

    result = _daily_loss_gate_status_uncached(store, cfg)
    env.write_daily_loss_gate_snapshot(cache_key, result)
    ok, detail, meta = result
    return ok, detail, copy.deepcopy(meta)
