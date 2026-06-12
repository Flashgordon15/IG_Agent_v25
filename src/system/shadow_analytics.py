"""
Shadow vs live learning-plane analytics — compare IG-import shadow registry to agent trades.

Metrics: win rate, profit factor, average drawdown (GBP). Exposed via learning_health and /api/health.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from system.learning_trade_policy import agent_trades_sql_clause

_PLANE_LABELS = ("shadow_training_registry", "agent_sourced")

_metrics_cache: dict[str, Any] | None = None
_metrics_cache_ts: float = 0.0
_METRICS_CACHE_TTL_SEC = 60.0


def _pnl_value(row: sqlite3.Row | dict[str, Any]) -> float:
    if isinstance(row, sqlite3.Row):
        ig = row["ig_pnl_currency"] if "ig_pnl_currency" in row.keys() else None
        pts = row["pnl_points"] if "pnl_points" in row.keys() else None
    else:
        ig = row.get("ig_pnl_currency")
        pts = row.get("pnl_points")
    if ig is not None:
        try:
            return float(ig)
        except (TypeError, ValueError):
            pass
    if pts is not None:
        try:
            return float(pts)
        except (TypeError, ValueError):
            pass
    return 0.0


def _drawdown_stats(pnls: list[float]) -> dict[str, float]:
    if not pnls:
        return {"max_drawdown_gbp": 0.0, "average_drawdown_gbp": 0.0}
    equity = 0.0
    peak = 0.0
    drawdowns: list[float] = []
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        dd = peak - equity
        if dd > 0:
            drawdowns.append(dd)
    max_dd = max(drawdowns) if drawdowns else 0.0
    avg_dd = sum(drawdowns) / len(drawdowns) if drawdowns else 0.0
    return {
        "max_drawdown_gbp": round(max_dd, 2),
        "average_drawdown_gbp": round(avg_dd, 2),
    }


def compute_plane_metrics(
    pnls: list[float],
    *,
    wins: int | None = None,
    losses: int | None = None,
) -> dict[str, Any]:
    """Aggregate win rate, profit factor, and drawdown from ordered PnL series."""
    n = len(pnls)
    if n == 0:
        return {
            "trade_count": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "gross_profit_gbp": 0.0,
            "gross_loss_gbp": 0.0,
            "net_pnl_gbp": 0.0,
            "max_drawdown_gbp": 0.0,
            "average_drawdown_gbp": 0.0,
        }

    win_pnls = [p for p in pnls if p > 0]
    loss_pnls = [p for p in pnls if p < 0]
    w = wins if wins is not None else len(win_pnls)
    lo = losses if losses is not None else len(loss_pnls)
    gross_profit = sum(win_pnls)
    gross_loss = abs(sum(loss_pnls))
    if gross_loss > 0:
        profit_factor = round(gross_profit / gross_loss, 4)
    elif gross_profit > 0:
        profit_factor = None  # no losses — unbounded PF
    else:
        profit_factor = 0.0

    dd = _drawdown_stats(pnls)
    return {
        "trade_count": n,
        "wins": w,
        "losses": lo,
        "win_rate": round(w / n, 4) if n else 0.0,
        "profit_factor": profit_factor,
        "gross_profit_gbp": round(gross_profit, 2),
        "gross_loss_gbp": round(gross_loss, 2),
        "net_pnl_gbp": round(sum(pnls), 2),
        **dd,
    }


def _fetch_shadow_pnls(conn: sqlite3.Connection) -> list[float]:
    rows = conn.execute(
        """
        SELECT ig_pnl_currency, pnl_points
        FROM shadow_training_registry
        WHERE is_shadow = 1 AND closed_at IS NOT NULL
        ORDER BY closed_at ASC, id ASC
        """
    ).fetchall()
    return [_pnl_value(r) for r in rows]


def _fetch_agent_pnls(conn: sqlite3.Connection) -> list[float]:
    clause = agent_trades_sql_clause()
    rows = conn.execute(
        f"""
        SELECT ig_pnl_currency, pnl_points, result
        FROM trades
        WHERE closed_at IS NOT NULL AND {clause}
        ORDER BY closed_at ASC, id ASC
        """
    ).fetchall()
    return [_pnl_value(r) for r in rows]


def build_shadow_vs_live_comparison(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """
    Compare shadow_training_registry (IG imports) vs agent-sourced live trades.
    Safe for GUI / API — no credentials.
    """
    close_conn = False
    if conn is None:
        from data.learning_store import LearningStore
        from system.paths import data_dir

        store = LearningStore(data_dir() / "learning_db.sqlite3")
        store.connect()
        conn = store.conn
        close_conn = True

    try:
        shadow_pnls = _fetch_shadow_pnls(conn)
        agent_pnls = _fetch_agent_pnls(conn)
        shadow = compute_plane_metrics(shadow_pnls)
        live = compute_plane_metrics(agent_pnls)
        shadow["label"] = "shadow_training_registry"
        shadow["is_shadow"] = True
        live["label"] = "agent_sourced"
        live["is_shadow"] = False

        def _delta(key: str) -> float | None:
            sv = shadow.get(key)
            lv = live.get(key)
            if sv is None or lv is None:
                return None
            if isinstance(sv, (int, float)) and isinstance(lv, (int, float)):
                if key == "profit_factor" and (sv is None or lv is None):
                    return None
                try:
                    return round(float(lv) - float(sv), 4)
                except (TypeError, ValueError):
                    return None
            return None

        return {
            "shadow": shadow,
            "live": live,
            "comparison": {
                "win_rate_delta_live_minus_shadow": _delta("win_rate"),
                "profit_factor_delta_live_minus_shadow": _delta("profit_factor"),
                "net_pnl_delta_live_minus_shadow_gbp": _delta("net_pnl_gbp"),
                "average_drawdown_delta_live_minus_shadow_gbp": _delta(
                    "average_drawdown_gbp"
                ),
            },
        }
    finally:
        if close_conn:
            try:
                conn.close()
            except Exception:
                pass


def shadow_vs_live_metrics(*, force: bool = False) -> dict[str, Any]:
    """
    Normalized payload for dashboard metrics.shadow_vs_live.

    Keys: shadow, live (each with win_rate, profit_factor, average_drawdown_gbp, …).
    """
    global _metrics_cache, _metrics_cache_ts
    now = time.time()
    if (
        not force
        and _metrics_cache is not None
        and (now - _metrics_cache_ts) < _METRICS_CACHE_TTL_SEC
    ):
        return dict(_metrics_cache)
    try:
        report = build_shadow_vs_live_comparison()
        payload = {
            "ok": True,
            "shadow": report.get("shadow") or {},
            "live": report.get("live") or {},
            "comparison": report.get("comparison") or {},
        }
    except Exception as exc:
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    _metrics_cache = dict(payload)
    _metrics_cache_ts = now
    return dict(payload)


def reset_shadow_analytics_cache_for_tests() -> None:
    global _metrics_cache, _metrics_cache_ts
    _metrics_cache = None
    _metrics_cache_ts = 0.0


def system_status_snapshot() -> dict[str, Any]:
    """Compact block for /api/health system_status."""
    try:
        metrics = shadow_vs_live_metrics()
        if not metrics.get("ok"):
            return metrics
        return {
            "ok": True,
            "shadow_training_registry": metrics.get("shadow") or {},
            "agent_sourced": metrics.get("live") or {},
            "comparison": metrics.get("comparison") or {},
            "shadow_vs_live": metrics,
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
