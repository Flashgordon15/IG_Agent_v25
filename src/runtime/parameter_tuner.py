"""
Autonomous parameter tuner — regime-aware edge optimization from triage closed trades.

Targets: >= 70% win rate per active Markov regime; £1,000/day net expectation (pro-rata).
Never modifies volatility_risk_engine circuit breaker thresholds (L1 2% / L2 4%).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.engine_log import log_engine

WIN_RATE_TARGET = 0.70
DAILY_PNL_TARGET_GBP = 1000.0
MIN_TRADES_FOR_TUNING = 3
_EVAL_INTERVAL_SEC = 3600.0
_HISTORY_MAX = 50
SLIPPAGE_ROLLING_TRADES = 5
SLIPPAGE_THRESHOLD_PIPS = 0.5
SLIPPAGE_TP_EXPANSION_MULT = 1.35

REGIME_LABELS = {
    0: "mean_reversion",
    1: "hv_trend",
    2: "chop",
}

# Tunable multiplier bounds — hard caps (immutable safety envelope)
_REGIME_BOUNDS: dict[str, tuple[float, float]] = {
    "size_factor": (0.25, 1.25),
    "stop_factor": (0.50, 1.50),
    "limit_factor": (0.50, 1.50),
    "trailing_sensitivity": (0.30, 1.50),
}

# Default baselines aligned with regime_switch_engine _STRATEGY_GATES
_DEFAULT_REGIME_MATRIX: dict[str, dict[str, float]] = {
    "0": {
        "size_factor": 0.85,
        "stop_factor": 0.90,
        "limit_factor": 0.85,
        "profit_target_multiplier": 0.85,
        "trailing_sensitivity": 1.0,
    },
    "1": {
        "size_factor": 1.10,
        "stop_factor": 1.25,
        "limit_factor": 1.35,
        "profit_target_multiplier": 1.35,
        "trailing_sensitivity": 1.0,
    },
    "2": {
        "size_factor": 0.50,
        "stop_factor": 0.75,
        "limit_factor": 0.70,
        "profit_target_multiplier": 0.70,
        "trailing_sensitivity": 1.0,
    },
}

_FORBIDDEN_TUNER_KEYS = frozenset(
    {
        "max_daily_loss_gbp",
        "l1_drawdown_pct",
        "l2_drawdown_pct",
        "circuit_breaker_level",
        "rest_api_budget",
        "iron_cage_override",
    }
)

_lock = threading.RLock()
_snapshot: dict[str, Any] = {
    "ok": True,
    "healthy": True,
    "regime_matrix": dict(_DEFAULT_REGIME_MATRIX),
    "regime_metrics": {},
    "target_deltas": {},
    "optimization_history": [],
    "daily_pnl_gbp": 0.0,
    "daily_pnl_target_gbp": DAILY_PNL_TARGET_GBP,
    "win_rate_target": WIN_RATE_TARGET,
    "last_run_ts": 0.0,
    "last_run_reason": "init",
    "ts": 0.0,
}
_daemon_thread: threading.Thread | None = None
_daemon_stop = threading.Event()
_regime_map: dict[str, int] = {}


@dataclass
class RegimeMetrics:
    regime_state: int
    trades: int = 0
    wins: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    total_pnl_gbp: float = 0.0
    avg_drawdown_gbp: float = 0.0
    avg_slippage_pts: float = 0.0
    gross_wins: float = 0.0
    gross_losses: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "regime_state": self.regime_state,
            "regime_label": REGIME_LABELS.get(self.regime_state, "unknown"),
            "trades": self.trades,
            "wins": self.wins,
            "win_rate": round(self.win_rate, 4),
            "profit_factor": round(self.profit_factor, 3),
            "total_pnl_gbp": round(self.total_pnl_gbp, 2),
            "avg_drawdown_gbp": round(self.avg_drawdown_gbp, 2),
            "avg_slippage_pts": round(self.avg_slippage_pts, 3),
        }


def _overlay_path() -> Path:
    env = os.environ.get("IG_TUNING_OVERLAY", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "config" / "tuning_overlay.json"


def _read_overlay() -> dict[str, Any]:
    path = _overlay_path()
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def ensure_tuning_overlay_or_default() -> dict[str, Any]:
    """
    Boot STAGE_1 fallback — serialize default regime matrix when overlay missing/corrupt.
    Never raises; returns status dict for orchestrator telemetry.
    """
    path = _overlay_path()
    body = _read_overlay()
    matrix = body.get("regime_matrix") if isinstance(body.get("regime_matrix"), dict) else None
    if matrix and all(str(k) in _DEFAULT_REGIME_MATRIX for k in matrix):
        return {"ok": True, "created": False, "path": str(path), "regime_matrix": matrix}
    fallback = {
        "regime_matrix": dict(_DEFAULT_REGIME_MATRIX),
        "params": {},
        "tuner_updated_at": time.time(),
        "boot_fallback": True,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(fallback, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        log_engine(f"ParameterTuner: wrote boot fallback overlay → {path}")
        return {"ok": True, "created": True, "path": str(path), "regime_matrix": fallback["regime_matrix"]}
    except Exception as exc:
        log_engine(f"ParameterTuner: overlay fallback in-memory only — {type(exc).__name__}: {exc}")
        with _lock:
            _snapshot["regime_matrix"] = dict(_DEFAULT_REGIME_MATRIX)
        return {
            "ok": True,
            "created": False,
            "path": str(path),
            "in_memory": True,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _write_overlay_patch(**sections: Any) -> None:
    path = _overlay_path()
    with _lock:
        body = _read_overlay()
        for key, val in sections.items():
            body[key] = val
        body["tuner_updated_at"] = time.time()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(body, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)


def _clamp_param(key: str, value: float) -> float:
    lo, hi = _REGIME_BOUNDS.get(key, (0.0, 999.0))
    return max(lo, min(hi, float(value)))


def _clamp_matrix(matrix: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for state_key, row in matrix.items():
        cleaned: dict[str, float] = {}
        for k, v in row.items():
            if k in _REGIME_BOUNDS:
                cleaned[k] = round(_clamp_param(k, float(v)), 4)
        out[str(state_key)] = cleaned
    return out


def get_regime_matrix() -> dict[str, dict[str, float]]:
    """O(1) cached matrix merged with overlay defaults."""
    overlay = _read_overlay()
    block = overlay.get("regime_matrix")
    merged = {k: dict(v) for k, v in _DEFAULT_REGIME_MATRIX.items()}
    if isinstance(block, dict):
        for state_key, row in block.items():
            if isinstance(row, dict):
                base = merged.setdefault(str(state_key), {})
                for k, v in row.items():
                    if k in _REGIME_BOUNDS:
                        base[k] = float(v)
    return _clamp_matrix(merged)


def merge_tuned_gate(gate: dict[str, Any], regime_state: int) -> None:
    """Apply tuned multipliers onto strategy gate dict (in-place)."""
    row = get_regime_matrix().get(str(regime_state)) or {}
    for key in ("size_factor", "stop_factor", "limit_factor"):
        if key in row:
            gate[key] = float(row[key])


def get_trailing_sensitivity_for_regime(regime_state: int) -> float | None:
    row = get_regime_matrix().get(str(regime_state)) or {}
    val = row.get("trailing_sensitivity")
    return float(val) if val is not None else None


def get_profit_target_multiplier(regime_state: int | None = None) -> float:
    """Regime-tuned target scale for GBP exits (from overlay or defaults)."""
    state_key = str(int(regime_state) if regime_state is not None else 0)
    overlay = _read_overlay()
    block = overlay.get("regime_matrix")
    row: dict[str, Any] = dict(_DEFAULT_REGIME_MATRIX.get(state_key) or {})
    if isinstance(block, dict) and isinstance(block.get(state_key), dict):
        row.update(block[state_key])
    val = row.get("profit_target_multiplier")
    if val is None:
        val = row.get("limit_factor", 1.0)
    return max(0.5, min(2.0, float(val)))


def record_trade_regime(*, ticket: str, regime_state: int) -> None:
    """Tag closed trade with Markov state for harvest attribution."""
    if not ticket:
        return
    with _lock:
        _regime_map[str(ticket)] = int(regime_state)
    try:
        overlay = _read_overlay()
        stored = overlay.get("regime_trade_map") or {}
        if not isinstance(stored, dict):
            stored = {}
        stored[str(ticket)] = int(regime_state)
        if len(stored) > 5000:
            for k in list(stored.keys())[:1000]:
                stored.pop(k, None)
        _write_overlay_patch(regime_trade_map=stored)
    except Exception:
        pass


def _triage_db_path() -> Path | None:
    try:
        from analytics.triage_logger import resolve_triage_db_path

        return resolve_triage_db_path()
    except Exception:
        import os

        raw = os.environ.get("IG_TRIAGE_DB", "").strip()
        if raw:
            return Path(raw)
    return None


def _infer_regime_for_epic(epic: str) -> int:
    try:
        from runtime.regime_switch_engine import get_regime_switch_snapshot

        for row in get_regime_switch_snapshot().get("markets") or []:
            if row.get("epic") == epic:
                return int(row.get("state") or 2)
    except Exception:
        pass
    return 2


def _load_regime_map() -> dict[str, int]:
    overlay = _read_overlay()
    stored = overlay.get("regime_trade_map") or {}
    merged = {str(k): int(v) for k, v in stored.items()} if isinstance(stored, dict) else {}
    with _lock:
        merged.update(_regime_map)
    return merged


def harvest_closed_trades(*, since_ts: float | None = None) -> list[dict[str, Any]]:
    """Query triage closed_positions for the evaluation window."""
    if since_ts is None:
        since_ts = time.time() - 86400.0
    since_iso = datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    path = _triage_db_path()
    if path is None or not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        from analytics.triage_db import connect_triage_sqlite_readonly

        conn = connect_triage_sqlite_readonly(path)
        try:
            cur = conn.execute(
                """
                SELECT ticket, epic, asset, net_pnl, gross_pnl, exit_timestamp, direction
                FROM closed_positions
                WHERE exit_timestamp >= ?
                ORDER BY exit_timestamp ASC
                """,
                (since_iso,),
            )
            for ticket, epic, asset, net_pnl, gross_pnl, exit_ts, direction in cur.fetchall():
                rows.append(
                    {
                        "ticket": str(ticket or ""),
                        "epic": str(epic or asset or ""),
                        "net_pnl": float(net_pnl or 0),
                        "gross_pnl": float(gross_pnl or net_pnl or 0),
                        "exit_timestamp": str(exit_ts or ""),
                        "direction": str(direction or ""),
                    }
                )
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return rows
    return rows


def _slippage_by_epic(*, since_ts: float) -> dict[str, float]:
    path = _triage_db_path()
    if path is None or not path.is_file():
        return {}
    out: dict[str, list[float]] = {}
    try:
        from analytics.triage_db import connect_triage_sqlite_readonly

        conn = connect_triage_sqlite_readonly(path)
        try:
            cur = conn.execute(
                """
                SELECT epic, slip_distance_points
                FROM latency_metrics
                WHERE timestamp > ? AND slip_distance_points IS NOT NULL
                """,
                (since_ts,),
            )
            for epic, slip in cur.fetchall():
                ep = str(epic or "")
                if ep:
                    out.setdefault(ep, []).append(float(slip or 0))
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return {}
    return {ep: sum(v) / len(v) for ep, v in out.items() if v}


def _rolling_slippage_by_epic(*, n: int = SLIPPAGE_ROLLING_TRADES) -> dict[str, float]:
    """Rolling last-N execution slippage per epic from latency_metrics."""
    path = _triage_db_path()
    if path is None or not path.is_file():
        return {}
    out: dict[str, float] = {}
    try:
        from analytics.triage_db import connect_triage_sqlite_readonly

        conn = connect_triage_sqlite_readonly(path)
        try:
            cur = conn.execute(
                "SELECT DISTINCT epic FROM latency_metrics WHERE epic IS NOT NULL AND epic != ''"
            )
            epics = [str(row[0]) for row in cur.fetchall() if row and row[0]]
            for epic in epics:
                rows = conn.execute(
                    """
                    SELECT slip_distance_points
                    FROM latency_metrics
                    WHERE epic = ? AND slip_distance_points IS NOT NULL
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (epic, int(max(1, n))),
                ).fetchall()
                samples = [float(r[0] or 0) for r in rows if r]
                if samples:
                    out[epic] = sum(samples) / len(samples)
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return {}
    return out


def compute_slippage_adaptive_offsets() -> dict[str, dict[str, Any]]:
    """Per-epic liquidity cost feedback — widen TP when rolling slippage > 0.5 pips."""
    rolling = _rolling_slippage_by_epic(n=SLIPPAGE_ROLLING_TRADES)
    offsets: dict[str, dict[str, Any]] = {}
    for epic, avg in rolling.items():
        active = avg > SLIPPAGE_THRESHOLD_PIPS
        mult = SLIPPAGE_TP_EXPANSION_MULT if active else 1.0
        offsets[epic] = {
            "avg_slippage_pips": round(avg, 4),
            "limit_factor_mult": mult,
            "profit_target_multiplier": mult,
            "active": active,
        }
    return offsets


def apply_slippage_adaptive_take_profit(
    matrix: dict[str, dict[str, float]],
    offsets: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, float]], list[str]]:
    """Expand limit_factor / profit_target_multiplier for high-slippage epics."""
    reasons: list[str] = []
    if not offsets:
        return matrix, reasons
    for epic, off in offsets.items():
        if not off.get("active"):
            continue
        state = _infer_regime_for_epic(epic)
        key = str(state)
        row = matrix.setdefault(key, dict(_DEFAULT_REGIME_MATRIX.get(key, {})))
        mult = float(off.get("limit_factor_mult") or SLIPPAGE_TP_EXPANSION_MULT)
        row["limit_factor"] = _clamp_param(
            "limit_factor", float(row.get("limit_factor", 1.0)) * mult
        )
        ptm = float(row.get("profit_target_multiplier", row.get("limit_factor", 1.0)))
        row["profit_target_multiplier"] = _clamp_param("limit_factor", ptm * mult)
        reasons.append(f"{epic}:slippage_tp_expansion_{mult:.2f}x")
    return _clamp_matrix(matrix), reasons


def get_slippage_adaptive_offsets() -> dict[str, dict[str, Any]]:
    """O(1) cached offsets when tuner has run; else live compute."""
    with _lock:
        cached = _snapshot.get("slippage_adaptive_offsets")
        if isinstance(cached, dict) and cached:
            return dict(cached)
    return compute_slippage_adaptive_offsets()


def aggregate_metrics_by_regime(
    trades: list[dict[str, Any]],
    *,
    regime_map: dict[str, int] | None = None,
    slippage_by_epic: dict[str, float] | None = None,
) -> dict[int, RegimeMetrics]:
    """Build per-regime performance metrics from closed trade rows."""
    rmap = regime_map or _load_regime_map()
    slip = slippage_by_epic or {}
    buckets: dict[int, RegimeMetrics] = {
        s: RegimeMetrics(regime_state=s) for s in (0, 1, 2)
    }
    running_dd: dict[int, float] = {0: 0.0, 1: 0.0, 2: 0.0}
    peak: dict[int, float] = {0: 0.0, 1: 0.0, 2: 0.0}
    dd_samples: dict[int, list[float]] = {0: [], 1: [], 2: []}
    slip_samples: dict[int, list[float]] = {0: [], 1: [], 2: []}

    for row in trades:
        ticket = str(row.get("ticket") or "")
        epic = str(row.get("epic") or "")
        pnl = float(row.get("net_pnl") or 0)
        state = int(rmap.get(ticket) if ticket in rmap else _infer_regime_for_epic(epic))
        state = max(0, min(2, state))
        m = buckets[state]
        m.trades += 1
        m.total_pnl_gbp += pnl
        if pnl >= 0:
            m.wins += 1
            m.gross_wins += pnl
        else:
            m.gross_losses += abs(pnl)
        running_dd[state] += pnl
        peak[state] = max(peak[state], running_dd[state])
        dd = peak[state] - running_dd[state]
        dd_samples[state].append(dd)
        if epic in slip:
            slip_samples[state].append(slip[epic])

    for state, m in buckets.items():
        if m.trades > 0:
            m.win_rate = m.wins / m.trades
        if m.gross_losses > 0:
            m.profit_factor = m.gross_wins / m.gross_losses
        elif m.gross_wins > 0:
            m.profit_factor = 99.0
        if dd_samples[state]:
            m.avg_drawdown_gbp = sum(dd_samples[state]) / len(dd_samples[state])
        if slip_samples[state]:
            m.avg_slippage_pts = sum(slip_samples[state]) / len(slip_samples[state])
    return buckets


def _proportional_daily_target() -> float:
    """Prorate £1k target by UTC hours elapsed in calendar day."""
    now = datetime.now(timezone.utc)
    hours = now.hour + now.minute / 60.0
    return DAILY_PNL_TARGET_GBP * max(0.05, hours / 24.0)


def compute_regime_adjustments(
    metrics: dict[int, RegimeMetrics],
    *,
    daily_pnl_gbp: float,
    current_matrix: dict[str, dict[str, float]],
) -> tuple[dict[str, dict[str, float]], dict[str, Any], list[str]]:
    """
    Return (new_matrix, target_deltas, reasons).
    Mathematical bounded nudges when win rate or P&L miss targets.
    """
    matrix = {k: dict(v) for k, v in current_matrix.items()}
    deltas: dict[str, Any] = {}
    reasons: list[str] = []
    p_target = _proportional_daily_target()
    deltas["daily_pnl_gbp"] = round(daily_pnl_gbp, 2)
    deltas["proportional_target_gbp"] = round(p_target, 2)
    deltas["win_rate_target"] = WIN_RATE_TARGET

    global_scale = 1.0
    if daily_pnl_gbp < p_target * 0.85 and daily_pnl_gbp < p_target:
        global_scale = 0.97
        reasons.append("daily_pnl_below_proportional_target")

    for state in (0, 1, 2):
        m = metrics[state]
        key = str(state)
        row = matrix.setdefault(key, dict(_DEFAULT_REGIME_MATRIX.get(key, {})))
        if m.trades < MIN_TRADES_FOR_TUNING:
            continue

        label = REGIME_LABELS.get(state, key)
        deltas[label] = m.to_dict()

        if m.win_rate < WIN_RATE_TARGET:
            row["size_factor"] = _clamp_param(
                "size_factor", row.get("size_factor", 1.0) * 0.95 * global_scale
            )
            row["stop_factor"] = _clamp_param(
                "stop_factor", row.get("stop_factor", 1.0) * 0.96
            )
            row["trailing_sensitivity"] = _clamp_param(
                "trailing_sensitivity",
                row.get("trailing_sensitivity", 1.0) * 1.05,
            )
            reasons.append(f"{label}:win_rate_below_70")
            if state == 1:
                row["limit_factor"] = _clamp_param(
                    "limit_factor", row.get("limit_factor", 1.0) * 0.97
                )
            if state == 0 and m.avg_slippage_pts > SLIPPAGE_THRESHOLD_PIPS:
                row["limit_factor"] = _clamp_param(
                    "limit_factor",
                    row.get("limit_factor", 1.0) * SLIPPAGE_TP_EXPANSION_MULT,
                )
                ptm = float(row.get("profit_target_multiplier", row.get("limit_factor", 1.0)))
                row["profit_target_multiplier"] = _clamp_param(
                    "limit_factor", ptm * SLIPPAGE_TP_EXPANSION_MULT
                )
                reasons.append(f"{label}:slippage_widen_limits")
        elif m.win_rate >= WIN_RATE_TARGET + 0.05 and m.profit_factor >= 1.2:
            row["size_factor"] = _clamp_param(
                "size_factor", row.get("size_factor", 1.0) * 1.02
            )
            reasons.append(f"{label}:performance_headroom")

        if m.profit_factor < 1.0 and m.trades >= MIN_TRADES_FOR_TUNING:
            row["stop_factor"] = _clamp_param(
                "stop_factor", row.get("stop_factor", 1.0) * 0.94
            )
            reasons.append(f"{label}:profit_factor_below_1")

    return _clamp_matrix(matrix), deltas, reasons


def run_tuning_cycle(*, force: bool = False) -> dict[str, Any]:
    """Full harvest → evaluate → bounded adjust → persist."""
    global _snapshot
    since_ts = time.time() - 86400.0
    trades = harvest_closed_trades(since_ts=since_ts)
    slip = _slippage_by_epic(since_ts=since_ts)
    metrics = aggregate_metrics_by_regime(trades, slippage_by_epic=slip)
    daily_pnl = sum(float(t.get("net_pnl") or 0) for t in trades)
    current = get_regime_matrix()
    new_matrix, deltas, reasons = compute_regime_adjustments(
        metrics, daily_pnl_gbp=daily_pnl, current_matrix=current
    )
    slip_offsets = compute_slippage_adaptive_offsets()
    new_matrix, slip_reasons = apply_slippage_adaptive_take_profit(new_matrix, slip_offsets)
    reasons.extend(slip_reasons)

    adjusted = new_matrix != current or bool(reasons)
    history_entry = {
        "ts": time.time(),
        "reasons": reasons,
        "daily_pnl_gbp": round(daily_pnl, 2),
        "trades": len(trades),
        "matrix_delta": {
            k: new_matrix.get(k) for k in new_matrix if new_matrix.get(k) != current.get(k)
        },
    }

    if adjusted or force:
        overlay = _read_overlay()
        hist = overlay.get("tuner_history") or []
        if not isinstance(hist, list):
            hist = []
        hist.append(history_entry)
        hist = hist[-_HISTORY_MAX:]
        _write_overlay_patch(regime_matrix=new_matrix, tuner_history=hist)

    body = {
        "ok": True,
        "healthy": True,
        "regime_matrix": new_matrix,
        "regime_metrics": {REGIME_LABELS[k]: v.to_dict() for k, v in metrics.items()},
        "target_deltas": deltas,
        "optimization_history": (_read_overlay().get("tuner_history") or [])[-10:],
        "daily_pnl_gbp": round(daily_pnl, 2),
        "daily_pnl_target_gbp": DAILY_PNL_TARGET_GBP,
        "win_rate_target": WIN_RATE_TARGET,
        "last_run_ts": time.time(),
        "last_run_reason": "; ".join(reasons) if reasons else "stable",
        "trades_evaluated": len(trades),
        "slippage_adaptive_offsets": slip_offsets,
        "ts": time.time(),
    }
    with _lock:
        _snapshot.clear()
        _snapshot.update(body)
    if reasons:
        log_engine(f"parameter_tuner: adjustments — {'; '.join(reasons[:5])}")
    return dict(body)


def get_tuner_state_snapshot() -> dict[str, Any]:
    """O(1) copy for HTTP — refreshes from overlay if never run."""
    with _lock:
        if _snapshot.get("last_run_ts", 0) > 0:
            return dict(_snapshot)
    matrix = get_regime_matrix()
    overlay = _read_overlay()
    return {
        "ok": True,
        "healthy": True,
        "regime_matrix": matrix,
        "regime_metrics": {},
        "target_deltas": {},
        "optimization_history": (overlay.get("tuner_history") or [])[-10:],
        "daily_pnl_gbp": 0.0,
        "daily_pnl_target_gbp": DAILY_PNL_TARGET_GBP,
        "win_rate_target": WIN_RATE_TARGET,
        "last_run_ts": overlay.get("tuner_updated_at", 0),
        "last_run_reason": "not_yet_run",
        "ts": time.time(),
    }


def _daemon_loop() -> None:
    while not _daemon_stop.wait(_EVAL_INTERVAL_SEC):
        try:
            run_tuning_cycle()
        except Exception as exc:
            log_engine(f"parameter_tuner: cycle failed {type(exc).__name__}: {exc}")
            with _lock:
                _snapshot["healthy"] = False
                _snapshot["last_run_reason"] = f"error:{type(exc).__name__}"


def start_parameter_tuner_daemon() -> None:
    global _daemon_thread
    if _daemon_thread is not None and _daemon_thread.is_alive():
        return
    _daemon_stop.clear()
    try:
        run_tuning_cycle(force=True)
    except Exception:
        pass
    _daemon_thread = threading.Thread(
        target=_daemon_loop, name="parameter-tuner", daemon=True
    )
    _daemon_thread.start()


def stop_parameter_tuner_daemon() -> None:
    _daemon_stop.set()


def reset_parameter_tuner_for_tests() -> None:
    global _snapshot, _regime_map, _daemon_thread
    with _lock:
        _regime_map.clear()
        _snapshot.clear()
        _snapshot.update(
            {
                "ok": True,
                "healthy": True,
                "regime_matrix": dict(_DEFAULT_REGIME_MATRIX),
                "regime_metrics": {},
                "target_deltas": {},
                "optimization_history": [],
                "daily_pnl_gbp": 0.0,
                "daily_pnl_target_gbp": DAILY_PNL_TARGET_GBP,
                "win_rate_target": WIN_RATE_TARGET,
                "last_run_ts": 0.0,
                "last_run_reason": "reset",
                "ts": 0.0,
            }
        )
    _daemon_thread = None


def validate_tuner_safety(payload: dict[str, Any]) -> list[str]:
    """Reject any attempt to tune immutable safety keys."""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["payload_not_object"]
    for key in payload:
        if key in _FORBIDDEN_TUNER_KEYS:
            errors.append(f"forbidden:{key}")
    return errors
