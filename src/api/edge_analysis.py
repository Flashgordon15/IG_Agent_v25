"""
Edge analysis — read-only trade statistics for dashboard STATS tab.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any

from signals.indicators import session_name
from system.closed_trades_display import is_excluded_display_row
from system.learning_trade_policy import is_agent_learning_row

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
    from system.config_loader import get_config

    return get_edge_analysis(
        db_path=resolve_learning_db_path(), cfg=get_config(), force=force
    )


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


def _stats_filter_config(cfg: Any | None) -> tuple[int, str, date, date]:
    """Return (lookback_days, exclude_before_iso, range_start, range_end)."""
    from system.config_loader import get_config

    active = cfg or get_config()
    lookback = max(1, int(active.get("stats_lookback_days", 30)))
    exclude_raw = str(active.get("stats_exclude_pre_fix_date") or "").strip()
    range_end = date.today()
    lookback_start = range_end - timedelta(days=lookback)
    if exclude_raw:
        try:
            exclude_day = datetime.fromisoformat(exclude_raw).date()
            range_start = max(lookback_start, exclude_day)
            exclude_before = exclude_raw
        except ValueError:
            range_start = lookback_start
            exclude_before = ""
    else:
        range_start = lookback_start
        exclude_before = ""
    return lookback, exclude_before, range_start, range_end


def _parse_dt(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
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


def _fetch_live_trades(
    conn: sqlite3.Connection, *, cfg: Any | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    lookback, exclude_before, range_start, range_end = _stats_filter_config(cfg)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
    if "ig_pnl_currency" not in cols:
        meta = {
            "from": range_start.isoformat(),
            "to": range_end.isoformat(),
            "exclude_before": exclude_before or None,
            "lookback_days": lookback,
        }
        return [], meta
    start_ts = f"{range_start.isoformat()} 00:00:00"
    end_ts = f"{range_end.isoformat()} 23:59:59"
    rows = conn.execute(
        """
        SELECT *
        FROM trades
        WHERE closed_at IS NOT NULL
          AND ig_pnl_currency IS NOT NULL
          AND dry_run = 0
          AND closed_at >= ?
          AND closed_at <= ?
        ORDER BY closed_at ASC
        """,
        (start_ts, end_ts),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        if is_excluded_display_row(d):
            continue
        if not is_agent_learning_row(d):
            continue
        out.append(d)
    meta = {
        "from": range_start.isoformat(),
        "to": range_end.isoformat(),
        "exclude_before": exclude_before or None,
        "lookback_days": lookback,
    }
    return out, meta


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


def _ml_readiness(cfg: Any | None, *, live_trade_count: int = 0) -> dict[str, Any]:
    """ML readiness from ml_training_store.jsonl — not SQLite trade count."""
    from data.ml_training_store import MLTrainingStore
    from ml.interim_scorer import ml_clean_start_date, ml_min_rows_for_model

    from system.config_loader import get_config

    active = cfg or get_config()
    needed = ml_min_rows_for_model(active)
    store = MLTrainingStore()
    total_ml = int(store.record_count())
    clean_start = ml_clean_start_date(active)
    if clean_start:
        clean_count = int(store.record_count_since(clean_start))
    else:
        clean_count = live_trade_count
    interim = clean_count < needed
    pct = min(100, int(round((clean_count / needed) * 100))) if needed > 0 else 0
    remaining = max(0, needed - clean_count)
    est = None
    if clean_count < needed and clean_count > 0:
        est = (date.today() + timedelta(days=max(7, remaining * 2))).isoformat()
    elif clean_count >= needed:
        est = date.today().isoformat()
    scorer_label = (
        f"Interim Scorer: ACTIVE ({clean_count}/{needed} clean trades)"
        if interim
        else "ML Model: ACTIVE"
    )
    return {
        "confirmed_live_trades": clean_count,
        "ml_training_store_rows": total_ml,
        "clean_trades_since_fix": clean_count,
        "trades_needed_for_ml": needed,
        "percentage_ready": pct,
        "estimated_ready_date": est,
        "scorer_mode": "interim" if interim else "ml_model",
        "scorer_label": scorer_label,
    }


def compute_edge_analysis(
    db_path: str, *, cfg: Any | None = None
) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows, date_range = _fetch_live_trades(conn, cfg=cfg)
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
        "date_range": date_range,
        "overall": overall,
        "by_instrument": by_instrument,
        "by_session": by_session,
        "by_hour_bst": by_hour_bst,
        "ml_readiness": _ml_readiness(cfg, live_trade_count=total),
    }


def get_edge_analysis(
    *, db_path: str, cfg: Any | None = None, force: bool = False
) -> dict[str, Any]:
    global _CACHE_AT, _CACHE_PAYLOAD
    now = time.monotonic()
    with _CACHE_LOCK:
        if (
            not force
            and _CACHE_PAYLOAD is not None
            and (now - _CACHE_AT) < _CACHE_TTL_SEC
        ):
            return dict(_CACHE_PAYLOAD)
    payload = compute_edge_analysis(db_path, cfg=cfg)
    with _CACHE_LOCK:
        _CACHE_PAYLOAD = payload
        _CACHE_AT = now
    return payload


def reset_edge_analysis_cache_for_tests() -> None:
    global _CACHE_AT, _CACHE_PAYLOAD
    with _CACHE_LOCK:
        _CACHE_AT = 0.0
        _CACHE_PAYLOAD = None
