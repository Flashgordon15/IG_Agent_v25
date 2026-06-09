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
from typing import Any

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
_DEFAULT_US_INDEX_EPICS = frozenset(
    {
        "IX.D.DOW.IFM.IP",
        "IX.D.NASDAQ.IFM.IP",
        "IX.D.SP500.IFM.IP",
    }
)


def _correlation_guard_config() -> dict[str, Any]:
    try:
        from system.config_loader import get_config

        cfg = get_config()
        raw = getattr(cfg, "correlation_guard", None)
        if isinstance(raw, dict):
            return raw
        if hasattr(cfg, "get"):
            block = cfg.get("correlation_guard")
            if isinstance(block, dict):
                return block
    except Exception:
        pass
    try:
        from system.v26_config import load_v26_overlay

        block = load_v26_overlay().get("correlation_guard") or {}
        if isinstance(block, dict):
            return block
    except Exception:
        pass
    return {}


def _us_index_epics() -> frozenset[str]:
    cfg = _correlation_guard_config()
    raw = cfg.get("us_index_epics")
    if isinstance(raw, list) and raw:
        return frozenset(str(x).strip() for x in raw if str(x).strip())
    try:
        from system.v26_config import load_v26_overlay

        regime = load_v26_overlay().get("regime") or {}
        idx = regime.get("index_epics")
        if isinstance(idx, list) and idx:
            return frozenset(str(x).strip() for x in idx if str(x).strip())
    except Exception:
        pass
    return _DEFAULT_US_INDEX_EPICS


def _max_open_positions_global() -> int:
    cfg = _correlation_guard_config()
    try:
        cap = int(cfg.get("max_open_positions_global") or 0)
        if cap > 0:
            return cap
    except (TypeError, ValueError):
        pass
    try:
        from system.config_loader import get_config

        return max(1, int(get_config().max_open_positions))
    except Exception:
        return 2


def _max_concurrent_us_index_shorts() -> int:
    cfg = _correlation_guard_config()
    try:
        return max(0, int(cfg.get("max_concurrent_us_index_shorts") or 1))
    except (TypeError, ValueError):
        return 1


def _position_epic(row: dict[str, Any]) -> str:
    return str(row.get("epic") or row.get("instrument_epic") or "").strip()


def _position_side(row: dict[str, Any]) -> str:
    side = str(row.get("side") or row.get("direction") or "").upper()
    if side in ("BUY", "SELL"):
        return side
    try:
        size = float(row.get("size") or row.get("deal_size") or 0)
        if size > 0:
            return "BUY"
        if size < 0:
            return "SELL"
    except (TypeError, ValueError):
        pass
    return ""


def check_open_book_limits(
    epic: str,
    direction: str,
    open_positions: list[dict[str, Any]] | None,
) -> tuple[bool, str]:
    """
    Block new entries when global open book or US-index short stack is at cap.
    """
    if not _enabled:
        return True, ""
    direction_u = str(direction or "").upper()
    if direction_u not in ("BUY", "SELL"):
        return True, ""

    positions = [p for p in (open_positions or []) if isinstance(p, dict)]
    epic_s = str(epic or "").strip()
    open_on_epic = any(_position_epic(p) == epic_s for p in positions)
    open_total = len(positions)
    max_global = _max_open_positions_global()

    if not open_on_epic and open_total >= max_global:
        return (
            False,
            f"correlation guard: global open book {open_total} >= max {max_global}",
        )

    us_epics = _us_index_epics()
    max_us_short = _max_concurrent_us_index_shorts()
    if direction_u == "SELL" and epic_s in us_epics and max_us_short > 0:
        us_shorts = sum(
            1
            for p in positions
            if _position_epic(p) in us_epics and _position_side(p) == "SELL"
        )
        if not open_on_epic and us_shorts >= max_us_short:
            return (
                False,
                f"correlation guard: US index shorts {us_shorts} >= max {max_us_short}",
            )

    return True, ""


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
            "max_open_positions_global": _max_open_positions_global(),
            "max_concurrent_us_index_shorts": _max_concurrent_us_index_shorts(),
            "us_index_epics": sorted(_us_index_epics()),
            "session": _session_key,
        }
