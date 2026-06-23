"""
Post-open audit hub — decoupled async Telegram dispatch.

1. Instant trade-close alerts (fulfillment cache → asyncio task).
2. Dual-horizon hourly summary (Last 1 Hour + Rolling 24 Hours).
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from requests.exceptions import RequestException, Timeout

from system.engine_log import log_engine

_HOURLY_INTERVAL_SEC = 3600.0
_LEDGER_MAX = 4096
_LONDON = None

_LOOP: asyncio.AbstractEventLoop | None = None
_LOOP_THREAD: threading.Thread | None = None
_HOURLY_THREAD: threading.Thread | None = None
_HOURLY_STOP = threading.Event()
_LEDGER: deque[dict[str, Any]] = deque(maxlen=_LEDGER_MAX)
_LEDGER_LOCK = threading.Lock()
_HUB_STARTED = False


def _london_tz():
    global _LONDON
    if _LONDON is None:
        from zoneinfo import ZoneInfo

        _LONDON = ZoneInfo("Europe/London")
    return _LONDON


def resolve_telegram_credentials() -> tuple[str, str]:
    """TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env, then config/credentials."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if token and chat:
        return token, chat
    try:
        from system.config_loader import get_config

        cfg = get_config()
        tg = getattr(cfg, "telegram", None) or {}
        if isinstance(tg, dict):
            token = token or str(tg.get("bot_token") or "").strip()
            chat = chat or str(tg.get("chat_id") or "").strip()
    except Exception:
        pass
    if not token or not chat:
        try:
            from system.credentials_loader import try_load_credentials

            status = try_load_credentials()
            if status.ok and status.credentials:
                creds = status.credentials
                token = token or str(getattr(creds, "telegram_bot_token", "") or "").strip()
                chat = chat or str(getattr(creds, "telegram_chat_id", "") or "").strip()
        except Exception:
            pass
    return token, chat


def telegram_configured() -> bool:
    token, chat = resolve_telegram_credentials()
    return bool(token and chat)


@dataclass
class DualHorizonMetrics:
    window_label: str
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    net_pnl_gbp: float = 0.0
    peak_gate_dropout: str = "—"

    @property
    def win_pct(self) -> float:
        if self.total_trades <= 0:
            return 0.0
        return 100.0 * float(self.wins) / float(self.total_trades)


def _parse_ts(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _ledger_rows_since(hours: float) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=float(hours))
    with _LEDGER_LOCK:
        rows = list(_LEDGER)
    out: list[dict[str, Any]] = []
    for row in rows:
        ts = _parse_ts(str(row.get("closed_at") or ""))
        if ts is None or ts >= cutoff:
            out.append(row)
    return out


def _peak_gate_dropout() -> str:
    try:
        from trading.gate_funnel_counter import read_funnel_snapshot

        snap = read_funnel_snapshot() or {}
        blocks = snap.get("first_block_counts") or {}
        if not isinstance(blocks, dict) or not blocks:
            return "—"
        best_gate = ""
        best_count = -1
        best_detail = ""
        for gate, details in blocks.items():
            if not isinstance(details, dict):
                continue
            for detail, count in details.items():
                c = int(count or 0)
                if c > best_count:
                    best_count = c
                    best_gate = str(gate)
                    best_detail = str(detail)
        if best_count < 0:
            return "—"
        return f"{best_gate} ({best_detail}) ×{best_count}"
    except Exception:
        return "—"


def compile_horizon_metrics(*, hours: float, label: str) -> DualHorizonMetrics:
    rows = _ledger_rows_since(hours)
    wins = sum(1 for r in rows if str(r.get("result", "")).upper() == "WIN")
    losses = sum(1 for r in rows if str(r.get("result", "")).upper() == "LOSS")
    net = sum(float(r.get("pnl_gbp") or 0.0) for r in rows)
    return DualHorizonMetrics(
        window_label=label,
        total_trades=len(rows),
        wins=wins,
        losses=losses,
        net_pnl_gbp=round(net, 2),
        peak_gate_dropout=_peak_gate_dropout(),
    )


def format_instant_trade_close(row: dict[str, Any]) -> str:
    epic = str(row.get("epic") or "—")
    action = str(row.get("direction") or row.get("action") or "—").upper()
    size = float(row.get("size") or 0.0)
    entry = float(row.get("entry") or 0.0)
    exit_px = float(row.get("exit") or row.get("exit_price") or 0.0)
    result = str(row.get("result") or "—").upper()
    pnl = float(row.get("pnl_gbp") or 0.0)
    icon = "✅" if result == "WIN" else "❌"
    now = datetime.now(_london_tz()).strftime("%H:%M BST")
    return (
        f"{icon} TRADE CLOSED — {epic}\n"
        f"Action: {action} | Size: {size:g}\n"
        f"Entry: {entry:,.2f} → Exit: {exit_px:,.2f}\n"
        f"Outcome: {result} | Realised P&L: £{pnl:+,.2f}\n"
        f"⏱ {now}"
    )


def format_dual_horizon_summary() -> str:
    h1 = compile_horizon_metrics(hours=1.0, label="Last 1 Hour")
    h24 = compile_horizon_metrics(hours=24.0, label="Rolling 24 Hours")
    now = datetime.now(_london_tz()).strftime("%H:%M BST")

    def _block(m: DualHorizonMetrics) -> str:
        return (
            f"▸ {m.window_label}\n"
            f"  Trades: {m.total_trades} | Win%: {m.win_pct:.1f}% "
            f"({m.wins}W/{m.losses}L)\n"
            f"  Net P&L: £{m.net_pnl_gbp:+,.2f}\n"
            f"  Peak gate dropout: {m.peak_gate_dropout}"
        )

    return (
        "📊 IG Agent — Dual-Horizon Audit\n"
        f"{_block(h1)}\n"
        f"{_block(h24)}\n"
        f"🕐 {now}"
    )


async def _telegram_send_async(text: str) -> bool:
    body = str(text or "").strip()
    if not body:
        return False
    token, chat = resolve_telegram_credentials()
    if not token or not chat:
        log_engine("PostOpenAudit: telegram skipped — credentials missing")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat,
        "text": body,
        "disable_web_page_preview": True,
    }

    def _post() -> bool:
        try:
            resp = requests.post(url, json=payload, timeout=15)
            return resp.status_code < 400
        except (Timeout, RequestException) as exc:
            log_engine(f"PostOpenAudit telegram network error: {exc}")
            return False
        except Exception as exc:
            log_engine(f"PostOpenAudit telegram error: {type(exc).__name__}: {exc}")
            return False

    return await asyncio.to_thread(_post)


def _ensure_async_loop() -> asyncio.AbstractEventLoop:
    global _LOOP, _LOOP_THREAD
    if _LOOP is not None and _LOOP.is_running():
        return _LOOP

    started = threading.Event()

    def _runner() -> None:
        global _LOOP
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _LOOP = loop
        started.set()
        loop.run_forever()

    _LOOP_THREAD = threading.Thread(target=_runner, name="post-open-audit-async", daemon=True)
    _LOOP_THREAD.start()
    started.wait(timeout=5.0)
    if _LOOP is None:
        raise RuntimeError("PostOpenAudit async loop failed to start")
    return _LOOP


def _schedule(coro: Any) -> None:
    loop = _ensure_async_loop()
    asyncio.run_coroutine_threadsafe(coro, loop)


async def _dispatch_instant_close_async(row: dict[str, Any]) -> None:
    text = format_instant_trade_close(row)
    ok = await _telegram_send_async(text)
    if ok:
        log_engine(f"PostOpenAudit: instant close dispatched epic={row.get('epic')}")
    else:
        log_engine("PostOpenAudit: instant close dispatch failed")


async def _dispatch_hourly_summary_async() -> None:
    text = format_dual_horizon_summary()
    ok = await _telegram_send_async(text)
    if ok:
        log_engine("PostOpenAudit: dual-horizon hourly summary dispatched")
    else:
        log_engine("PostOpenAudit: hourly summary dispatch failed")


def record_closed_trade(row: dict[str, Any]) -> None:
    """Append to audit ledger and fire instant Telegram (decoupled asyncio task)."""
    enriched = dict(row)
    enriched.setdefault("closed_at", datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
    with _LEDGER_LOCK:
        _LEDGER.append(enriched)
    try:
        _schedule(_dispatch_instant_close_async(enriched))
    except Exception as exc:
        log_engine(f"PostOpenAudit schedule close failed: {type(exc).__name__}: {exc}")


def dispatch_hourly_dual_horizon_summary() -> None:
    """Hourly cron entry — non-blocking via asyncio.create_task equivalent."""
    try:
        _schedule(_dispatch_hourly_summary_async())
    except Exception as exc:
        log_engine(f"PostOpenAudit hourly schedule failed: {type(exc).__name__}: {exc}")


def _hourly_loop() -> None:
    while not _HOURLY_STOP.wait(_HOURLY_INTERVAL_SEC):
        try:
            dispatch_hourly_dual_horizon_summary()
        except Exception as exc:
            log_engine(f"PostOpenAudit hourly loop: {type(exc).__name__}: {exc}")


def start_post_open_audit_hub(*, hourly: bool = True) -> None:
    """Arm async Telegram pipeline + optional hourly dual-horizon scheduler."""
    global _HUB_STARTED, _HOURLY_THREAD
    if _HUB_STARTED:
        return
    _HUB_STARTED = True
    _ensure_async_loop()
    if hourly and os.environ.get("IG_DISABLE_POST_OPEN_AUDIT_HOURLY", "").strip() not in (
        "1",
        "true",
        "yes",
    ):
        _HOURLY_STOP.clear()
        _HOURLY_THREAD = threading.Thread(
            target=_hourly_loop,
            name="post-open-audit-hourly",
            daemon=True,
        )
        _HOURLY_THREAD.start()
    log_engine(
        f"PostOpenAuditHub: armed telegram={telegram_configured()} hourly={hourly}"
    )


def stop_post_open_audit_hub() -> None:
    global _HUB_STARTED, _HOURLY_THREAD, _LOOP
    _HOURLY_STOP.set()
    _HUB_STARTED = False
    if _LOOP is not None and _LOOP.is_running():
        _LOOP.call_soon_threadsafe(_LOOP.stop)


def reset_post_open_audit_for_tests() -> None:
    stop_post_open_audit_hub()
    global _LEDGER, _LOOP, _LOOP_THREAD, _HOURLY_THREAD, _HUB_STARTED
    _HUB_STARTED = False
    with _LEDGER_LOCK:
        _LEDGER.clear()
    if _LOOP is not None and _LOOP.is_running():
        try:
            _LOOP.call_soon_threadsafe(_LOOP.stop)
        except Exception:
            pass
    if _LOOP_THREAD is not None and _LOOP_THREAD.is_alive():
        _LOOP_THREAD.join(timeout=2.0)
    _LOOP = None
    _LOOP_THREAD = None
    _HOURLY_THREAD = None
