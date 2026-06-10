"""
v29.0 Hourly Telegram Executive Status — consolidated status (not trade fills).

Real-time entry/exit notifications remain in telegram_notifier.notify_trade_*.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from system.engine_log import log_engine

_LONDON = ZoneInfo("Europe/London")
_HOURLY_INTERVAL_SEC = 3600.0
_hourly_stop = threading.Event()
_hourly_thread: threading.Thread | None = None
_hourly_lock = threading.RLock()


def is_agent_active() -> bool:
    """True when trading loops are running and the health cache reports alive."""
    try:
        from api.agent_control import is_trading_running
        from api.agent_health import get_cached_health_status

        health = get_cached_health_status()
        return bool(health.get("agent_alive")) and is_trading_running()
    except Exception:
        return False


def build_executive_report_state(state: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Assemble executive metrics from in-memory caches (health, rotation, points).

    Trade ledger rollups use the already-open LearningStore connection once per hour.
    """
    merged: dict[str, Any] = dict(state or {})
    try:
        from api.agent_health import get_cached_health_status

        health = get_cached_health_status()
        merged.update(
            {
                "trading_healthy": bool(health.get("trading_healthy")),
                "quotes_fresh": bool(health.get("quotes_fresh")),
                "quotes_fresh_count": int(health.get("quotes_fresh_count") or 0),
                "quotes_total": int(health.get("quotes_total") or 0),
                "trading_loops_running": bool(health.get("trading_loops_running")),
                "issues": list(health.get("issues") or []),
            }
        )
    except Exception as e:
        merged.setdefault("issues", []).append(f"health:{type(e).__name__}")

    try:
        from runtime.market_orchestrator import MarketOrchestrator

        active = MarketOrchestrator.get_global_active_epics()
        merged["active_epics"] = list(active)
        if active:
            from trading.open_position_view import epic_market_label

            merged["top_rotation_epic"] = active[0]
            merged["top_rotation_label"] = epic_market_label(active[0])
        else:
            merged["top_rotation_epic"] = ""
            merged["top_rotation_label"] = "—"
    except Exception:
        merged.setdefault("active_epics", [])
        merged.setdefault("top_rotation_label", "—")

    try:
        from api.snapshot_store import get_tick

        tick = get_tick() or {}
        pts = tick.get("points") if isinstance(tick.get("points"), dict) else {}
        merged["points_cumulative"] = float(
            pts.get("cumulative") or pts.get("cumulative_points") or 0.0
        )
        merged["points_state"] = str(pts.get("state") or "CAUTION")
    except Exception:
        merged.setdefault("points_cumulative", 0.0)
        merged.setdefault("points_state", "CAUTION")

    try:
        from system.gate_activity import count_liquidity_shield_blocks_last_hour

        merged["liquidity_shield_blocks_1hr"] = (
            count_liquidity_shield_blocks_last_hour()
        )
    except Exception:
        merged["liquidity_shield_blocks_1hr"] = 0

    now = datetime.now()
    hour_ago = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    day_ago = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        from data.learning_store import LearningStore
        from system.config_loader import get_config

        cfg = get_config()
        store = LearningStore(str(cfg.learning_db))
        store.connect()
        stats = store.closed_trade_stats_since(hour_ago)
        merged["trades_1hr"] = int(stats.get("trades") or 0)
        merged["wins_1hr"] = int(stats.get("wins") or 0)
        merged["losses_1hr"] = int(stats.get("losses") or 0)
        merged["pnl_24hr_gbp"] = float(store.sum_realised_pnl_since(day_ago))
    except Exception as e:
        log_engine(f"executive report ledger read skipped: {type(e).__name__}: {e}")
        merged.setdefault("trades_1hr", 0)
        merged.setdefault("wins_1hr", 0)
        merged.setdefault("losses_1hr", 0)
        merged.setdefault("pnl_24hr_gbp", 0.0)

    return merged


def _able_to_trade_yes_no(state: dict[str, Any]) -> str:
    fresh = int(state.get("quotes_fresh_count") or 0)
    total = int(state.get("quotes_total") or 0)
    if (
        bool(state.get("trading_healthy"))
        and bool(state.get("quotes_fresh"))
        and total > 0
        and fresh == total
    ):
        return "YES"
    return "NO"


def format_hourly_executive_report(state: dict[str, Any]) -> str:
    """Structured hourly payload for Telegram."""
    ts = datetime.now(_LONDON).strftime("%H:%M BST")
    able = _able_to_trade_yes_no(state)
    fresh = int(state.get("quotes_fresh_count") or 0)
    total = int(state.get("quotes_total") or 0)
    trades = int(state.get("trades_1hr") or 0)
    wins = int(state.get("wins_1hr") or 0)
    losses = int(state.get("losses_1hr") or 0)
    pnl = float(state.get("pnl_24hr_gbp") or 0.0)
    pnl_sign = "+" if pnl >= 0 else ""
    cumulative = float(state.get("points_cumulative") or 0.0)
    points_state = str(state.get("points_state") or "CAUTION")
    top = str(state.get("top_rotation_label") or "—")
    shield = int(state.get("liquidity_shield_blocks_1hr") or 0)
    loops = "active" if state.get("trading_loops_running") else "stopped"

    return (
        f"📊 IG Agent v29 — Hourly Executive ({ts})\n"
        f"Able to Trade: {able}\n"
        f"Quotes: {fresh}/{total} fresh | Loops: {loops}\n"
        f"Trades (1hr): {trades} ({wins}W / {losses}L)\n"
        f"P&L (24hr): {pnl_sign}£{pnl:,.2f}\n"
        f"Points: {cumulative:.1f} cumulative ({points_state})\n"
        f"Top rotation: {top}\n"
        f"Liquidity shield blocks (1hr): {shield}"
    )


def send_hourly_executive_report(state: dict[str, Any] | None = None) -> bool:
    """
    Send the v29 hourly executive status update.

    Does not replace notify_trade_opened / notify_trade_closed — those stay real-time.
    """
    payload = build_executive_report_state(state)
    text = format_hourly_executive_report(payload)
    try:
        from system.telegram_notifier import get_telegram_notifier

        notifier = get_telegram_notifier()
        if notifier is None or not notifier.enabled:
            log_engine("hourly executive report skipped — telegram disabled")
            return False
        ok = notifier.send_now(text)
        if ok:
            log_engine("hourly executive report sent")
        else:
            log_engine("hourly executive report send failed")
        return ok
    except Exception as e:
        log_engine(f"hourly executive report error: {type(e).__name__}: {e}")
        return False


def start_hourly_executive_telegram_scheduler(
    interval_sec: float = _HOURLY_INTERVAL_SEC,
) -> None:
    """
    Background hourly status thread (daemon).

    Uses threading to match the agent's sync uvicorn runtime; does not touch trade alerts.
    """
    global _hourly_thread
    with _hourly_lock:
        if _hourly_thread is not None and _hourly_thread.is_alive():
            return
        _hourly_stop.clear()

        def _loop() -> None:
            # First report after one full interval (avoid duplicate with startup alert).
            while not _hourly_stop.wait(interval_sec):
                try:
                    if is_agent_active():
                        send_hourly_executive_report()
                except Exception as e:
                    log_engine(
                        f"hourly executive scheduler tick failed: {type(e).__name__}: {e}"
                    )

        _hourly_thread = threading.Thread(
            target=_loop,
            name="telegram-hourly-executive",
            daemon=True,
        )
        _hourly_thread.start()
        log_engine(
            f"hourly executive telegram scheduler started (interval {interval_sec / 60:.0f}m)"
        )


def stop_hourly_executive_telegram_scheduler() -> None:
    _hourly_stop.set()
