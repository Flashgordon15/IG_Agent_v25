"""
Trade quality metrics — acceptance, rejection, slippage, risk vs P&L.

Background refresh (~2s); HTTP returns cached snapshot only.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_REFRESH_SEC = 2.0
_lock = threading.Lock()
_snapshot: dict[str, Any] = {
    "ok": True,
    "acceptance_rate": None,
    "orders_accepted": 0,
    "orders_rejected": 0,
    "rejections_recent": [],
    "slippage": {},
    "trailing_events": 0,
    "dynamic_limit_events": 0,
    "risk_vs_pnl": {},
    "ts": 0.0,
}
_refresher_thread: threading.Thread | None = None
_refresher_stop = threading.Event()


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _triage_db_path() -> Path:
    import os

    raw = os.environ.get("IG_TRIAGE_DB", "").strip()
    if raw:
        return Path(raw).resolve()
    return (Path(__file__).resolve().parent / "triage_v31.db").resolve()


# The slippage aggregate touches latency_metrics (10M+ rows of tick telemetry).
# Slippage only changes when a fill lands, so poll SQLite once a minute and
# serve the cached value on the 2s snapshot refresh — the previous per-refresh
# scan kept a core pinned on disk reads and starved the network threads.
_SLIPPAGE_QUERY_INTERVAL_SEC = 60.0
_slippage_cache: dict[str, Any] = {"avg_points": None, "samples": 0, "max_points": None}
_slippage_cache_ts = 0.0


def _slippage_from_triage() -> dict[str, Any]:
    global _slippage_cache, _slippage_cache_ts
    now = time.monotonic()
    if _slippage_cache_ts and (now - _slippage_cache_ts) < _SLIPPAGE_QUERY_INTERVAL_SEC:
        return dict(_slippage_cache)
    _slippage_cache_ts = now
    path = _triage_db_path()
    if not path.is_file():
        return dict(_slippage_cache)
    try:
        from analytics.triage_db import connect_triage_sqlite_readonly

        conn = connect_triage_sqlite_readonly(path)
        try:
            row = conn.execute(
                """
                SELECT AVG(slip_distance_points), MAX(slip_distance_points), COUNT(*)
                FROM latency_metrics
                WHERE event_type IN ('fill', 'broker_fill', 'execution')
                  AND timestamp > ?
                """,
                (time.time() - 86400.0,),
            ).fetchone()
            if not row or not row[2]:
                _slippage_cache = {"avg_points": None, "samples": 0, "max_points": None}
            else:
                _slippage_cache = {
                    "avg_points": round(float(row[0] or 0), 3),
                    "max_points": round(float(row[1] or 0), 3),
                    "samples": int(row[2]),
                }
            return dict(_slippage_cache)
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return dict(_slippage_cache)


def _lifecycle_event_counts() -> tuple[int, int]:
    trailing = 0
    dynamic = 0
    try:
        from runtime.trade_lifecycle import get_trade_events

        for ev in get_trade_events(limit=100):
            if not isinstance(ev, dict):
                continue
            state = str(ev.get("state") or ev.get("to_state") or "").upper()
            if "TRAILING" in state:
                trailing += 1
            if "DYNAMIC_LIMIT" in state:
                dynamic += 1
    except Exception:
        pass
    try:
        from system.trade_lifecycle_bus import get_lifecycle_bus

        for ev in get_lifecycle_bus().snapshot().get("recent") or []:
            if not isinstance(ev, dict):
                continue
            stage = str(ev.get("stage") or "").upper()
            if "TRAIL" in stage:
                trailing += 1
            if "DYNAMIC" in stage or "LIMIT" in stage:
                dynamic += 1
    except Exception:
        pass
    return trailing, dynamic


def _risk_vs_pnl() -> dict[str, Any]:
    out: dict[str, Any] = {
        "daily_pnl_gbp": None,
        "max_daily_loss_gbp": None,
        "risk_utilization_pct": None,
        "open_exposure_gbp": None,
    }
    try:
        from system.config_loader import get_config

        cfg = get_config().as_dict()
        cap = float(
            cfg.get("max_daily_risk_loss")
            or cfg.get("max_daily_loss_gbp")
            or cfg.get("execution", {}).get("max_daily_risk_loss")
            or 0
        )
        out["max_daily_loss_gbp"] = cap if cap > 0 else None
    except Exception:
        pass
    try:
        from system.daily_loss_policy import effective_daily_pnl
        from system.config_loader import get_config
        from data.learning_store import LearningStore

        store = LearningStore(str(get_config().learning_db))
        daily = float(effective_daily_pnl(store))
        out["daily_pnl_gbp"] = round(daily, 2)
        cap = out.get("max_daily_loss_gbp")
        if cap and cap > 0:
            out["risk_utilization_pct"] = round(min(100.0, abs(daily) / cap * 100.0), 1)
    except Exception:
        pass
    try:
        from api.v31_telemetry import _resolve_position_sync

        sync = _resolve_position_sync()
        if sync is not None:
            total = 0.0
            for p in getattr(sync, "positions", None) or []:
                upl = float(
                    getattr(p, "upl", None)
                    or (p.get("upl") if isinstance(p, dict) else 0)
                    or 0
                )
                total += upl
            out["open_exposure_gbp"] = round(total, 2)
    except Exception:
        pass
    return out


def _refresh_snapshot() -> None:
    rejections: list[dict[str, Any]] = []
    accepted = 0
    rejected = 0
    try:
        from system.unified_runtime_state import get_rejections

        rejections = get_rejections(limit=20)
        rejected = len(rejections)
    except Exception:
        pass
    try:
        from runtime.trade_lifecycle import snapshot as lc_snap

        lc = lc_snap()
        accepted = len(lc.get("active") or {})
        for row in (lc.get("history") or [])[-30:]:
            if not isinstance(row, dict):
                continue
            st = str(row.get("state") or "").upper()
            if st in ("ORDER_ACCEPTED", "ACTIVE", "EXIT_FILLED"):
                accepted += 1
            elif st == "REJECTED":
                rejected += 1
    except Exception:
        pass

    total = accepted + rejected
    rate = round(accepted / total, 3) if total > 0 else None
    trailing, dynamic = _lifecycle_event_counts()
    guard: dict[str, Any] = {}
    try:
        from runtime.broker_reject_guard import broker_reject_guard_status

        guard = broker_reject_guard_status()
    except Exception:
        pass

    body: dict[str, Any] = {
        "ok": True,
        "acceptance_rate": rate,
        "orders_accepted": accepted,
        "orders_rejected": rejected,
        "rejections_recent": rejections[:10],
        "broker_reject_guard": guard,
        "slippage": _slippage_from_triage(),
        "trailing_events": trailing,
        "dynamic_limit_events": dynamic,
        "risk_vs_pnl": _risk_vs_pnl(),
        "ts": time.time(),
        "heartbeat_ts": _utc_now_iso(),
    }
    with _lock:
        _snapshot.clear()
        _snapshot.update(body)


def get_trade_quality_snapshot() -> dict[str, Any]:
    with _lock:
        body = dict(_snapshot)
        body["rejections_recent"] = list(_snapshot.get("rejections_recent") or [])
        body["slippage"] = dict(_snapshot.get("slippage") or {})
        body["risk_vs_pnl"] = dict(_snapshot.get("risk_vs_pnl") or {})
        body["broker_reject_guard"] = dict(_snapshot.get("broker_reject_guard") or {})
        return body


def _refresher_loop() -> None:
    while not _refresher_stop.is_set():
        try:
            _refresh_snapshot()
        except Exception:
            pass
        _refresher_stop.wait(_REFRESH_SEC)


def start_trade_quality_refresher() -> None:
    global _refresher_thread
    if _refresher_thread is not None and _refresher_thread.is_alive():
        return
    _refresher_stop.clear()
    _refresher_thread = threading.Thread(
        target=_refresher_loop,
        name="trade-quality-refresher",
        daemon=True,
    )
    _refresher_thread.start()


def stop_trade_quality_refresher() -> None:
    _refresher_stop.set()


def reset_trade_quality_for_tests() -> None:
    global _refresher_thread
    stop_trade_quality_refresher()
    _refresher_thread = None
    with _lock:
        _snapshot.clear()
        _snapshot.update({"ok": True, "orders_accepted": 0, "orders_rejected": 0})
