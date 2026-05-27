"""
Japan225 daily risk guardrails — pause new entries for the rest of the JST
trading day when realized loss, consecutive losses, or trade count limits
are breached.

Tracks per JST trading day:
- realized PnL
- consecutive losses
- trades opened (bot entries)

When any configured threshold is hit, new entries are blocked at
LiveExecutor.execute(). Position management, exit submissions, broker
reconciliation, and IG sync are not gated.

State resets automatically on the next JST trading day.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from system.engine_log import log_engine


def _request_save() -> None:
    try:
        from system.runtime_state_persist import request_save

        request_save()
    except Exception:
        pass

JAPAN225_EPIC = "IX.D.NIKKEI.IFM.IP"
_JST = ZoneInfo("Asia/Tokyo")
_PAUSE_LOG_MSG = "Daily risk limit hit — entries paused until next JST session"

_lock = threading.RLock()


@dataclass
class DailyRiskCounters:
    jst_date: date
    realized_pnl: float = 0.0
    consecutive_losses: int = 0
    trades_opened: int = 0
    paused: bool = False
    pause_reason: str = ""
    log_emitted: bool = False


_counters: DailyRiskCounters | None = None


def _jst_date_now(now_utc: datetime | None = None) -> date:
    if now_utc is None:
        return datetime.now(_JST).date()
    src = now_utc if now_utc.tzinfo is not None else now_utc.replace(tzinfo=timezone.utc)
    return src.astimezone(_JST).date()


def _is_japan225_epic(epic: str) -> bool:
    if not epic:
        return False
    if epic == JAPAN225_EPIC:
        return True
    try:
        from system.market_watch.japan225_session import is_japan225_epic

        return bool(is_japan225_epic(epic))
    except Exception:
        return False


def _ensure_counters_for_today(
    now_utc: datetime | None = None,
) -> DailyRiskCounters:
    global _counters
    today = _jst_date_now(now_utc)
    if _counters is None or _counters.jst_date != today:
        rolled = _counters is not None and _counters.jst_date != today
        _counters = DailyRiskCounters(jst_date=today)
        if rolled:
            _request_save()
    return _counters


def _get_thresholds() -> tuple[float, int, int]:
    """Return (max_daily_loss_amount, max_consecutive_losses, max_trades_per_day)."""
    try:
        from system.config_loader import get_config

        cfg = get_config()
    except Exception:
        return 0.0, 0, 0
    max_loss = float(getattr(cfg, "max_daily_loss_amount", 0.0) or 0.0)
    max_consec = int(getattr(cfg, "max_consecutive_losses", 0) or 0)
    max_trades = int(getattr(cfg, "max_trades_per_day", 0) or 0)
    return max_loss, max_consec, max_trades


def _evaluate_paused(c: DailyRiskCounters) -> tuple[bool, str]:
    max_loss, max_consec, max_trades = _get_thresholds()
    if max_loss > 0 and c.realized_pnl <= -max_loss:
        return True, (
            f"daily realized P&L {c.realized_pnl:.2f} <= -{max_loss:.2f}"
        )
    if max_consec > 0 and c.consecutive_losses >= max_consec:
        return True, (
            f"consecutive losses {c.consecutive_losses} >= {max_consec}"
        )
    if max_trades > 0 and c.trades_opened >= max_trades:
        return True, (
            f"trades opened today {c.trades_opened} >= {max_trades}"
        )
    return False, ""


def _maybe_pause_and_log(c: DailyRiskCounters) -> None:
    paused, reason = _evaluate_paused(c)
    if paused and not c.paused:
        c.paused = True
        c.pause_reason = reason
    if c.paused and not c.log_emitted:
        c.log_emitted = True
        log_engine(_PAUSE_LOG_MSG)


def record_trade_opened(epic: str) -> None:
    """Increment opened-trade counter for the current JST day."""
    if not _is_japan225_epic(epic):
        return
    with _lock:
        c = _ensure_counters_for_today()
        c.trades_opened += 1
        _maybe_pause_and_log(c)
    _request_save()


def record_trade_closed(
    epic: str, *, pnl: float, result: str = ""
) -> None:
    """Update realized PnL and consecutive-loss streak for current JST day."""
    if not _is_japan225_epic(epic):
        return
    with _lock:
        c = _ensure_counters_for_today()
        c.realized_pnl += float(pnl)
        result_upper = str(result or "").upper()
        if result_upper == "LOSS" or (not result_upper and float(pnl) < 0):
            c.consecutive_losses += 1
        elif result_upper in ("WIN", "BREAKEVEN") or (
            not result_upper and float(pnl) > 0
        ):
            c.consecutive_losses = 0
        _maybe_pause_and_log(c)
    _request_save()


def is_paused(epic: str = JAPAN225_EPIC) -> bool:
    if not _is_japan225_epic(epic):
        return False
    with _lock:
        c = _ensure_counters_for_today()
        if not c.paused:
            _maybe_pause_and_log(c)
        return c.paused


def pause_reason(epic: str = JAPAN225_EPIC) -> str:
    if not _is_japan225_epic(epic):
        return ""
    with _lock:
        c = _ensure_counters_for_today()
        return c.pause_reason if c.paused else ""


def get_daily_counters(epic: str = JAPAN225_EPIC) -> DailyRiskCounters | None:
    if not _is_japan225_epic(epic):
        return None
    with _lock:
        return _ensure_counters_for_today()


def reset_state_for_tests() -> None:
    global _counters
    with _lock:
        _counters = None


def dump_daily_risk_state() -> dict[str, Any]:
    with _lock:
        if _counters is None:
            return {}
        return {
            "jst_date": _counters.jst_date.isoformat(),
            "realized_pnl": _counters.realized_pnl,
            "consecutive_losses": _counters.consecutive_losses,
            "trades_opened": _counters.trades_opened,
            "paused": _counters.paused,
            "pause_reason": _counters.pause_reason,
            "log_emitted": _counters.log_emitted,
        }


def load_daily_risk_state(data: dict[str, Any]) -> None:
    """Restore counters only if persisted JST date matches today's JST date."""
    global _counters
    if not isinstance(data, dict) or not data:
        return
    raw_date = str(data.get("jst_date") or "")
    if not raw_date:
        return
    try:
        persisted_date = date.fromisoformat(raw_date)
    except ValueError:
        return
    today = _jst_date_now()
    if persisted_date != today:
        with _lock:
            _counters = None
        return
    try:
        realized = float(data.get("realized_pnl") or 0.0)
        consec = int(data.get("consecutive_losses") or 0)
        trades = int(data.get("trades_opened") or 0)
    except (TypeError, ValueError):
        return
    with _lock:
        _counters = DailyRiskCounters(
            jst_date=persisted_date,
            realized_pnl=realized,
            consecutive_losses=consec,
            trades_opened=trades,
            paused=bool(data.get("paused")),
            pause_reason=str(data.get("pause_reason") or ""),
            log_emitted=bool(data.get("log_emitted")),
        )
