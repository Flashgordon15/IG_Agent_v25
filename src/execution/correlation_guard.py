"""
Portfolio-level correlation guard — caps new entries per direction per session.

Professional trading systems cap directional concentration to prevent the portfolio
from becoming a one-way bet when all markets gap together (e.g. risk-off open).
This guard blocks new entries once MAX_NEW_PER_DIRECTION entries in the same
direction have been submitted in the current session window.

This is a soft gate checked BEFORE order submission — it does not affect positions
already open, only new entries. The counter resets when reset_session() is called
(typically on each new trading session open).
"""

from __future__ import annotations

import json
import threading
from datetime import datetime

from system.engine_log import log_engine
from system.paths import data_dir

_lock = threading.Lock()
_buy_count: int = 0
_sell_count: int = 0
_buy_risk_gbp: float = 0.0
_sell_risk_gbp: float = 0.0
_session_key: str = ""

MAX_NEW_PER_DIRECTION = 5  # max new entries in the same direction per calendar day
_enabled: bool = True
_STATE_FILE = data_dir() / "state" / "correlation_guard.json"


def _max_same_direction_risk_gbp() -> float:
    try:
        from system.v26_config import load_v26_overlay

        regime = load_v26_overlay().get("regime") or {}
        return float(regime.get("max_same_direction_risk_gbp") or 0)
    except Exception:
        return 0.0


def _session_date_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _is_date_session_key(key: str) -> bool:
    if len(key) != 10:
        return False
    try:
        datetime.strptime(key, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _persist_state() -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(
            json.dumps(
                {
                    "buy": _buy_count,
                    "sell": _sell_count,
                    "buy_risk_gbp": round(_buy_risk_gbp, 2),
                    "sell_risk_gbp": round(_sell_risk_gbp, 2),
                    "session": _session_key,
                }
            ),
            encoding="utf-8",
        )
    except Exception as e:
        log_engine(f"correlation_guard: persist failed: {type(e).__name__}: {e}")


def _load_state() -> None:
    global _buy_count, _sell_count, _buy_risk_gbp, _sell_risk_gbp, _session_key
    if not _STATE_FILE.is_file():
        return
    try:
        raw = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        _session_key = str(raw.get("session") or "")
        _buy_count = int(raw.get("buy") or 0)
        _sell_count = int(raw.get("sell") or 0)
        _buy_risk_gbp = float(raw.get("buy_risk_gbp") or 0)
        _sell_risk_gbp = float(raw.get("sell_risk_gbp") or 0)
        log_engine(
            f"correlation_guard: restored buy={_buy_count} sell={_sell_count} "
            f"buy_£={_buy_risk_gbp:.0f} sell_£={_sell_risk_gbp:.0f} "
            f"session={_session_key}"
        )
    except Exception as e:
        log_engine(f"correlation_guard: load failed: {type(e).__name__}: {e}")


_load_state()


def reset_correlation_guard_for_tests() -> None:
    """Clear in-memory and on-disk guard state between pytest cases."""
    global _buy_count, _sell_count, _buy_risk_gbp, _sell_risk_gbp, _session_key
    with _lock:
        _session_key = ""
        _buy_count = 0
        _sell_count = 0
        _buy_risk_gbp = 0.0
        _sell_risk_gbp = 0.0
    try:
        _STATE_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def reset_session(*, key: str | None = None) -> None:
    global _buy_count, _sell_count, _buy_risk_gbp, _sell_risk_gbp, _session_key
    with _lock:
        _session_key = key or _session_date_key()
        _buy_count = 0
        _sell_count = 0
        _buy_risk_gbp = 0.0
        _sell_risk_gbp = 0.0
        _persist_state()
    log_engine(f"correlation_guard: session reset key={_session_key}")


def _maybe_auto_reset() -> None:
    global _buy_count, _sell_count, _buy_risk_gbp, _sell_risk_gbp, _session_key
    today = _session_date_key()
    if not _session_key:
        _session_key = today
        _persist_state()
        return
    if not _is_date_session_key(_session_key):
        return
    if _session_key != today:
        _session_key = today
        _buy_count = 0
        _sell_count = 0
        _buy_risk_gbp = 0.0
        _sell_risk_gbp = 0.0
        _persist_state()


def rehydrate_direction_risk(
    *,
    buy_risk_gbp: float = 0.0,
    sell_risk_gbp: float = 0.0,
) -> None:
    """Set open-position £ heat by direction (agent restart)."""
    global _buy_risk_gbp, _sell_risk_gbp
    with _lock:
        _buy_risk_gbp = max(0.0, float(buy_risk_gbp))
        _sell_risk_gbp = max(0.0, float(sell_risk_gbp))
        _persist_state()
    log_engine(
        f"correlation_guard: rehydrated open risk "
        f"BUY=£{_buy_risk_gbp:.0f} SELL=£{_sell_risk_gbp:.0f}"
    )


def confirm_direction_risk(direction: str, risk_gbp: float) -> None:
    """Add open £ risk after a fill confirms (check_and_record only reserves count)."""
    global _buy_risk_gbp, _sell_risk_gbp
    risk = max(0.0, float(risk_gbp))
    if risk <= 0:
        return
    with _lock:
        d = str(direction or "").upper()
        if d == "BUY":
            _buy_risk_gbp += risk
        elif d == "SELL":
            _sell_risk_gbp += risk
        _persist_state()


def release_direction_risk(direction: str, risk_gbp: float) -> None:
    """Release open £ risk when a position closes."""
    global _buy_risk_gbp, _sell_risk_gbp
    risk = max(0.0, float(risk_gbp))
    if risk <= 0:
        return
    with _lock:
        d = str(direction or "").upper()
        if d == "BUY":
            _buy_risk_gbp = max(0.0, _buy_risk_gbp - risk)
        elif d == "SELL":
            _sell_risk_gbp = max(0.0, _sell_risk_gbp - risk)
        _persist_state()


def check_and_record(direction: str, *, risk_gbp: float = 0.0) -> tuple[bool, str]:
    """
    Return (allowed, reason).

    Records the entry if allowed. Call this just before submitting an order;
    if the order is later rejected by the broker, call undo() to release the slot.
    """
    global _buy_count, _sell_count
    if not _enabled:
        return True, ""
    proposed = max(0.0, float(risk_gbp))
    max_heat = _max_same_direction_risk_gbp()
    with _lock:
        _maybe_auto_reset()
        d = str(direction or "").upper()
        if d == "BUY":
            if _buy_count >= MAX_NEW_PER_DIRECTION:
                return (
                    False,
                    f"correlation guard: {_buy_count} BUY entries this session "
                    f"(max {MAX_NEW_PER_DIRECTION})",
                )
            if max_heat > 0 and proposed > 0:
                if _buy_risk_gbp + proposed > max_heat:
                    return (
                        False,
                        f"correlation guard: BUY £{_buy_risk_gbp:.0f}+£{proposed:.0f} "
                        f"> £{max_heat:.0f} same-direction cap",
                    )
            _buy_count += 1
        elif d == "SELL":
            if _sell_count >= MAX_NEW_PER_DIRECTION:
                return (
                    False,
                    f"correlation guard: {_sell_count} SELL entries this session "
                    f"(max {MAX_NEW_PER_DIRECTION})",
                )
            if max_heat > 0 and proposed > 0:
                if _sell_risk_gbp + proposed > max_heat:
                    return (
                        False,
                        f"correlation guard: SELL £{_sell_risk_gbp:.0f}+£{proposed:.0f} "
                        f"> £{max_heat:.0f} same-direction cap",
                    )
            _sell_count += 1
        _persist_state()
        return True, ""


def undo(direction: str) -> None:
    """Release one slot when an order is rejected after check_and_record was called."""
    global _buy_count, _sell_count
    with _lock:
        d = str(direction or "").upper()
        if d == "BUY":
            _buy_count = max(0, _buy_count - 1)
        elif d == "SELL":
            _sell_count = max(0, _sell_count - 1)
        _persist_state()


def snapshot() -> dict[str, object]:
    with _lock:
        return {
            "buy": _buy_count,
            "sell": _sell_count,
            "buy_risk_gbp": round(_buy_risk_gbp, 2),
            "sell_risk_gbp": round(_sell_risk_gbp, 2),
            "max": MAX_NEW_PER_DIRECTION,
            "max_same_direction_risk_gbp": _max_same_direction_risk_gbp(),
            "session": _session_key,
        }
