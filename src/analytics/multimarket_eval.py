"""
Multi-market evaluation snapshot — per-epic health, lifecycle, P&L, feed state.

Background thread refreshes every ~1.5s; HTTP handlers return O(1) dict copy.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

from system.market_data_hub import NIGHT_MATRIX_EPICS, get_market_data_hub

_REFRESH_SEC = 1.5
_lock = threading.Lock()
_snapshot: dict[str, Any] = {
    "ok": True,
    "markets": [],
    "active_stack": [],
    "ts": 0.0,
    "heartbeat_ts": "",
}
_refresher_thread: threading.Thread | None = None
_refresher_stop = threading.Event()

_MARKET_LABELS: dict[str, str] = {
    "CS.D.CFPGOLD.CFP.IP": "Gold",
    "IX.D.DOW.IFM.IP": "Wall St",
    "IX.D.NIKKEI.IFM.IP": "Nikkei",
    "CS.D.EURUSD.CFD.IP": "EUR/USD",
    "CS.D.CRUDE.CFD.IP": "Crude",
    "IX.D.FTSE.IFM.IP": "FTSE",
    "IX.D.DAX.IFM.IP": "DAX",
}


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _shadow_signal_counts() -> dict[str, int]:
    counts: Counter[str] = Counter()
    try:
        from system.paths import data_dir

        path = data_dir() / "shadow_log.jsonl"
        if not path.is_file():
            return {}
        cutoff = time.time() - 3600.0
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            for line in fh:
                try:
                    row = json.loads(line.decode("utf-8", errors="ignore"))
                except Exception:
                    continue
                ts = float(row.get("ts") or row.get("timestamp") or 0)
                if ts > 0 and ts < cutoff:
                    continue
                epic = str(row.get("epic") or "").strip()
                if epic:
                    counts[epic] += 1
    except Exception:
        pass
    return dict(counts)


def _lifecycle_by_epic() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    try:
        from runtime.trade_lifecycle import snapshot as lifecycle_snapshot

        lc = lifecycle_snapshot()
        for deal_id, trade in (lc.get("active") or {}).items():
            if not isinstance(trade, dict):
                continue
            epic = str(trade.get("epic") or "").strip()
            if not epic:
                continue
            row = out.setdefault(
                epic,
                {"open_trades": 0, "states": [], "orders_submitted": 0},
            )
            row["open_trades"] += 1
            state = str(trade.get("state") or trade.get("lifecycle_state") or "")
            if state:
                row["states"].append(state)
    except Exception:
        pass
    try:
        from system.config_loader import get_config
        from data.learning_store import LearningStore
        from runtime.active_lifecycle_trades import list_active_lifecycle_trades

        store = LearningStore(str(get_config().learning_db))
        for row in list_active_lifecycle_trades(store):
            epic = str(row.get("epic") or "").strip()
            if not epic:
                continue
            entry = out.setdefault(
                epic,
                {"open_trades": 0, "states": [], "orders_submitted": 0},
            )
            entry["broker_managed"] = entry.get("broker_managed", 0) + 1
            entry["lifecycle_state"] = str(row.get("lifecycle_state") or "")
            entry["broker_upl"] = float(row.get("broker_upl") or 0)
    except Exception:
        pass
    return out


def _stops_limits_by_epic() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    try:
        from runtime.virtual_stop_loss import virtual_stop_snapshot

        for row in virtual_stop_snapshot().get("positions") or []:
            if not isinstance(row, dict):
                continue
            epic = str(row.get("epic") or "").strip()
            if epic:
                out[epic] = {"stop": row.get("stop"), "trailing": row.get("trailing_active")}
    except Exception:
        pass
    try:
        from runtime.dynamic_limit_engine import snapshot as dyn_snap

        for row in dyn_snap().get("positions") or []:
            if not isinstance(row, dict):
                continue
            epic = str(row.get("epic") or "").strip()
            if epic:
                out.setdefault(epic, {})["limit"] = row.get("limit")
    except Exception:
        pass
    return out


def _pnl_by_epic() -> dict[str, float]:
    out: dict[str, float] = {}
    try:
        from api.v31_telemetry import _resolve_position_sync

        sync = _resolve_position_sync()
        if sync is None:
            return out
        positions = getattr(sync, "positions", None) or []
        for p in positions:
            epic = str(getattr(p, "epic", None) or (p.get("epic") if isinstance(p, dict) else "") or "")
            if not epic:
                continue
            upl = float(
                getattr(p, "upl", None)
                or (p.get("upl") if isinstance(p, dict) else 0)
                or (p.get("pnl_gbp") if isinstance(p, dict) else 0)
                or 0
            )
            out[epic] = out.get(epic, 0.0) + upl
    except Exception:
        pass
    return out


def _regime_display_label(epic: str, z: float) -> str:
    """Prefer regime_switch_engine state; Z-score label is display fallback only."""
    try:
        from runtime.regime_switch_engine import get_regime_switch_snapshot

        for row in get_regime_switch_snapshot().get("markets") or []:
            if row.get("epic") == epic:
                return str(row.get("state_label") or "unknown")
    except Exception:
        pass
    z_abs = abs(float(z))
    if z_abs >= 2.45:
        return "expansion"
    if z_abs <= 2.0:
        return "compressed"
    return "neutral"


def _refresh_snapshot() -> None:
    signals = _shadow_signal_counts()
    lifecycle = _lifecycle_by_epic()
    stops = _stops_limits_by_epic()
    pnl_map = _pnl_by_epic()
    hub = get_market_data_hub()

    feed_per_epic: dict[str, str] = {}
    try:
        from system.feeds.data_feed_orchestrator import get_data_feed_state

        feed_state = get_data_feed_state()
        for row in feed_state.get("epics") or []:
            if isinstance(row, dict):
                epic = str(row.get("epic") or "")
                if epic:
                    feed_per_epic[epic] = str(row.get("health") or feed_state.get("health") or "unknown")
    except Exception:
        feed_state = {}

    rotation_reasons: dict[str, str] = {}
    active_stack_epics: set[str] = set()
    try:
        from runtime.dual_core_execution import get_rotation_state

        rot = get_rotation_state()
        active_stack_epics = {str(e) for e in (rot.get("active_stack_epics") or []) if e}
        for row in rot.get("active_instruments") or []:
            if isinstance(row, dict) and row.get("epic"):
                active_stack_epics.add(str(row["epic"]))
        for row in (
            list(rot.get("active_instruments") or [])
            + list(rot.get("eligible_instruments") or [])
            + list(rot.get("inactive_instruments") or [])
        ):
            if isinstance(row, dict) and row.get("epic"):
                rotation_reasons[str(row["epic"])] = str(row.get("reason") or "")
    except Exception:
        rot = {}

    markets: list[dict[str, Any]] = []
    for epic in NIGHT_MATRIX_EPICS:
        quote = hub.get_snapshot(epic)
        age = float(quote.age_seconds()) if quote is not None else 999.0
        z = 0.0
        tpm = 0
        try:
            from runtime.dual_core_execution import _ticks_per_minute, epic_display_name

            tpm = int(_ticks_per_minute(epic))
            label = epic_display_name(epic)
        except Exception:
            label = _MARKET_LABELS.get(epic, epic)

        try:
            from runtime.dual_core_execution import _snapshots

            snap = _snapshots.get(epic)
            if snap is not None:
                z = float(snap.live_calculated_zscore or snap.volatility_z_score or 0)
        except Exception:
            pass

        lc = lifecycle.get(epic, {})
        sl = stops.get(epic, {})
        feed_h = feed_per_epic.get(epic)
        if not feed_h:
            if quote is None or age > 120:
                feed_h = "offline"
            elif age > 45:
                feed_h = "degraded"
            else:
                feed_h = "ok"

        markets.append(
            {
                "epic": epic,
                "label": label,
                "signals_1h": int(signals.get(epic, 0)),
                "orders_open": int(lc.get("open_trades") or lc.get("broker_managed") or 0),
                "lifecycle_state": lc.get("lifecycle_state") or (
                    lc.get("states", [""])[0] if lc.get("states") else "idle"
                ),
                "stops": sl.get("stop"),
                "limits": sl.get("limit"),
                "trailing_active": bool(sl.get("trailing")),
                "pnl_open_gbp": round(float(pnl_map.get(epic, lc.get("broker_upl", 0)) or 0), 2),
                "vol_regime": _regime_display_label(epic, z),
                "z_score": round(z, 4),
                "ticks_per_minute": tpm,
                "feed_health": feed_h,
                "quote_age_sec": round(age, 1) if age < 900 else None,
                "rotation_reason": rotation_reasons.get(epic, ""),
                "in_active_stack": epic in active_stack_epics,
            }
        )

    body: dict[str, Any] = {
        "ok": True,
        "markets": markets,
        "active_stack": [
            str(e) for e in (rot.get("active_stack_epics") or []) if e
        ],
        "feed_summary": {
            "health": str((feed_state or {}).get("health") or "unknown"),
            "primary_feed": str((feed_state or {}).get("primary_feed") or ""),
            "fresh_count": int((feed_state or {}).get("fresh_count") or 0),
            "total_epics": int((feed_state or {}).get("total_epics") or len(NIGHT_MATRIX_EPICS)),
        },
        "ts": time.time(),
        "heartbeat_ts": _utc_now_iso(),
    }
    with _lock:
        _snapshot.clear()
        _snapshot.update(body)


def get_multimarket_eval_snapshot() -> dict[str, Any]:
    """O(1) copy for HTTP — no external I/O."""
    with _lock:
        body = dict(_snapshot)
        body["markets"] = [dict(m) for m in (_snapshot.get("markets") or [])]
        body["active_stack"] = list(_snapshot.get("active_stack") or [])
        return body


def _refresher_loop() -> None:
    while not _refresher_stop.is_set():
        try:
            _refresh_snapshot()
        except Exception:
            pass
        _refresher_stop.wait(_REFRESH_SEC)


def start_multimarket_eval_refresher() -> None:
    global _refresher_thread
    if _refresher_thread is not None and _refresher_thread.is_alive():
        return
    _refresher_stop.clear()
    _refresher_thread = threading.Thread(
        target=_refresher_loop,
        name="multimarket-eval-refresher",
        daemon=True,
    )
    _refresher_thread.start()


def stop_multimarket_eval_refresher() -> None:
    _refresher_stop.set()


def reset_multimarket_eval_for_tests() -> None:
    global _refresher_thread
    stop_multimarket_eval_refresher()
    _refresher_thread = None
    with _lock:
        _snapshot.clear()
        _snapshot.update({"ok": True, "markets": [], "active_stack": [], "ts": 0.0})
