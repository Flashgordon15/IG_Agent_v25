"""Authoritative trade ledger — triage_v30.db fills + latency metrics."""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from analytics.triage_db import connect_triage_sqlite
from system.paths import triage_db_path


def fetch_triage_ledger(*, limit: int = 50) -> dict[str, Any]:
    path = triage_db_path()
    rows: list[dict[str, Any]] = []
    stats: dict[str, Any] = {}
    if not path.is_file():
        return {"rows": [], "stats": stats, "source": str(path)}

    try:
        conn = connect_triage_sqlite(path)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT timestamp, epic, event_type, slip_distance_points,
                   spread_penalty_points, processing_latency_us, session_window
            FROM latency_metrics
            WHERE event_type = 'slippage_fill'
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 200)),),
        )
        for r in cur.fetchall():
            lat_us = float(r["processing_latency_us"] or 0)
            rows.append(
                {
                    "ts": float(r["timestamp"] or time.time()),
                    "epic": str(r["epic"] or "—"),
                    "action": "FILL",
                    "size": 1,
                    "entry": 0.0,
                    "latency_ms": round(lat_us / 1000.0, 1),
                    "slip_pts": float(r["slip_distance_points"] or 0),
                    "spread_premium_pts": float(r["spread_penalty_points"] or 0),
                    "session_window": str(r["session_window"] or ""),
                }
            )
        agg = conn.execute(
            """
            SELECT
                COUNT(*) AS fills,
                AVG(slip_distance_points) AS avg_slip,
                AVG(spread_penalty_points) AS avg_spread,
                AVG(processing_latency_us) AS avg_lat_us
            FROM latency_metrics
            WHERE event_type = 'slippage_fill'
            """
        ).fetchone()
        sess = conn.execute(
            "SELECT value FROM triage_meta WHERE key = 'session_performance' LIMIT 1"
        ).fetchone()
        conn.close()
        if agg:
            stats = {
                "fills": int(agg["fills"] or 0),
                "avg_slip_pts": round(float(agg["avg_slip"] or 0), 3),
                "avg_spread_premium_pts": round(float(agg["avg_spread"] or 0), 3),
                "avg_latency_ms": round(float(agg["avg_lat_us"] or 0) / 1000.0, 1),
            }
        if sess and sess["value"]:
            import json

            stats["session"] = json.loads(str(sess["value"]))
    except Exception as exc:
        return {"rows": rows, "stats": stats, "error": str(exc), "source": str(path)}

    return {"rows": rows, "stats": stats, "source": str(path)}


def fetch_triage_stats() -> dict[str, Any]:
    """Rolling Sharpe, slippage, spread premium for STATS tab."""
    ledger = fetch_triage_ledger(limit=1)
    stats = dict(ledger.get("stats") or {})
    session = stats.pop("session", {}) or {}
    sharpe = float(session.get("sharpe_ratio") or 0.0)
    wave = []
    try:
        path = triage_db_path()
        if path.is_file():
            conn = connect_triage_sqlite(path)
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """
                SELECT slip_distance_points, spread_penalty_points, timestamp
                FROM latency_metrics
                WHERE event_type = 'slippage_fill'
                ORDER BY id DESC
                LIMIT 24
                """
            )
            wave = [
                {
                    "slip": float(r["slip_distance_points"] or 0),
                    "spread": float(r["spread_penalty_points"] or 0),
                    "ts": float(r["timestamp"] or 0),
                }
                for r in reversed(cur.fetchall())
            ]
            conn.close()
    except Exception:
        pass
    return {
        "sharpe_ratio": sharpe,
        "expectancy_gbp": float(session.get("expectancy_gbp") or 0.0),
        "trade_count": int(session.get("trade_count") or 0),
        "avg_slip_pts": stats.get("avg_slip_pts", 0),
        "avg_spread_premium_pts": stats.get("avg_spread_premium_pts", 0),
        "wave": wave,
        "ml_readiness_pct": min(100, int((stats.get("fills") or 0) * 4)),
    }
