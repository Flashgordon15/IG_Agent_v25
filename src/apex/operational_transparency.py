"""
Operational Transparency HUD — live funnel counters, role health, micro-ticker, ML post-mortem.

Thread-safe in-process metrics streamed via snapshot_store → apex_ipc.sock.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from system.engine_log import log_engine

_lock = threading.RLock()
_opportunities_scanned = 0
_spread_rejections = 0
_liquidity_blocks = 0
_ml_veto_flags = 0
_weekend_blackout_holds = 0
_executed_trades = 0
_other_rejections = 0

_last_bridge_handshake_mono = 0.0
_last_bridge_handshake_wall = 0.0
_last_rest_handshake_mono = 0.0
_last_rest_handshake_wall = 0.0

_micro_ticker: deque[dict[str, str]] = deque(maxlen=48)
_ML_CACHE: dict[str, Any] = {}
_ML_CACHE_TS = 0.0
_ML_CACHE_TTL = 45.0
_recovery_active = False


def _utc_hms() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def record_opportunity_scanned(*, epic: str = "") -> None:
    global _opportunities_scanned
    with _lock:
        _opportunities_scanned += 1


def _categorize_gate_rejection(gate_name: str, detail: str) -> str:
    name = str(gate_name or "").lower()
    text = str(detail or "").lower()
    if "spread" in name or "spread" in text:
        return "spread"
    if name in ("atr", "structural_risk", "execution_protect_spread") or "atr" in text:
        return "liquidity"
    if "ml" in name or "ml_veto" in name:
        return "ml_veto"
    if any(
        k in name or k in text
        for k in (
            "session",
            "blackout",
            "weekend",
            "market_hours",
            "rollover",
            "closed",
        )
    ):
        return "weekend"
    return "other"


def record_gate_rejection(gate_name: str, detail: str = "", *, epic: str = "") -> None:
    global _spread_rejections, _liquidity_blocks, _ml_veto_flags
    global _weekend_blackout_holds, _other_rejections
    cat = _categorize_gate_rejection(gate_name, detail)
    with _lock:
        if cat == "spread":
            _spread_rejections += 1
        elif cat == "liquidity":
            _liquidity_blocks += 1
        elif cat == "ml_veto":
            _ml_veto_flags += 1
        elif cat == "weekend":
            _weekend_blackout_holds += 1
        else:
            _other_rejections += 1


def record_executed_trade(*, epic: str = "", side: str = "") -> None:
    global _executed_trades
    with _lock:
        _executed_trades += 1
    append_micro_action(
        f"LiveExecutor dispatched {side or 'ORDER'} on {epic or 'market'} "
        f"via IG DEMO REST gateway"
    )


def set_recovery_active(active: bool) -> None:
    global _recovery_active
    with _lock:
        _recovery_active = bool(active)


def clear_recovery_active() -> None:
    set_recovery_active(False)


def is_recovery_active() -> bool:
    with _lock:
        return bool(_recovery_active)


def record_bridge_handshake(source: str = "ipc") -> None:
    global _last_bridge_handshake_mono, _last_bridge_handshake_wall
    now_mono = time.monotonic()
    now_wall = time.time()
    with _lock:
        _last_bridge_handshake_mono = now_mono
        _last_bridge_handshake_wall = now_wall
    if source == "rest":
        global _last_rest_handshake_mono, _last_rest_handshake_wall
        with _lock:
            _last_rest_handshake_mono = now_mono
            _last_rest_handshake_wall = now_wall


def append_micro_action(message: str) -> None:
    line = str(message or "").strip()
    if not line:
        return
    entry = {"ts_utc": _utc_hms(), "line": line}
    with _lock:
        _micro_ticker.appendleft(entry)
    try:
        from apex.ipc_bridge import broadcast_story_event

        broadcast_story_event({"kind": "micro_action", **entry})
    except Exception:
        pass


def _seconds_since(mono_ts: float) -> float | None:
    if mono_ts <= 0:
        return None
    return max(0.0, time.monotonic() - mono_ts)


def _role_status(alive: bool, *, failed: bool = False) -> str:
    if failed:
        return "failed"
    return "active" if alive else "failed"


def _build_health_grid() -> dict[str, Any]:
    ingest_active = False
    numpy_ready = False
    order_router_active = False
    wal_active = False
    bridge_sec: float | None = None
    rest_sec: float | None = None

    try:
        from apex.microkernel import get_microkernel, is_warmup_complete

        kernel = get_microkernel()
        threads = getattr(kernel, "_threads", []) or []
        ingest_active = bool(threads) and all(t.is_alive() for t in threads[:1])
        numpy_ready = bool(is_warmup_complete())
        order_router_active = len(threads) >= 3 and all(t.is_alive() for t in threads[2:3])
        wal_active = len(threads) >= 4 and threads[-1].is_alive()
    except Exception:
        pass

    try:
        from trading.multi_api_broker import multi_api_broker_running

        if multi_api_broker_running():
            ingest_active = True
    except Exception:
        pass

    try:
        from apex.ipc_bridge import get_ipc_bridge

        stats = get_ipc_bridge().stats()
        if stats.get("clients", 0) > 0:
            bridge_sec = bridge_sec if bridge_sec is not None else 0.0
    except Exception:
        pass

    with _lock:
        bridge_sec = _seconds_since(_last_bridge_handshake_mono)
        rest_sec = _seconds_since(_last_rest_handshake_mono)

    if bridge_sec is None:
        try:
            from system.gate_activity import seconds_since_last_gate_eval

            gate_age = seconds_since_last_gate_eval()
            if gate_age is not None:
                bridge_sec = float(gate_age)
        except Exception:
            pass

    roles = [
        {
            "id": "data_ingestion",
            "label": "Data Ingestion",
            "status": _role_status(ingest_active),
            "detail": "Multi-API broker + Worker A ingest queue",
        },
        {
            "id": "numpy_warmup",
            "label": "NumPy Matrix Warmup",
            "status": "active" if numpy_ready else "failed",
            "detail": "READY" if numpy_ready else "WARMING — 4-worker ring compile",
        },
        {
            "id": "order_router",
            "label": "Order Router",
            "status": _role_status(order_router_active or numpy_ready),
            "detail": "Gate 7 → LiveExecutor DEMO REST",
        },
        {
            "id": "wal_logger",
            "label": "Asynchronous WAL Logger",
            "status": _role_status(wal_active or numpy_ready),
            "detail": "Worker D ledger + learning WAL",
        },
    ]

    bridge_label = "Core Communications Bridge"
    if bridge_sec is not None:
        bridge_detail = f"Connected [{bridge_sec:.1f}s ago]"
    else:
        bridge_detail = "Awaiting first handshake"

    return {
        "roles": roles,
        "bridge_label": bridge_label,
        "bridge_connected_sec_ago": bridge_sec,
        "bridge_detail": bridge_detail,
        "rest_handshake_sec_ago": rest_sec,
        "recovery_active": is_recovery_active(),
        "recovery_badge": "RECOVERY_ACTIVE" if is_recovery_active() else "",
    }


def _volume_regime_score(regime: str) -> float:
    r = str(regime or "").lower()
    if "high" in r or "surge" in r:
        return 85.0
    if "low" in r or "quiet" in r:
        return 25.0
    return 55.0


def _feature_weights_for_loss(row: dict[str, Any]) -> list[dict[str, Any]]:
    rsi = abs(float(row.get("rsi") or 50.0) - 50.0) / 50.0 * 100.0
    atr = min(100.0, float(row.get("atr") or 0.0) * 4.0)
    spread = min(100.0, float(row.get("spread") or 0.0) * 2.5)
    fitness = min(100.0, float(row.get("fitness_score") or 0.0))
    vol = _volume_regime_score(str(row.get("volume_regime") or ""))
    conf = min(100.0, float(row.get("confidence") or 0.0))
    raw = {
        "Volume Momentum": vol,
        "RSI Divergence": rsi,
        "ATR Volatility": atr,
        "Spread Pressure": spread,
        "Trend Alignment": fitness,
        "Sentiment Divergence": max(0.0, 100.0 - conf),
    }
    total = sum(raw.values()) or 1.0
    return [
        {"feature": name, "weight_pct": round(val / total * 100.0, 1)}
        for name, val in sorted(raw.items(), key=lambda x: -x[1])
    ]


def _anomaly_flag(row: dict[str, Any]) -> tuple[bool, str]:
    atr = float(row.get("atr") or 0.0)
    spread = float(row.get("spread") or 0.0)
    exit_reason = str(row.get("exit_reason") or "").lower()
    if atr >= 35.0:
        return True, "Unprecedented ATR spike — outside training volatility band"
    if spread >= 8.0:
        return True, "Extreme spread event — liquidity vacuum"
    if any(k in exit_reason for k in ("gap", "news", "halt", "flash")):
        return True, "Market discontinuity — event outside historical dataset"
    if float(row.get("gbp_pnl") or 0.0) <= -50.0:
        return True, "Tail loss magnitude — exceeds model calibration envelope"
    return False, ""


def _read_ml_records(limit: int = 120) -> list[dict[str, Any]]:
    try:
        from data.ml_training_store import default_store_path

        path = default_store_path()
        if not path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows[-limit:]
    except Exception as exc:
        log_engine(f"operational_transparency ml read failed: {type(exc).__name__}: {exc}")
        return []


def _build_ml_post_mortem() -> dict[str, Any]:
    global _ML_CACHE, _ML_CACHE_TS
    now = time.time()
    with _lock:
        if _ML_CACHE and (now - _ML_CACHE_TS) < _ML_CACHE_TTL:
            return dict(_ML_CACHE)

    rows = _read_ml_records()
    wins = 0
    losses = 0
    losing_trades: list[dict[str, Any]] = []

    for row in rows:
        pnl = float(row.get("gbp_pnl") or row.get("pts_pnl") or 0.0)
        result = str(row.get("result") or "").lower()
        is_loss = pnl < 0 or result in ("loss", "lose", "l")
        is_win = pnl > 0 or result in ("win", "w")
        if is_loss:
            losses += 1
        elif is_win:
            wins += 1

    total = wins + losses
    win_pct = round(wins / total * 100.0, 1) if total else 0.0
    loss_pct = round(losses / total * 100.0, 1) if total else 0.0

    for row in reversed(rows):
        pnl = float(row.get("gbp_pnl") or row.get("pts_pnl") or 0.0)
        result = str(row.get("result") or "").lower()
        if pnl >= 0 and result not in ("loss", "lose", "l"):
            continue
        anomaly, anomaly_reason = _anomaly_flag(row)
        losing_trades.append(
            {
                "deal_id": str(row.get("deal_id") or ""),
                "instrument": str(row.get("instrument") or row.get("setup_name") or "—"),
                "entry_time": str(row.get("entry_time") or ""),
                "exit_time": str(row.get("exit_time") or ""),
                "gbp_pnl": round(pnl, 2),
                "model_confidence_pct": round(float(row.get("confidence") or 0.0), 1),
                "feature_weights": _feature_weights_for_loss(row),
                "anomaly": anomaly,
                "anomaly_reason": anomaly_reason,
            }
        )
        if len(losing_trades) >= 24:
            break

    payload = {
        "wins": wins,
        "losses": losses,
        "win_pct": win_pct,
        "loss_pct": loss_pct,
        "total_closed": total,
        "losing_trades": losing_trades,
    }
    with _lock:
        _ML_CACHE = dict(payload)
        _ML_CACHE_TS = now
    return payload


def build_funnel_snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "opportunities_scanned": _opportunities_scanned,
            "spread_rejections": _spread_rejections,
            "liquidity_blocks": _liquidity_blocks,
            "ml_veto_flags": _ml_veto_flags,
            "weekend_blackout_holds": _weekend_blackout_holds,
            "other_rejections": _other_rejections,
            "executed_trades": _executed_trades,
        }


def build_transparency_snapshot(*, include_ml: bool = True) -> dict[str, Any]:
    with _lock:
        ticker = list(_micro_ticker)
    out: dict[str, Any] = {
        "ts": time.time(),
        "funnel": build_funnel_snapshot(),
        "health_grid": _build_health_grid(),
        "micro_ticker": ticker,
    }
    if include_ml:
        out["ml_post_mortem"] = _build_ml_post_mortem()
    try:
        from apex.system_monitor import build_monitor_snapshot

        out["system_monitor"] = build_monitor_snapshot()
    except Exception:
        pass
    return out


def attach_to_tick(tick: dict[str, Any], *, include_ml: bool = False) -> None:
    """Merge operational transparency into dashboard tick (IPC / WS)."""
    try:
        tick["operational_transparency"] = build_transparency_snapshot(
            include_ml=include_ml
        )
    except Exception as exc:
        log_engine(
            f"operational_transparency attach failed: {type(exc).__name__}: {exc}"
        )


def reset_operational_transparency_for_tests() -> None:
    global \
        _opportunities_scanned, \
        _spread_rejections, \
        _liquidity_blocks, \
        _ml_veto_flags, \
        _weekend_blackout_holds, \
        _executed_trades, \
        _other_rejections, \
        _ML_CACHE, \
        _ML_CACHE_TS
    with _lock:
        _opportunities_scanned = 0
        _spread_rejections = 0
        _liquidity_blocks = 0
        _ml_veto_flags = 0
        _weekend_blackout_holds = 0
        _executed_trades = 0
        _other_rejections = 0
        _micro_ticker.clear()
        _ML_CACHE = {}
        _ML_CACHE_TS = 0.0
