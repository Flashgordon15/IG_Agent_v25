"""
Edge analysis — read-only trade statistics for dashboard STATS tab.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import date, timedelta
from typing import Any

from signals.indicators import session_name
from system.closed_trades_display import is_excluded_display_row

_CACHE_LOCK = threading.Lock()
_CACHE_AT: float = 0.0
_CACHE_PAYLOAD: dict[str, Any] | None = None
_CACHE_TTL_SEC = 60.0


def resolve_learning_db_path() -> str:
    """Production learning DB path — matches dashboard_data / agent_bootstrap."""
    from system.config_loader import get_config

    return str(get_config().learning_db)


def get_edge_analysis_payload(*, force: bool = False) -> dict[str, Any]:
    """API entry point — read-only stats without instantiating LearningStore."""
    return get_edge_analysis(db_path=resolve_learning_db_path(), force=force)


def _patch_learning_store_default_db_path() -> None:
    """routes.py calls LearningStore() without db_path; default from config."""
    import data.learning_store as mod

    if getattr(mod, "_EDGE_ANALYSIS_DB_PATCHED", False):
        return

    _original = mod.LearningStore.__init__

    def __init__(self, db_path: str | None = None) -> None:
        _original(self, db_path if db_path is not None else resolve_learning_db_path())

    mod.LearningStore.__init__ = __init__  # type: ignore[method-assign]
    mod._EDGE_ANALYSIS_DB_PATCHED = True


_patch_learning_store_default_db_path()


def _parse_dt(text: str | None) -> Any:
    if not text:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(text).replace("Z", ""))
    except ValueError:
        return None


def _trade_pnl_gbp(row: dict[str, Any]) -> float:
    for key in ("ig_pnl_currency", "pnl_gbp", "pnl"):
        if row.get(key) is not None:
            try:
                return float(row[key])
            except (TypeError, ValueError):
                continue
    try:
        return float(row.get("pnl_points") or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_win(row: dict[str, Any]) -> bool:
    result = str(row.get("result") or "").upper()
    if result == "WIN":
        return True
    if result == "LOSS":
        return False
    return _trade_pnl_gbp(row) > 0


def _fetch_live_trades(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
    if "ig_pnl_currency" not in cols:
        return []
    rows = conn.execute(
        """
        SELECT *
        FROM trades
        WHERE closed_at IS NOT NULL
          AND ig_pnl_currency IS NOT NULL
          AND dry_run = 0
        ORDER BY closed_at ASC
        """
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        if is_excluded_display_row(d):
            continue
        out.append(d)
    return out


def _profit_factor(pnls: list[float]) -> float | None:
    wins = sum(p for p in pnls if p > 0)
    losses = abs(sum(p for p in pnls if p < 0))
    if losses <= 0:
        return None if wins <= 0 else round(wins / 1.0, 2)
    return round(wins / losses, 2)


def _win_rate(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    wins = sum(1 for r in rows if _is_win(r))
    return round(wins / len(rows), 4)


def _duration_minutes(row: dict[str, Any]) -> float | None:
    opened = _parse_dt(str(row.get("opened_at") or ""))
    closed = _parse_dt(str(row.get("closed_at") or ""))
    if opened is None or closed is None:
        return None
    return max(0.0, (closed - opened).total_seconds() / 60.0)


def _rr_achieved(row: dict[str, Any]) -> float | None:
    try:
        entry = float(row.get("entry") or 0)
        exit_px = float(row.get("exit") or 0)
        stop = float(row.get("stop") or 0)
        if entry <= 0 or stop <= 0:
            return None
        risk = abs(entry - stop)
        if risk <= 0:
            return None
        side = str(row.get("side") or "BUY").upper()
        move = exit_px - entry if side == "BUY" else entry - exit_px
        return round(move / risk, 2)
    except (TypeError, ValueError):
        return None


def _rr_theoretical(row: dict[str, Any]) -> float | None:
    try:
        entry = float(row.get("entry") or 0)
        target = float(row.get("target") or 0)
        stop = float(row.get("stop") or 0)
        risk = abs(entry - stop)
        if risk <= 0 or target <= 0:
            return None
        side = str(row.get("side") or "BUY").upper()
        reward = target - entry if side == "BUY" else entry - target
        return round(reward / risk, 2)
    except (TypeError, ValueError):
        return None


def _display_name(epic: str, market: str) -> str:
    if market:
        return str(market)
    try:
        from trading.open_position_view import epic_market_label

        return epic_market_label(epic)
    except Exception:
        return epic


def _ml_readiness(count: int, *, needed: int = 50) -> dict[str, Any]:
    pct = min(100, int(round((count / needed) * 100))) if needed > 0 else 0
    remaining = max(0, needed - count)
    est = None
    if count < needed and count > 0:
        est = (date.today() + timedelta(days=max(7, remaining * 2))).isoformat()
    elif count >= needed:
        est = date.today().isoformat()
    return {
        "confirmed_live_trades": count,
        "trades_needed_for_ml": needed,
        "percentage_ready": pct,
        "estimated_ready_date": est,
    }


def compute_edge_analysis(db_path: str) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = _fetch_live_trades(conn)
    finally:
        conn.close()

    pnls = [_trade_pnl_gbp(r) for r in rows]
    total = len(rows)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    rr_achieved = [x for x in (_rr_achieved(r) for r in rows) if x is not None]
    rr_theo = [x for x in (_rr_theoretical(r) for r in rows) if x is not None]
    streak = 0
    max_streak = 0
    for row in rows:
        if _is_win(row):
            streak = 0
        else:
            streak += 1
            max_streak = max(max_streak, streak)

    overall = {
        "total_trades": total,
        "win_rate": _win_rate(rows),
        "profit_factor": _profit_factor(pnls),
        "average_rr_achieved": round(sum(rr_achieved) / len(rr_achieved), 2)
        if rr_achieved
        else None,
        "average_rr_theoretical": round(sum(rr_theo) / len(rr_theo), 2)
        if rr_theo
        else None,
        "largest_win": round(max(wins), 2) if wins else 0.0,
        "largest_loss": round(min(losses), 2) if losses else 0.0,
        "max_consecutive_losses": max_streak,
        "expectancy_per_trade_gbp": round(sum(pnls) / total, 2) if total else 0.0,
    }

    by_epic: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        epic = str(row.get("epic") or "unknown")
        by_epic.setdefault(epic, []).append(row)

    by_instrument = []
    for epic, group in sorted(by_epic.items()):
        g_pnls = [_trade_pnl_gbp(r) for r in group]
        durations = [d for d in (_duration_minutes(r) for r in group) if d is not None]
        by_instrument.append(
            {
                "epic": epic,
                "display_name": _display_name(epic, str(group[-1].get("market") or "")),
                "trades": len(group),
                "win_rate": _win_rate(group),
                "profit_factor": _profit_factor(g_pnls),
                "net_pnl_gbp": round(sum(g_pnls), 2),
                "avg_duration_minutes": round(sum(durations) / len(durations), 1)
                if durations
                else None,
            }
        )

    by_session_map: dict[str, list[dict[str, Any]]] = {}
    by_hour_map: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        opened = _parse_dt(str(row.get("opened_at") or ""))
        sess = session_name(opened) if opened else "unknown"
        by_session_map.setdefault(sess, []).append(row)
        if opened is not None:
            by_hour_map.setdefault(opened.hour, []).append(row)

    by_session = []
    for sess, group in sorted(by_session_map.items()):
        g_pnls = [_trade_pnl_gbp(r) for r in group]
        by_session.append(
            {
                "session": sess,
                "trades": len(group),
                "win_rate": _win_rate(group),
                "profit_factor": _profit_factor(g_pnls),
                "net_pnl_gbp": round(sum(g_pnls), 2),
            }
        )

    by_hour_bst = []
    for hour in sorted(by_hour_map):
        group = by_hour_map[hour]
        g_pnls = [_trade_pnl_gbp(r) for r in group]
        by_hour_bst.append(
            {
                "hour": hour,
                "trades": len(group),
                "win_rate": _win_rate(group),
                "net_pnl_gbp": round(sum(g_pnls), 2),
            }
        )

    return {
        "overall": overall,
        "by_instrument": by_instrument,
        "by_session": by_session,
        "by_hour_bst": by_hour_bst,
        "ml_readiness": _ml_readiness(total),
    }


def get_edge_analysis(*, db_path: str, force: bool = False) -> dict[str, Any]:
    global _CACHE_AT, _CACHE_PAYLOAD
    now = time.monotonic()
    with _CACHE_LOCK:
        if (
            not force
            and _CACHE_PAYLOAD is not None
            and (now - _CACHE_AT) < _CACHE_TTL_SEC
        ):
            return dict(_CACHE_PAYLOAD)
    payload = compute_edge_analysis(db_path)
    with _CACHE_LOCK:
        _CACHE_PAYLOAD = payload
        _CACHE_AT = now
    return payload


def reset_edge_analysis_cache_for_tests() -> None:
    global _CACHE_AT, _CACHE_PAYLOAD
    with _CACHE_LOCK:
        _CACHE_AT = 0.0
        _CACHE_PAYLOAD = None
