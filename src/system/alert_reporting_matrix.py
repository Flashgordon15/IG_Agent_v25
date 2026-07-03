"""
Alert & reporting matrix — async webhook dispatch, tiered telemetry, EOD summaries.

Non-blocking queue ensures outbound HTTP never blocks the 500ms orchestrator hot path.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import requests

from system.engine_log import log_engine

_EOD_INTERVAL_SEC = 86400.0
_QUEUE_MAX = 512
_LOG_MAX = 64
_HTTP_TIMEOUT_SEC = 8.0
_COALESCE_WINDOW_SEC = 3600.0
_COALESCE_POLL_SEC = 0.25

WIN_RATE_TARGET = 0.70
DAILY_PNL_TARGET_GBP = 1000.0
PP_EXPANSION = 1200
PP_DEFENSE = 800
ACCOUNT_EQUITY_GBP = 10_000.0


class EventCategory(str, Enum):
    SYSTEM_SECURITY = "system_security"
    GAMIFICATION = "gamification"
    AUTO_TUNING = "auto_tuning"
    EOD_REPORT = "eod_report"


class EventPriority(str, Enum):
    CRITICAL = "critical"
    INFO = "info"
    DEBUG = "debug"


@dataclass
class AlertEvent:
    category: EventCategory
    priority: EventPriority
    title: str
    body: str
    ts: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def format_message(self) -> str:
        return format_alert_markdown(self)


def format_alert_markdown(event: AlertEvent) -> str:
    """High-fidelity Telegram Markdown — critical vs milestone card layouts."""
    meta = dict(event.metadata or {})
    if event.priority == EventPriority.CRITICAL or event.category == EventCategory.SYSTEM_SECURITY:
        return _format_critical_markdown(event, meta)
    if event.category == EventCategory.GAMIFICATION and event.priority == EventPriority.INFO:
        return _format_milestone_markdown(event, meta)
    if event.priority == EventPriority.DEBUG:
        return _format_debug_markdown(event, meta)
    return _format_info_markdown(event, meta)


def _escape_md(text: str) -> str:
    return str(text or "").replace("`", "'")


def _format_critical_markdown(event: AlertEvent, meta: dict[str, Any]) -> str:
    ticker = _escape_md(meta.get("ticker") or meta.get("epic") or "PLATFORM")
    slippage = meta.get("slippage_pts", meta.get("slippage"))
    slip_line = ""
    if slippage is not None:
        try:
            slip_line = f"Slippage: `{float(slippage):.3f} pts`\n"
        except (TypeError, ValueError):
            slip_line = f"Slippage: `{_escape_md(slippage)}`\n"
    status = _escape_md(meta.get("status_line") or "⚡ FLASHING — ACTIVE")
    body = _escape_md(event.body)
    return (
        f"🚨 *{_escape_md(event.title)}*\n"
        f"```\n"
        f"Ticker: {ticker}\n"
        f"{slip_line}"
        f"Status: {status}\n"
        f"Category: {event.category.value}\n"
        f"```\n"
        f"{body}"
    )


def _format_milestone_markdown(event: AlertEvent, meta: dict[str, Any]) -> str:
    pp = int(meta.get("platform_pp") or 1000)
    rank = _escape_md(meta.get("operational_rank") or meta.get("rank") or "standard")
    cap_exp = float(meta.get("capacity_expansion_pct") or 0.0)
    body = _escape_md(event.body)
    return (
        f"🟢 *{_escape_md(event.title)}*\n"
        f"```\n"
        f"Platform PP: {pp}\n"
        f"Operational Rank: {rank}\n"
        f"Capacity Expansion: +{cap_exp:.1f}%\n"
        f"```\n"
        f"{body}"
    )


def _format_info_markdown(event: AlertEvent, meta: dict[str, Any]) -> str:
    ticker = meta.get("ticker") or meta.get("epic")
    header = f"ℹ️ *{_escape_md(event.title)}*"
    if ticker:
        header = f"ℹ️ *{_escape_md(event.title)}* — `{_escape_md(ticker)}`"
    return f"{header}\n```\n{_escape_md(event.body)}\n```"


def _format_debug_markdown(event: AlertEvent, meta: dict[str, Any]) -> str:
    return (
        f"🔧 *{_escape_md(event.title)}*\n"
        f"```\n"
        f"Category: {event.category.value}\n"
        f"{_escape_md(event.body)}\n"
        f"```"
    )


def _format_coalesced_batch_markdown(events: list[AlertEvent]) -> str:
    """Compress burst scalper / tuning events into one structured status card."""
    if not events:
        return ""
    if len(events) == 1:
        return events[0].format_message()

    categories: dict[str, int] = {}
    tickers: list[str] = []
    slip_vals: list[float] = []
    lines: list[str] = []

    for ev in events:
        cat = ev.category.value
        categories[cat] = categories.get(cat, 0) + 1
        meta = ev.metadata or {}
        ticker = meta.get("ticker") or meta.get("epic")
        if ticker and ticker not in tickers:
            tickers.append(str(ticker))
        slip = meta.get("slippage_pts", meta.get("slippage"))
        if slip is not None:
            try:
                slip_vals.append(float(slip))
            except (TypeError, ValueError):
                pass
        summary = ev.body.split("\n", 1)[0][:72] if ev.body else ""
        lines.append(f"• [{cat}] {_escape_md(ev.title)}: {_escape_md(summary)}")

    avg_slip = sum(slip_vals) / len(slip_vals) if slip_vals else None
    slip_summary = f"`{avg_slip:.3f} pts avg`" if avg_slip is not None else "`n/a`"
    ticker_summary = ", ".join(tickers[:6]) if tickers else "—"
    if len(tickers) > 6:
        ticker_summary += f" +{len(tickers) - 6} more"

    cat_summary = " | ".join(f"{k}:{v}" for k, v in sorted(categories.items()))
    detail_block = "\n".join(lines[:50])
    if len(lines) > 50:
        detail_block += f"\n… +{len(lines) - 50} more events"

    window_label = (
        "hourly"
        if _COALESCE_WINDOW_SEC >= 3600.0
        else f"{_COALESCE_WINDOW_SEC:.0f}s"
    )
    return (
        f"📦 *Batch Status — {len(events)} events / {window_label} window*\n"
        f"```\n"
        f"Tickers: {ticker_summary}\n"
        f"Avg Slippage: {slip_summary}\n"
        f"Mix: {cat_summary}\n"
        f"```\n"
        f"```\n"
        f"{detail_block}\n"
        f"```"
    )


_lock = threading.RLock()
_event_queue: queue.Queue[AlertEvent | None] = queue.Queue(maxsize=_QUEUE_MAX)
_broadcast_log: deque[dict[str, Any]] = deque(maxlen=_LOG_MAX)
_worker_thread: threading.Thread | None = None
_eod_thread: threading.Thread | None = None
_stop = threading.Event()
_coalesce_lock = threading.Lock()
_coalesce_buffer: list[AlertEvent] = []
_coalesce_deadline: float = 0.0
_coalesce_batches_sent: int = 0
_last_pp_tier: str = "standard"
_last_eod_ts: float = 0.0
_webhook_status: dict[str, Any] = {
    "telegram": {"configured": False, "state": "IDLE", "last_ok": False, "last_error": ""},
    "discord": {"configured": False, "state": "IDLE", "last_ok": False, "last_error": ""},
}
_snapshot: dict[str, Any] = {
    "ok": True,
    "healthy": True,
    "queue_depth": 0,
    "webhooks": dict(_webhook_status),
    "last_broadcasts": [],
    "last_eod_ts": 0.0,
    "ts": 0.0,
}


def _reporting_config() -> dict[str, Any]:
    try:
        from system.config_loader import get_config

        cfg = get_config()
        block = getattr(cfg, "alert_reporting", None)
        if isinstance(block, dict):
            return block
        if hasattr(cfg, "get"):
            raw = cfg.get("alert_reporting")
            if isinstance(raw, dict):
                return raw
        tg = getattr(cfg, "telegram", None)
        if isinstance(tg, dict):
            return {"telegram": tg}
        if hasattr(cfg, "get"):
            tg2 = cfg.get("telegram")
            if isinstance(tg2, dict):
                return {"telegram": tg2}
    except Exception:
        pass
    try:
        from system.v26_config import load_v26_overlay

        mw = load_v26_overlay().get("milestone_webhooks") or {}
        if isinstance(mw, dict):
            return {"discord_webhook_url": mw.get("discord_webhook_url", "")}
    except Exception:
        pass
    return {}


def _telegram_configured() -> bool:
    try:
        from system.telegram_notifier import get_telegram_notifier

        notifier = get_telegram_notifier()
        return notifier is not None and bool(getattr(notifier, "enabled", False))
    except Exception:
        pass
    cfg = _reporting_config().get("telegram") or {}
    if isinstance(cfg, dict):
        return bool(cfg.get("enabled")) and bool(str(cfg.get("bot_token") or "").strip())
    return False


def _discord_configured() -> bool:
    return bool(_discord_webhook_url())


def _sync_webhook_idle_states() -> None:
    """Mark unconfigured channels IDLE — never a health failure."""
    tg_cfg = _telegram_configured()
    dc_cfg = _discord_configured()
    with _lock:
        if not tg_cfg:
            _webhook_status["telegram"] = {
                "configured": False,
                "state": "IDLE",
                "last_ok": True,
                "last_error": "",
            }
        if not dc_cfg:
            _webhook_status["discord"] = {
                "configured": False,
                "state": "IDLE",
                "last_ok": True,
                "last_error": "",
            }


def _discord_webhook_url() -> str:
    cfg = _reporting_config()
    url = str(cfg.get("discord_webhook_url") or "").strip()
    if url:
        return url
    try:
        from system.v26_config import load_v26_overlay

        mw = load_v26_overlay().get("milestone_webhooks") or {}
        return str(mw.get("discord_webhook_url") or "").strip()
    except Exception:
        return ""


def _telegram_send(text: str, *, parse_mode: str = "Markdown") -> bool:
    if not _telegram_configured():
        with _lock:
            _webhook_status["telegram"]["state"] = "SKIPPED"
            _webhook_status["telegram"]["last_ok"] = True
        return True
    try:
        from system.telegram_notifier import get_telegram_notifier, send_critical_alert

        notifier = get_telegram_notifier()
        if notifier is not None and notifier.enabled:
            payload = text[:3500]
            if payload.startswith("🚨"):
                return bool(send_critical_alert(payload, parse_mode=parse_mode))
            notifier.send_markdown(payload, parse_mode=parse_mode)
            return True
        with _lock:
            _webhook_status["telegram"]["state"] = "SKIPPED"
            _webhook_status["telegram"]["last_ok"] = True
        return True
    except Exception as exc:
        _webhook_status["telegram"]["last_error"] = f"{type(exc).__name__}: {exc}"
        return False


def _discord_send(text: str) -> bool:
    url = _discord_webhook_url()
    if not url:
        with _lock:
            _webhook_status["discord"]["state"] = "SKIPPED"
            _webhook_status["discord"]["last_ok"] = True
        return True
    try:
        resp = requests.post(
            url,
            json={"content": text[:1900]},
            timeout=_HTTP_TIMEOUT_SEC,
        )
        ok = resp.status_code in (200, 204)
        if not ok:
            _webhook_status["discord"]["last_error"] = f"HTTP {resp.status_code}"
        return ok
    except Exception as exc:
        _webhook_status["discord"]["last_error"] = f"{type(exc).__name__}: {exc}"
        return False


def _resolve_coalesce_window_sec() -> float:
    """Align burst coalescing with telegram hourly digest cadence."""
    try:
        from system.config_loader import get_config

        cfg = get_config()
        tg = getattr(cfg, "telegram", None)
        if isinstance(tg, dict):
            raw = tg.get("telegram_heartbeat_interval_seconds")
            if raw is not None:
                sec = float(raw)
                if sec > 0:
                    return sec
    except Exception:
        pass
    return _COALESCE_WINDOW_SEC


def _buffer_coalesced_telegram_digest(msg: str) -> bool:
    try:
        from system.telegram_notifier import get_telegram_notifier

        notifier = get_telegram_notifier()
        if notifier is not None and notifier.enabled:
            notifier.buffer_hourly_digest(msg)
            return True
    except Exception:
        pass
    return False


def _dispatch_webhooks_text(msg: str) -> None:
    tg_cfg = _telegram_configured()
    dc_cfg = _discord_configured()
    tg_ok = _telegram_send(msg) if tg_cfg or not tg_cfg else True
    dc_ok = _discord_send(msg) if dc_cfg or not dc_cfg else True
    with _lock:
        if tg_cfg:
            _webhook_status["telegram"]["configured"] = True
            _webhook_status["telegram"]["state"] = "ACTIVE"
            _webhook_status["telegram"]["last_ok"] = tg_ok
        else:
            _webhook_status["telegram"]["state"] = "SKIPPED"
            _webhook_status["telegram"]["last_ok"] = True
        if dc_cfg:
            _webhook_status["discord"]["configured"] = True
            _webhook_status["discord"]["state"] = "ACTIVE"
            _webhook_status["discord"]["last_ok"] = dc_ok
        else:
            _webhook_status["discord"]["state"] = "SKIPPED"
            _webhook_status["discord"]["last_ok"] = True


def _dispatch_webhooks(event: AlertEvent) -> None:
    _dispatch_webhooks_text(event.format_message())


def _should_coalesce_event(event: AlertEvent) -> bool:
    """Burst-prone scalper/tuning traffic coalesces; security-critical events bypass."""
    if event.priority == EventPriority.CRITICAL:
        return False
    if event.category == EventCategory.SYSTEM_SECURITY:
        return False
    return True


def _flush_coalesce_batch(*, force: bool = False) -> None:
    global _coalesce_deadline, _coalesce_batches_sent
    with _coalesce_lock:
        if not _coalesce_buffer:
            _coalesce_deadline = 0.0
            return
        if not force and _coalesce_deadline > 0 and time.time() < _coalesce_deadline:
            return
        batch = list(_coalesce_buffer)
        _coalesce_buffer.clear()
        _coalesce_deadline = 0.0
    if not batch:
        return
    msg = _format_coalesced_batch_markdown(batch)
    try:
        if not _buffer_coalesced_telegram_digest(msg):
            _telegram_send(msg)
        _discord_send(msg)
        for item in batch:
            _record_broadcast(item, delivered=True)
        with _lock:
            _coalesce_batches_sent += 1
    except Exception as exc:
        log_engine(f"AlertReporting: coalesce flush failed {type(exc).__name__}: {exc}")
        for item in batch:
            _record_broadcast(item, delivered=False)


def flush_coalesce_to_hourly_digest(*, force: bool = True) -> None:
    """Force pending coalesced events into the hourly Telegram digest buffer."""
    _flush_coalesce_batch(force=force)


def _enqueue_coalesce_event(event: AlertEvent) -> None:
    global _coalesce_deadline
    window_sec = _resolve_coalesce_window_sec()
    with _coalesce_lock:
        if not _coalesce_buffer:
            _coalesce_deadline = time.time() + window_sec
        _coalesce_buffer.append(event)


def _record_broadcast(event: AlertEvent, *, delivered: bool) -> None:
    row = {
        "ts": event.ts,
        "category": event.category.value,
        "priority": event.priority.value,
        "title": event.title,
        "delivered": delivered,
    }
    with _lock:
        _broadcast_log.append(row)


def _worker_loop() -> None:
    """Async transmission worker — hourly coalescing buffer for burst scalper events."""
    while not _stop.is_set():
        try:
            timeout = _COALESCE_POLL_SEC
            with _coalesce_lock:
                if _coalesce_deadline > 0:
                    remaining = _coalesce_deadline - time.time()
                    if remaining <= 0:
                        timeout = 0.0
                    else:
                        timeout = min(_COALESCE_POLL_SEC, remaining)
            try:
                item = _event_queue.get(timeout=max(timeout, 0.05))
            except queue.Empty:
                _flush_coalesce_batch()
                continue
            if item is None:
                _flush_coalesce_batch(force=True)
                break
            try:
                skip_debug = _reporting_config().get("skip_debug_webhooks", False)
                if item.priority == EventPriority.DEBUG and skip_debug:
                    _record_broadcast(item, delivered=False)
                    continue
                if _should_coalesce_event(item):
                    _enqueue_coalesce_event(item)
                else:
                    _flush_coalesce_batch(force=True)
                    _dispatch_webhooks(item)
                    _record_broadcast(item, delivered=True)
            except Exception as exc:
                log_engine(f"AlertReporting: dispatch failed {type(exc).__name__}: {exc}")
                _record_broadcast(item, delivered=False)
            finally:
                _event_queue.task_done()
        except Exception as exc:
            log_engine(f"AlertReporting: worker loop {type(exc).__name__}: {exc}")


def enqueue_event(event: AlertEvent) -> bool:
    """Non-blocking enqueue — returns False if queue saturated."""
    try:
        _event_queue.put_nowait(event)
        return True
    except queue.Full:
        log_engine("AlertReporting: queue full — dropping oldest event")
        try:
            _event_queue.get_nowait()
            _event_queue.put_nowait(event)
            return True
        except queue.Empty:
            return False


def broadcast_critical_event_async(
    *,
    category: str | EventCategory,
    title: str,
    body: str,
    priority: str | EventPriority = EventPriority.CRITICAL,
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Fire-and-forget alert — never blocks caller."""
    cat = EventCategory(category) if isinstance(category, str) else category
    pri = EventPriority(priority) if isinstance(priority, str) else priority
    event = AlertEvent(
        category=cat,
        priority=pri,
        title=title,
        body=body,
        metadata=dict(metadata or {}),
    )
    return enqueue_event(event)


def notify_scalper_trade_event(
    *,
    ticker: str,
    title: str,
    body: str,
    slippage_pts: float | None = None,
    priority: str | EventPriority = EventPriority.INFO,
) -> bool:
    """Enqueue burst-prone micro-scalper telemetry — coalesced by the worker."""
    meta: dict[str, Any] = {"ticker": ticker, "epic": ticker}
    if slippage_pts is not None:
        meta["slippage_pts"] = slippage_pts
    return broadcast_critical_event_async(
        category=EventCategory.AUTO_TUNING,
        priority=priority,
        title=title,
        body=body,
        metadata=meta,
    )


def notify_circuit_breaker_trip(
    *,
    level: int,
    drawdown_pct: float,
    ticker: str = "PLATFORM",
    slippage_pts: float | None = None,
) -> None:
    broadcast_critical_event_async(
        category=EventCategory.SYSTEM_SECURITY,
        priority=EventPriority.CRITICAL,
        title=f"Circuit Breaker L{level} TRIPPED",
        body=(
            f"Volatility risk engine Level {level} active.\n"
            f"Drawdown: {drawdown_pct:.2f}%\n"
            f"Trading halted per L1/L2 policy."
        ),
        metadata={
            "ticker": ticker,
            "slippage_pts": slippage_pts,
            "status_line": "⚡ FLASHING — CIRCUIT BREAKER",
        },
    )


def notify_emergency_flatten(*, reason: str, closed: int, errors: int) -> None:
    broadcast_critical_event_async(
        category=EventCategory.SYSTEM_SECURITY,
        priority=EventPriority.CRITICAL,
        title="Chaos Guardian Emergency Flatten",
        body=(
            f"Reason: {reason}\n"
            f"Positions closed: {closed}\n"
            f"Errors: {errors}"
        ),
        metadata={
            "ticker": "ALL",
            "status_line": "⚡ FLASHING — EMERGENCY FLATTEN",
        },
    )


def notify_drift_clear(*, blockers: list[str], result: dict[str, Any]) -> None:
    """Critical alert when autonomic drift flattener clears boot blockers."""
    actions = result.get("actions") or []
    broadcast_critical_event_async(
        category=EventCategory.SYSTEM_SECURITY,
        priority=EventPriority.CRITICAL,
        title="Autonomic Drift Flattener — CLEARED",
        body=(
            f"Blockers cleared: {', '.join(blockers)}\n"
            f"Actions: {', '.join(str(a) for a in actions[:6])}\n"
            f"Trade ready: {result.get('trade_ready', False)}"
        ),
        metadata={
            "ticker": "RECONCILE",
            "status_line": "⚡ FLASHING — DRIFT CLEAR",
            "slippage_pts": result.get("avg_slippage_pts"),
        },
    )


def notify_pp_boundary_crossing(old_pp: int, new_pp: int) -> None:
    global _last_pp_tier
    tier = "standard"
    if new_pp >= PP_EXPANSION:
        tier = "expansion"
    elif new_pp <= PP_DEFENSE:
        tier = "defensive"
    old_tier = _last_pp_tier
    if tier == old_tier:
        return
    _last_pp_tier = tier
    rank = "standard"
    cap_exp_pct = 0.0
    try:
        from runtime.master_orchestrator import get_platform_scoreboard

        sb = get_platform_scoreboard()
        rank = sb.rank_label()
        cap_exp_pct = max(0.0, (sb.capacity_multiplier() - 1.0) * 100.0)
    except Exception:
        pass
    milestone_meta = {
        "platform_pp": new_pp,
        "operational_rank": rank,
        "capacity_expansion_pct": cap_exp_pct,
    }
    if tier == "expansion" and old_pp < PP_EXPANSION <= new_pp:
        broadcast_critical_event_async(
            category=EventCategory.GAMIFICATION,
            priority=EventPriority.INFO,
            title="Platform PP — Expansion Tier",
            body=f"PP crossed {PP_EXPANSION}: {old_pp} → {new_pp}. Capital utilization expanded.",
            metadata=milestone_meta,
        )
    elif tier == "defensive" and old_pp > PP_DEFENSE >= new_pp:
        broadcast_critical_event_async(
            category=EventCategory.GAMIFICATION,
            priority=EventPriority.INFO,
            title="Platform PP — Defensive Contraction",
            body=f"PP fell below {PP_DEFENSE}: {old_pp} → {new_pp}. Size factors compressed 50%.",
            metadata=milestone_meta,
        )


def notify_auto_tuning_digest(*, matrix: dict[str, Any], history: list[Any]) -> None:
    body_lines = ["Regime matrix adjustments:"]
    for state, row in sorted(matrix.items()):
        if isinstance(row, dict):
            body_lines.append(
                f"  State {state}: size={row.get('size_factor')} "
                f"stop={row.get('stop_factor')} limit={row.get('limit_factor')}"
            )
    if history:
        body_lines.append(f"Recent history entries: {len(history)}")
    broadcast_critical_event_async(
        category=EventCategory.AUTO_TUNING,
        priority=EventPriority.DEBUG,
        title="Daily Auto-Tuning Digest",
        body="\n".join(body_lines[:20]),
    )


def compile_eod_summary() -> dict[str, Any]:
    """Executive EOD snapshot from triage DB + platform telemetry."""
    since = time.time() - 86400.0
    trades: list[dict[str, Any]] = []
    try:
        from runtime.parameter_tuner import harvest_closed_trades

        trades = harvest_closed_trades(since_ts=since)
    except Exception:
        pass

    total = len(trades)
    wins = sum(1 for t in trades if float(t.get("net_pnl") or 0) > 0)
    win_rate = (wins / total) if total > 0 else 0.0
    net_pnl = sum(float(t.get("net_pnl") or 0) for t in trades)
    win_rate_delta = win_rate - WIN_RATE_TARGET
    pnl_delta = net_pnl - DAILY_PNL_TARGET_GBP

    avg_slippage = 0.0
    try:
        from runtime.parameter_tuner import _slippage_by_epic

        slip_map = _slippage_by_epic(since_ts=since)
        if slip_map:
            avg_slippage = sum(slip_map.values()) / len(slip_map)
    except Exception:
        pass

    peak_margin_pct = 0.0
    try:
        from runtime.portfolio_exploration_engine import get_exploration_state_snapshot

        explore = get_exploration_state_snapshot()
        peak_margin_pct = float(explore.get("capital_allocation_pct") or 0)
    except Exception:
        pass

    pp = 1000
    try:
        from runtime.master_orchestrator import get_platform_scoreboard

        pp = get_platform_scoreboard().total_pp
    except Exception:
        pass

    return {
        "ok": True,
        "period_hours": 24,
        "total_trades": total,
        "win_rate": round(win_rate, 4),
        "win_rate_target": WIN_RATE_TARGET,
        "win_rate_delta": round(win_rate_delta, 4),
        "net_pnl_gbp": round(net_pnl, 2),
        "pnl_target_gbp": DAILY_PNL_TARGET_GBP,
        "pnl_delta_gbp": round(pnl_delta, 2),
        "avg_slippage_pts": round(avg_slippage, 3),
        "peak_margin_utilization_pct": round(peak_margin_pct, 2),
        "capital_base_gbp": ACCOUNT_EQUITY_GBP,
        "platform_pp": pp,
        "ts": time.time(),
    }


def _format_eod_report(summary: dict[str, Any]) -> str:
    return (
        f"📊 EOD Executive Summary\n"
        f"Trades: {summary.get('total_trades', 0)}\n"
        f"Win rate: {float(summary.get('win_rate', 0)) * 100:.1f}% "
        f"(target {WIN_RATE_TARGET * 100:.0f}%, Δ {float(summary.get('win_rate_delta', 0)) * 100:+.1f}%)\n"
        f"Net P&L: £{summary.get('net_pnl_gbp', 0):.2f} "
        f"(target £{DAILY_PNL_TARGET_GBP:.0f}, Δ £{summary.get('pnl_delta_gbp', 0):+.2f})\n"
        f"Avg slippage: {summary.get('avg_slippage_pts', 0):.2f} pts\n"
        f"Peak margin: {summary.get('peak_margin_utilization_pct', 0):.1f}% "
        f"of £{ACCOUNT_EQUITY_GBP:,.0f}\n"
        f"Platform PP: {summary.get('platform_pp', 1000)}"
    )


def run_eod_report_once() -> dict[str, Any]:
    global _last_eod_ts
    summary = compile_eod_summary()
    _last_eod_ts = time.time()
    body = _format_eod_report(summary)
    broadcast_critical_event_async(
        category=EventCategory.EOD_REPORT,
        priority=EventPriority.INFO,
        title="Daily Executive Market Summary",
        body=body,
    )
    try:
        from runtime.parameter_tuner import get_regime_matrix, get_tuner_state_snapshot

        tuner = get_tuner_state_snapshot()
        notify_auto_tuning_digest(
            matrix=get_regime_matrix(),
            history=(tuner.get("optimization_history") or [])[-5:],
        )
    except Exception:
        pass
    summary["last_eod_ts"] = _last_eod_ts
    return summary


def _eod_loop() -> None:
    while not _stop.wait(_EOD_INTERVAL_SEC):
        try:
            run_eod_report_once()
        except Exception as exc:
            log_engine(f"AlertReporting: EOD failed {type(exc).__name__}: {exc}")


def _refresh_snapshot() -> None:
    _sync_webhook_idle_states()
    worker_alive = _worker_thread is not None and _worker_thread.is_alive()
    tg = _webhook_status.get("telegram") or {}
    dc = _webhook_status.get("discord") or {}
    webhooks_idle = (
        str(tg.get("state") or "") in ("IDLE", "SKIPPED")
        and str(dc.get("state") or "") in ("IDLE", "SKIPPED")
    )
    with _lock:
        depth = _event_queue.qsize()
        body = {
            "ok": True,
            "healthy": worker_alive,
            "subsystem_status": "IDLE" if webhooks_idle and worker_alive else "ACTIVE",
            "queue_depth": depth,
            "webhooks": dict(_webhook_status),
            "last_broadcasts": list(_broadcast_log)[-10:],
            "last_eod_ts": _last_eod_ts,
            "coalesce_window_sec": _COALESCE_WINDOW_SEC,
            "coalesce_batches_sent": _coalesce_batches_sent,
            "coalesce_buffer_depth": len(_coalesce_buffer),
            "ts": time.time(),
        }
        _snapshot.clear()
        _snapshot.update(body)


def get_reporting_status_snapshot() -> dict[str, Any]:
    _refresh_snapshot()
    with _lock:
        return dict(_snapshot)


def reporting_healthy() -> bool:
    """Healthy when worker runs; unconfigured webhooks (IDLE/SKIPPED) are not failures."""
    snap = get_reporting_status_snapshot()
    if not bool(snap.get("healthy", False)):
        return False
    webhooks = snap.get("webhooks") or {}
    for ch in ("telegram", "discord"):
        row = webhooks.get(ch) or {}
        state = str(row.get("state") or "IDLE").upper()
        if state == "ACTIVE" and not row.get("last_ok", True):
            return False
    return True


def ensure_alert_reporting_boot_ready() -> dict[str, Any]:
    """Boot helper — sync IDLE webhook states and start async worker without blocking."""
    _sync_webhook_idle_states()
    start_alert_reporting_matrix()
    snap = get_reporting_status_snapshot()
    snap["boot_ready"] = True
    return snap


def start_alert_reporting_matrix() -> None:
    global _worker_thread, _eod_thread
    _sync_webhook_idle_states()
    if _worker_thread is None or not _worker_thread.is_alive():
        _stop.clear()
        _worker_thread = threading.Thread(
            target=_worker_loop, name="alert-reporting-worker", daemon=True
        )
        _worker_thread.start()
    if _eod_thread is None or not _eod_thread.is_alive():
        _eod_thread = threading.Thread(target=_eod_loop, name="alert-eod-daemon", daemon=True)
        _eod_thread.start()
    log_engine("AlertReporting: matrix started (async queue + EOD daemon)")


def stop_alert_reporting_matrix() -> None:
    _stop.set()
    try:
        _event_queue.put_nowait(None)
    except queue.Full:
        pass


def set_coalesce_window_for_tests(sec: float) -> None:
    global _COALESCE_WINDOW_SEC
    _COALESCE_WINDOW_SEC = max(0.05, float(sec))


def flush_coalesce_buffer_for_tests() -> None:
    _flush_coalesce_batch(force=True)


def reset_alert_reporting_for_tests() -> None:
    global _worker_thread, _eod_thread, _last_pp_tier, _last_eod_ts
    global _COALESCE_WINDOW_SEC, _coalesce_deadline, _coalesce_batches_sent
    _stop.set()
    try:
        while True:
            _event_queue.get_nowait()
    except queue.Empty:
        pass
    _worker_thread = None
    _eod_thread = None
    _last_pp_tier = "standard"
    _last_eod_ts = 0.0
    _COALESCE_WINDOW_SEC = 3600.0
    _coalesce_deadline = 0.0
    _coalesce_batches_sent = 0
    with _coalesce_lock:
        _coalesce_buffer.clear()
    with _lock:
        _broadcast_log.clear()
        _webhook_status["telegram"] = {"configured": False, "state": "IDLE", "last_ok": True, "last_error": ""}
        _webhook_status["discord"] = {"configured": False, "state": "IDLE", "last_ok": True, "last_error": ""}
        _snapshot.clear()
        _snapshot.update(
            {
                "ok": True,
                "healthy": False,
                "queue_depth": 0,
                "webhooks": dict(_webhook_status),
                "last_broadcasts": [],
                "last_eod_ts": 0.0,
                "ts": 0.0,
            }
        )
    _stop.clear()
