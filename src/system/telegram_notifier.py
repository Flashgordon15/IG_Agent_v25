"""
Telegram bot notifications for IG Agent v25.

Setup (disabled until configured):
  1. Open Telegram and message @BotFather → /newbot → copy the bot token.
  2. Start a chat with your new bot (Send /start).
  3. Get chat_id: message the bot, then open in a browser:
     https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
     and read message.chat.id (negative for groups).
  4. In config/config_v25.json set:
       "telegram": { "enabled": true, "bot_token": "<token>", "chat_id": "<id>" }
  5. Restart the agent.

All sends are fire-and-forget; API failures are logged only and never block trading.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

import requests
from requests.exceptions import RequestException, Timeout
from typing import Any, Callable
from zoneinfo import ZoneInfo

from system.engine_log import log_engine

_LONDON = ZoneInfo("Europe/London")
_HEARTBEAT_INTERVAL_SEC = 60 * 60
_ALERT_DEDUPE_SEC = 300.0
_UNRESOLVED_ALERT_DEDUPE_SEC = 900.0

_lock = threading.RLock()
_unresolved_alert_last: dict[str, float] = {}
_instance: TelegramNotifier | None = None
_heartbeat_provider: Callable[[], dict[str, Any]] | None = None
_heartbeat_thread: threading.Thread | None = None
_heartbeat_stop = threading.Event()


def configure_telegram(cfg: Any) -> TelegramNotifier:
    """Build or refresh the global notifier from Config or a telegram dict."""
    global _instance
    data: dict[str, Any] = {}
    if hasattr(cfg, "telegram"):
        data = dict(cfg.telegram)
    elif isinstance(cfg, dict):
        raw = cfg.get("telegram")
        data = dict(raw) if isinstance(raw, dict) else {}
    with _lock:
        _instance = TelegramNotifier.from_config(data)
    return _instance


def get_telegram_notifier() -> TelegramNotifier | None:
    with _lock:
        return _instance


def set_heartbeat_provider(fn: Callable[[], dict[str, Any]] | None) -> None:
    global _heartbeat_provider
    with _lock:
        _heartbeat_provider = fn


def start_telegram_heartbeat(interval_sec: float = _HEARTBEAT_INTERVAL_SEC) -> None:
    """Start daemon thread that sends periodic heartbeat messages."""
    global _heartbeat_thread
    with _lock:
        if _heartbeat_thread is not None and _heartbeat_thread.is_alive():
            return
        _heartbeat_stop.clear()

        def _loop() -> None:
            while not _heartbeat_stop.wait(interval_sec):
                try:
                    notifier = get_telegram_notifier()
                    if notifier is None or not notifier.enabled:
                        continue
                    provider = _heartbeat_provider
                    payload = provider() if provider else {}
                    notifier.send_heartbeat(payload)
                except Exception as e:
                    log_engine(f"telegram heartbeat failed: {type(e).__name__}: {e}")

        _heartbeat_thread = threading.Thread(
            target=_loop, name="telegram-heartbeat", daemon=True
        )
        _heartbeat_thread.start()


def stop_telegram_heartbeat() -> None:
    _heartbeat_stop.set()


def executive_status_only_enabled(cfg: Any | None = None) -> bool:
    """When True, status noise is suppressed; hourly executive + trade fills only."""
    try:
        if cfg is None:
            from system.config_loader import get_config

            cfg = get_config()
        tg = getattr(cfg, "telegram", None)
        if isinstance(tg, dict):
            return bool(tg.get("executive_status_only", True))
        return True
    except Exception:
        return True


def _preserve_alert_in_executive_mode(message: str) -> bool:
    """Trade fills and true emergencies still notify when executive mode is on."""
    body = str(message or "").strip()
    if not body:
        return False
    if body.startswith(("✅ WIN", "❌ LOSS")):
        return True
    lower = body.lower()
    preserve = (
        "drawdown limit",
        "agent crash",
        "agent stopped",
        "trading loops stopped",
        "emergency",
        "broker confirm overdue",
        "entry halt",
        "protection fail",
        "equity circuit",
        "startup blocked",
        "watchdog fatal",
        "manual intervention",
        "[risk shield]",
        "risk shield",
    )
    return any(token in lower for token in preserve)


def send_unresolved_order_alert(
    epic: str,
    *,
    age_seconds: float = 0.0,
    order_type: str = "",
    deal_reference: str = "",
) -> bool:
    """
    Throttled alert when broker confirm is genuinely overdue (>30s).

    Normal in-flight confirms (<30s) must not notify — scalping async worker
    routinely takes 2–15s on IG DEMO.
    """
    key = str(epic or "").strip()
    if not key:
        return False
    now = time.time()
    with _lock:
        last = _unresolved_alert_last.get(key, 0.0)
        if now - last < _UNRESOLVED_ALERT_DEDUPE_SEC:
            return False
        _unresolved_alert_last[key] = now
    kind = str(order_type or "order").lower()
    ref = str(deal_reference or "").strip() or "—"
    body = (
        f"Broker confirm overdue — {key}\n"
        f"Type: {kind} | Age: {int(age_seconds)}s | Ref: {ref}\n"
        f"Entries paused until IG reconcile (not a failed trade yet)"
    )
    return send_critical_alert(body, dedupe_key=f"unresolved:{key}")


def send_critical_alert(message: str, *, dedupe_key: str | None = None) -> bool:
    """Send an immediate critical alert (blocking). Logs all failures; never raises."""
    body = str(message or "").strip()
    if not body:
        log_engine("telegram critical alert skipped: empty message")
        return False
    if executive_status_only_enabled() and not _preserve_alert_in_executive_mode(body):
        log_engine(f"telegram status alert suppressed (executive mode): {body[:100]}")
        return False
    if body.startswith("🚨 IG Agent CRITICAL"):
        text = body
    elif body.startswith("🚨"):
        text = f"🚨 IG Agent CRITICAL\n{body[1:].lstrip()}"
    else:
        text = f"🚨 IG Agent CRITICAL\n{body}"
    try:
        notifier = get_telegram_notifier()
        if notifier is None or not notifier.enabled:
            log_engine(
                "telegram critical alert NOT SENT — notifier disabled "
                "(set telegram.enabled=true and bot_token/chat_id in config "
                "or credentials.json)"
            )
            return False
        if dedupe_key:
            now = time.time()
            with _lock:
                last = notifier._alert_last_sent.get(dedupe_key, 0.0)
                if now - last < _UNRESOLVED_ALERT_DEDUPE_SEC:
                    return False
                notifier._alert_last_sent[dedupe_key] = now
        ok = notifier.send_now(text)
        if not ok:
            log_engine(f"TELEGRAM ALERTS NOT WORKING: send failed for: {body[:120]}")
        return ok
    except Exception as e:
        log_engine(f"TELEGRAM ALERTS NOT WORKING: {type(e).__name__}: {e}")
        return False


def send_startup_test() -> bool:
    """Verify Telegram delivery at bootstrap. Logs clearly on failure."""
    if executive_status_only_enabled():
        log_engine("telegram startup test skipped — executive status mode")
        return False
    try:
        notifier = get_telegram_notifier()
        if notifier is None or not notifier.enabled:
            log_engine(
                "TELEGRAM ALERTS NOT WORKING: notifier disabled "
                "(missing bot_token or chat_id)"
            )
            return False
        ok = notifier.send_now("IG Agent v25 online — alerts active")
        if not ok:
            log_engine("TELEGRAM ALERTS NOT WORKING: startup test send failed")
        return ok
    except Exception as e:
        log_engine(f"TELEGRAM ALERTS NOT WORKING: {type(e).__name__}: {e}")
        return False


class TelegramNotifier:
    """Async Telegram Bot API client (sendMessage only)."""

    def __init__(
        self,
        *,
        enabled: bool,
        bot_token: str,
        chat_id: str,
    ) -> None:
        self.enabled = bool(enabled) and bool(bot_token) and bool(chat_id)
        self.bot_token = str(bot_token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self._alert_last_sent: dict[str, float] = {}
        self._stream_stale_sent: set[str] = set()

    @classmethod
    def from_config(cls, data: dict[str, Any] | None) -> TelegramNotifier:
        raw = data or {}
        return cls(
            enabled=bool(raw.get("enabled", False)),
            bot_token=str(raw.get("bot_token", "") or ""),
            chat_id=str(raw.get("chat_id", "") or ""),
        )

    def _send_async(self, text: str) -> None:
        if not self.enabled:
            return
        body = text.strip()
        if not body:
            return
        threading.Thread(
            target=self._send_sync,
            args=(body,),
            name="telegram-send",
            daemon=True,
        ).start()

    def _send_sync(self, text: str) -> bool:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code >= 400:
                log_engine(f"telegram API HTTP {resp.status_code}")
                return False
            return True
        except Timeout:
            log_engine("telegram API network error: timeout")
        except RequestException as e:
            log_engine(f"telegram API network error: {e}")
        except Exception as e:
            log_engine(f"telegram send failed: {type(e).__name__}: {e}")
        return False

    def send_now(self, text: str) -> bool:
        """Blocking send — for tests and startup when the caller may return immediately."""
        if not self.enabled:
            return False
        body = text.strip()
        if not body:
            return False
        return self._send_sync(body)

    @staticmethod
    def _fmt_price(value: float) -> str:
        return f"{float(value):,.0f}"

    @staticmethod
    def _fmt_signed_pts(delta: float) -> str:
        sign = "+" if delta >= 0 else ""
        return f"{sign}{delta:.0f} pts"

    @staticmethod
    def _now_bst() -> str:
        return datetime.now(_LONDON).strftime("%H:%M BST")

    def _alert_deduped(
        self, key: str, text: str, *, dedupe_sec: float = _ALERT_DEDUPE_SEC
    ) -> None:
        if executive_status_only_enabled():
            return
        now = time.time()
        with _lock:
            last = self._alert_last_sent.get(key, 0.0)
            if now - last < dedupe_sec:
                return
            self._alert_last_sent[key] = now
        self._send_async(text)

    def send(self, text: str) -> None:
        """Async send — convenience alias for fire-and-forget messages."""
        self._send_async(text)

    def send_alert(self, message: str, *, dedupe_key: str | None = None) -> None:
        line = (
            message
            if message.startswith(("⚠️", "❌", "💓", "🟢", "🔴", "🚨"))
            else f"⚠️ {message}"
        )
        if dedupe_key:
            self._alert_deduped(dedupe_key, line)
        else:
            self._send_async(line)

    def notify_startup(
        self,
        *,
        state_restored: bool = True,
        market_count: int = 0,
        points_state: str = "CAUTION",
    ) -> None:
        if executive_status_only_enabled():
            return
        markets_line = f"Markets: {market_count} active | " if market_count else ""
        self.send_now(f"🟢 IG Agent v25 started\n{markets_line}Points: {points_state}")

    def notify_shutdown(self) -> None:
        self.send_now("🔴 IG Agent v25 stopped")

    def notify_critical(self, message: str) -> None:
        self._send_async(f"🚨 CRITICAL: {message}")

    def notify_crash(self, error: str) -> None:
        self._send_async(f"❌ Agent crash — check logs immediately\n{error[:500]}")

    def notify_stream_stale(self, epic: str, tick_age_s: float) -> None:
        if epic in self._stream_stale_sent:
            return
        self._stream_stale_sent.add(epic)
        self.send_alert(
            f"Stream STALE >{int(tick_age_s)}s — switching to REST poll\nEpic: {epic}",
            dedupe_key=f"stale:{epic}",
        )

    def clear_stream_stale(self, epic: str) -> None:
        self._stream_stale_sent.discard(epic)

    def notify_rest_fallback(self) -> None:
        self.send_alert(
            "Stream unavailable — switching to REST poll",
            dedupe_key="rest_fallback",
        )

    def notify_points_state_change(
        self,
        old_state: str,
        new_state: str,
        cumulative: float,
    ) -> None:
        self.send_alert(
            f"Points state: {old_state} → {new_state}\nCumulative: {cumulative:.1f}",
            dedupe_key=f"points:{old_state}:{new_state}",
        )

    def notify_trade_opened(
        self,
        *,
        market: str,
        direction: str,
        entry: float,
        size: float,
        stop: float,
        target: float,
        signal_pct: float,
        fitness_pct: float,
        points_state: str = "CAUTION",
        order_type: str = "MARKET",
        protected: bool = True,
        scalping: bool = False,
    ) -> None:
        mode = "SCALP" if scalping else "STD"
        entry_label = str(order_type or "MARKET").upper()
        prot = "SL+TP ✓" if protected else "UNPROTECTED"
        text = (
            f"📈 {market} {direction} [{mode}] {entry_label} @ {self._fmt_price(entry)}\n"
            f"Size:{size:g} Stop:{self._fmt_price(stop)} TP:{self._fmt_price(target)}\n"
            f"Protection: {prot} | Signal:{signal_pct:.0f}% Fitness:{fitness_pct:.0f}%\n"
            f"Points:{points_state}"
        )
        self._send_async(text)

    def notify_trade_closed(
        self,
        *,
        market: str,
        direction: str,
        entry: float,
        exit_price: float,
        pnl_gbp: float | None,
        pnl_pts: float,
        duration_mins: float | None,
        points_before: float | None,
        points_after: float | None,
        points_state: str,
    ) -> None:
        win = (pnl_gbp if pnl_gbp is not None else pnl_pts) >= 0
        emoji = "✅ WIN" if win else "❌ LOSS"
        pts_sign = "+" if pnl_pts >= 0 else ""
        pts_str = f"{pts_sign}{pnl_pts:.1f}pts"
        cumulative = points_after if points_after is not None else 0.0
        dur = f" ({duration_mins:.0f}m)" if duration_mins is not None else ""
        lines = [f"{emoji} {market} {direction}{dur}"]
        if pnl_gbp is not None:
            gbp_sign = "+" if pnl_gbp >= 0 else ""
            lines.append(f"P&L: {gbp_sign}£{pnl_gbp:,.2f} | {pts_str}")
        else:
            lines.append(f"P&L: {pts_str}")
        lines.append(f"Entry: {entry:.5f} → Exit: {exit_price:.5f}")
        lines.append(f"Cumulative: {cumulative:.1f}pts {points_state}")
        self._send_async("\n".join(lines))

    def send_heartbeat(self, snapshot: dict[str, Any]) -> None:
        if executive_status_only_enabled():
            return
        fitness = float(snapshot.get("fitness") or 0)
        signal = float(snapshot.get("signal") or 0)
        stream = str(snapshot.get("stream") or "DISCONNECTED")
        positions = int(snapshot.get("positions") or 0)
        cumulative = float(snapshot.get("cumulative") or 0)
        state = str(snapshot.get("state") or "CAUTION")
        balance = snapshot.get("balance")
        daily_pnl = snapshot.get("daily_pnl")
        balance_line = ""
        if balance is not None:
            pnl_sign = "+" if float(daily_pnl or 0) >= 0 else ""
            pnl_str = (
                f" | Daily P&L: {pnl_sign}£{float(daily_pnl or 0):,.2f}"
                if daily_pnl is not None
                else ""
            )
            balance_line = f"\nBalance: £{float(balance):,.2f}{pnl_str}"
        text = (
            f"💓 Hourly update {self._now_bst()}\n"
            f"Fitness: {fitness:.0f}% | Signal: {signal:.0f}%\n"
            f"Stream: {stream} | Positions: {positions}\n"
            f"Points: {cumulative:.1f} {state}"
            f"{balance_line}"
        )
        self._send_async(text)
