"""
Volatility-adaptive risk engine — Kelly-inspired sizing, ATR compression, circuit breakers.

Level 1: 2% intraday drawdown → 15-minute trading halt
Level 2: 4% drawdown → hard kill (flatten, cancel, lock until reset)
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any

from system.engine_log import log_engine
from system.paths import data_dir

_L1_DRAWDOWN_PCT = 2.0
_L2_DRAWDOWN_PCT = 4.0
_L1_HALT_SEC = 15 * 60
_STATE_FILE = data_dir() / "state" / "volatility_risk_engine.json"
_lock = threading.RLock()

_snapshot: dict[str, Any] = {
    "ok": True,
    "circuit_breaker_level": 0,
    "halt_until_ts": 0.0,
    "intraday_drawdown_pct": 0.0,
    "peak_equity_gbp": 0.0,
    "current_equity_gbp": 0.0,
    "ts": 0.0,
}


@dataclass
class VolatilityRiskResult:
    approved: bool
    size: float
    stop_distance: float
    limit_distance: float
    size_factor: float
    reason: str = ""


def _load_state() -> dict[str, Any]:
    if not _STATE_FILE.is_file():
        return {}
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


def _account_equity_gbp(store: Any | None) -> tuple[float, float]:
    """Return (current_equity, daily_pnl)."""
    current = 0.0
    daily_pnl = 0.0
    try:
        if store is not None:
            daily_pnl = float(getattr(store, "realized_daily_pnl_gbp", lambda: 0)())
    except Exception:
        pass
    try:
        from api.v31_telemetry import build_v31_telemetry_snapshot

        telem = build_v31_telemetry_snapshot()
        bal = float(telem.get("account_balance_gbp") or 0)
        if bal > 0:
            current = bal
    except Exception:
        pass
    if current <= 0 and store is not None:
        try:
            current = float(getattr(store, "get_account_balance", lambda: 0)() or 0)
        except Exception:
            pass
    return current, daily_pnl


def update_drawdown_state(*, store: Any | None = None) -> dict[str, Any]:
    """Refresh peak equity and circuit breaker levels — background safe."""
    now = time.time()
    state = _load_state()
    current, _ = _account_equity_gbp(store)
    peak = float(state.get("peak_equity_gbp") or 0)
    if current > 0 and (peak <= 0 or current > peak):
        peak = current
        state["peak_equity_gbp"] = peak
    drawdown_pct = 0.0
    if peak > 0 and current > 0:
        drawdown_pct = max(0.0, (peak - current) / peak * 100.0)

    level = int(state.get("circuit_breaker_level") or 0)
    halt_until = float(state.get("halt_until_ts") or 0)

    if drawdown_pct >= _L2_DRAWDOWN_PCT and level < 2:
        level = 2
        state["circuit_breaker_level"] = 2
        state["tripped_at"] = now
        log_engine(f"VOL_RISK L2 HARD KILL: drawdown {drawdown_pct:.2f}%")
        try:
            from system.alert_reporting_matrix import notify_circuit_breaker_trip

            notify_circuit_breaker_trip(level=2, drawdown_pct=drawdown_pct)
        except Exception:
            pass
        _trigger_hard_kill(reason=f"drawdown_{drawdown_pct:.1f}pct")
    elif drawdown_pct >= _L1_DRAWDOWN_PCT and level < 1:
        level = 1
        halt_until = now + _L1_HALT_SEC
        state["circuit_breaker_level"] = 1
        state["halt_until_ts"] = halt_until
        log_engine(f"VOL_RISK L1 HALT: drawdown {drawdown_pct:.2f}% for {_L1_HALT_SEC}s")
        try:
            from system.alert_reporting_matrix import notify_circuit_breaker_trip

            notify_circuit_breaker_trip(level=1, drawdown_pct=drawdown_pct)
        except Exception:
            pass

    if level == 1 and halt_until > 0 and now >= halt_until:
        level = 0
        state["circuit_breaker_level"] = 0
        state["halt_until_ts"] = 0.0
        log_engine("VOL_RISK L1 halt expired — trading resumed")

    state["intraday_drawdown_pct"] = round(drawdown_pct, 3)
    state["current_equity_gbp"] = round(current, 2)
    state["updated_at"] = now
    _save_state(state)

    snap = {
        "ok": level < 2,
        "circuit_breaker_level": level,
        "halt_until_ts": halt_until,
        "intraday_drawdown_pct": round(drawdown_pct, 3),
        "peak_equity_gbp": round(peak, 2),
        "current_equity_gbp": round(current, 2),
        "l1_threshold_pct": _L1_DRAWDOWN_PCT,
        "l2_threshold_pct": _L2_DRAWDOWN_PCT,
        "ts": now,
    }
    with _lock:
        _snapshot.clear()
        _snapshot.update(snap)
    return snap


def _trigger_hard_kill(*, reason: str) -> None:
    try:
        from runtime.strategy_kill_switch import trip_master_strategy_kill_switch

        trip_master_strategy_kill_switch(deal_id="", reason=reason, notify=True)
    except Exception as exc:
        log_engine(f"vol_risk: hard kill trip failed: {type(exc).__name__}: {exc}")
    try:
        from cockpit.emergency import execute_emergency_cockpit_override

        threading.Thread(
            target=execute_emergency_cockpit_override,
            name="vol-risk-l2-flatten",
            daemon=True,
        ).start()
    except Exception:
        pass


def circuit_breaker_blocks_entry() -> tuple[bool, str]:
    state = _load_state()
    level = int(state.get("circuit_breaker_level") or 0)
    now = time.time()
    if level >= 2:
        return True, "circuit_breaker_l2_hard_kill"
    if level >= 1:
        halt_until = float(state.get("halt_until_ts") or 0)
        if now < halt_until:
            remain = int(halt_until - now)
            return True, f"circuit_breaker_l1_halt_{remain}s"
    return False, ""


def apply_volatility_risk(
    *,
    epic: str,
    size: float,
    stop_distance: float,
    limit_distance: float,
    store: Any | None = None,
) -> VolatilityRiskResult:
    """Adjust sizing/stops by regime + ATR expansion; enforce circuit breakers."""
    blocked, reason = circuit_breaker_blocks_entry()
    if blocked:
        return VolatilityRiskResult(
            approved=False,
            size=size,
            stop_distance=stop_distance,
            limit_distance=limit_distance,
            size_factor=0.0,
            reason=reason,
        )

    size_factor = 1.0
    stop_f = 1.0
    limit_f = 1.0
    try:
        from runtime.regime_switch_engine import get_regime_gate, regime_allows_entry

        allowed, reg_reason = regime_allows_entry(epic)
        if not allowed:
            return VolatilityRiskResult(
                approved=False,
                size=size,
                stop_distance=stop_distance,
                limit_distance=limit_distance,
                size_factor=0.0,
                reason=reg_reason,
            )
        gate = get_regime_gate(epic)
        size_factor = float(gate.get("size_factor") or 1.0)
        stop_f = float(gate.get("stop_factor") or 1.0)
        limit_f = float(gate.get("limit_factor") or 1.0)
        try:
            from runtime.regime_switch_engine import get_regime_switch_snapshot

            regime_state = 2
            for row in get_regime_switch_snapshot().get("markets") or []:
                if row.get("epic") == epic:
                    regime_state = int(row.get("state") or 2)
                    break
            from runtime.parameter_tuner import get_trailing_sensitivity_for_regime

            trail = get_trailing_sensitivity_for_regime(regime_state)
            if trail is not None:
                stop_f *= float(trail)
        except Exception:
            pass
    except Exception:
        pass

    atr_ratio = 1.0
    try:
        from ai.strategy.performance_reviewer import active_14_bar_atr

        atr_now = active_14_bar_atr(epic)
        if atr_now and atr_now > 0:
            from trading.ohlc_cache_paths import ohlc_cache_path

            path = ohlc_cache_path(epic)
            if path.is_file():
                lines = path.read_text(encoding="utf-8").strip().splitlines()[-80:]
                closes = []
                for line in lines:
                    try:
                        obj = json.loads(line)
                        c = float(obj.get("close") or obj.get("c") or 0)
                        if c > 0:
                            closes.append(c)
                    except Exception:
                        continue
                if len(closes) >= 20:
                    import pandas as pd
                    from signals.indicators import atr

                    df = pd.DataFrame(
                        {
                            "high": closes,
                            "low": closes,
                            "close": closes,
                        }
                    )
                    atr_s = atr(df, period=14)
                    atr_long = float(atr_s.iloc[-min(40, len(atr_s)) :].mean()) if len(atr_s) else atr_now
                    if atr_long > 0:
                        atr_ratio = atr_now / atr_long
    except Exception:
        pass

    # Kelly-inspired compression when vol expands beyond norms
    if atr_ratio > 1.25:
        compression = max(0.45, 1.0 - (atr_ratio - 1.0) * 0.35)
        size_factor *= compression
    elif atr_ratio < 0.75:
        size_factor *= min(1.15, 1.0 + (0.75 - atr_ratio) * 0.2)

    try:
        from analytics.tuning_params import get_tuning_params

        tp = get_tuning_params().get("params") or {}
        base_risk = float(tp.get("risk_per_trade_gbp") or 40.0)
        trail_sens = float(tp.get("trailing_sensitivity") or 1.0)
        dyn_scale = float(tp.get("dynamic_limit_scale") or 1.0)
        stop_f *= trail_sens
        limit_f *= dyn_scale
        _ = base_risk
    except Exception:
        pass

    adj_size = max(0.01, size * size_factor)
    adj_stop = max(1.0, stop_distance * stop_f)
    adj_limit = max(1.0, limit_distance * limit_f)

    return VolatilityRiskResult(
        approved=True,
        size=adj_size,
        stop_distance=adj_stop,
        limit_distance=adj_limit,
        size_factor=size_factor,
        reason="ok",
    )


def get_volatility_risk_snapshot() -> dict[str, Any]:
    with _lock:
        if _snapshot.get("ts", 0) > 0:
            return dict(_snapshot)
    return update_drawdown_state()


def reset_volatility_risk_for_tests() -> None:
    with _lock:
        _snapshot.clear()
        _snapshot.update({"ok": True, "circuit_breaker_level": 0, "ts": 0.0})
    try:
        if _STATE_FILE.is_file():
            _STATE_FILE.unlink()
    except Exception:
        pass


def start_volatility_risk_refresher(*, store: Any | None = None) -> None:
    def _loop() -> None:
        while True:
            try:
                update_drawdown_state(store=store)
            except Exception:
                pass
            time.sleep(2.0)

    threading.Thread(target=_loop, name="vol-risk-refresher", daemon=True).start()
