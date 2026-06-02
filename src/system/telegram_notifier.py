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

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from system.engine_log import log_engine

_LONDON = ZoneInfo("Europe/London")
_HEARTBEAT_INTERVAL_SEC = 30 * 60
_ALERT_DEDUPE_SEC = 300.0

_lock = threading.RLock()
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
                    log_engine(
                        f"telegram heartbeat failed: {type(e).__name__}: {e}"
                    )

        _heartbeat_thread = threading.Thread(
            target=_loop, name="telegram-heartbeat", daemon=True
        )
        _heartbeat_thread.start()


def stop_telegram_heartbeat() -> None:
    _heartbeat_stop.set()


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

    def _send_sync(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = json.dumps(
            {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status >= 400:
                    log_engine(f"telegram API HTTP {resp.status}")
        except urllib.error.HTTPError as e:
            log_engine(f"telegram API HTTP error: {e.code} {e.reason}")
        except urllib.error.URLError as e:
            log_engine(f"telegram API network error: {e.reason}")
        except Exception as e:
            log_engine(f"telegram send failed: {type(e).__name__}: {e}")

    def send_now(self, text: str) -> None:
        """Blocking send — for tests and startup when the caller may return immediately."""
        if not self.enabled:
            return
        body = text.strip()
        if not body:
            return
        self._send_sync(body)

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

    def _alert_deduped(self, key: str, text: str, *, dedupe_sec: float = _ALERT_DEDUPE_SEC) -> None:
        now = time.time()
        with _lock:
            last = self._alert_last_sent.get(key, 0.0)
            if now - last < dedupe_sec:
                return
            self._alert_last_sent[key] = now
        self._send_async(text)

    def send_alert(self, message: str, *, dedupe_key: str | None = None) -> None:
        line = message if message.startswith(("⚠️", "❌", "💓", "🟢", "🔴")) else f"⚠️ {message}"
        if dedupe_key:
            self._alert_deduped(dedupe_key, line)
        else:
            self._send_async(line)

    def notify_startup(self, *, state_restored: bool = True) -> None:
        if state_restored:
            self.send_now("⚠️ Agent restarted — state restored")
        else:
            self.send_now("⚠️ Agent restarted")

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
            f"Points state: {old_state} → {new_state}\n"
            f"Cumulative: {cumulative:.1f}",
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
    ) -> None:
        if direction == "BUY":
            stop_delta = stop - entry
            limit_delta = target - entry
        else:
            stop_delta = entry - stop
            limit_delta = entry - target
        text = (
            f"🟢 Trade Opened — {market}\n"
            f"Direction: {direction}\n"
            f"Entry: {self._fmt_price(entry)}\n"
            f"Size: {size:g}\n"
            f"Stop: {self._fmt_price(stop)} ({self._fmt_signed_pts(stop_delta)})\n"
            f"Limit: {self._fmt_price(target)} ({self._fmt_signed_pts(limit_delta)})\n"
            f"Signal: {signal_pct:.0f}% | Fitness: {fitness_pct:.0f}%"
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
        if pnl_gbp is not None:
            gbp_sign = "+" if pnl_gbp >= 0 else ""
            pnl_line = f"P&L: {gbp_sign}£{abs(pnl_gbp):,.2f} ({self._fmt_signed_pts(pnl_pts)})"
        else:
            pnl_line = f"P&L: {self._fmt_signed_pts(pnl_pts)}"
        dur_line = (
            f"Duration: {int(duration_mins)} mins"
            if duration_mins is not None
            else "Duration: —"
        )
        pts_line = ""
        if points_before is not None and points_after is not None:
            pts_line = (
                f"\nPoints: {points_before:.1f} → {points_after:.1f} ({points_state})"
            )
        text = (
            f"🔴 Trade Closed — {market}\n"
            f"Direction: {direction}\n"
            f"Entry: {self._fmt_price(entry)} → Exit: {self._fmt_price(exit_price)}\n"
            f"{pnl_line}\n"
            f"{dur_line}"
            f"{pts_line}"
        )
        self._send_async(text)

    def send_heartbeat(self, snapshot: dict[str, Any]) -> None:
        fitness = float(snapshot.get("fitness") or 0)
        signal = float(snapshot.get("signal") or 0)
        stream = str(snapshot.get("stream") or "DISCONNECTED")
        positions = int(snapshot.get("positions") or 0)
        cumulative = float(snapshot.get("cumulative") or 0)
        state = str(snapshot.get("state") or "CAUTION")
        text = (
            f"💓 Agent alive {self._now_bst()}\n"
            f"Fitness: {fitness:.0f}% | Signal: {signal:.0f}%\n"
            f"Stream: {stream} | Positions: {positions}\n"
            f"Points: {cumulative:.1f} {state}"
        )
        self._send_async(text)
