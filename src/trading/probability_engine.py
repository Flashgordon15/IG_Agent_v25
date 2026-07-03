"""
Hierarchical probability engine — deterministic technical filters + Pillar 4 ML brain.

Technical setups crossing 42% ingest a 128-dim state matrix; ML steers promote/veto thresholds.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np

from signals.indicators import STRATEGY_THRESHOLD_LOW_PCT
from signals.signal_engine import SignalResult
from system.engine_log import log_engine

WIN_PROMOTE_FLOOR = 0.65
WIN_VETO_FLOOR_STRICT = 0.55
WIN_VETO_FLOOR_RELAXED = 0.45
WIN_VETO_FLOOR_NEWS_ALPHA = 0.48
WIN_VETO_FLOOR = WIN_VETO_FLOOR_STRICT
WIN_VETO_FLOOR_CAP = 0.65
VETO_FLOOR_EXPANSION_PP = 0.50
VETO_FLOOR_DEFENSIVE_PP = 0.62
PROMOTE_THRESHOLD_RELIEF_PCT = 10.0
_SHADOW_STREAK_MIN_WINS = 3
_SHADOW_STREAK_MIN_WINRATE = 0.70
_FORWARD_WALK_BARS = 48

# Multi-horizon vetting horizons
_HORIZON_5_TICK_BARS = 5
_HORIZON_15M_BARS = 3  # ~15m on 5m bars
_HORIZON_4H_BARS = 48  # 48 × 5m = 4h
_multi_horizon_cache: dict[str, dict[str, Any]] = {}
_FORWARD_WALK_VETO_FLOOR = 0.65
_FORWARD_WALK_VETO_FLOOR_SYNTHETIC = 0.12
_REGIME_STATE_PAYOFF = np.array([0.48, 0.74, 0.36], dtype=np.float64)  # mean_rev, hv_trend, chop

_cognitive_veto_bump = 0.0
_synthetic_alpha_gate_active = False
_feature_baseline_slots: dict[str, np.ndarray] = {}
_ml_route_outcomes: dict[str, deque[bool]] = {}
_ml_route_outcomes_ts: dict[str, deque[tuple[float, bool]]] = {}

# --- Alpha time-decay (limit_chase_hf unfilled) ---
_ALPHA_DECAY_FILL_MS = 1500
_ALPHA_DECAY_STRICT_FLOOR = 0.42
_ALPHA_DECAY_HALF_LIFE_MS = 750.0
_alpha_decay_orders: dict[str, dict[str, Any]] = {}

# --- RLS runtime calibrator (feature drift slots 98-111) ---
_RLS_LAMBDA = 0.995
_RLS_THETA = 0.0
_RLS_P = 1.0
_RLS_WINDOW_SEC = 48 * 3600
_RLS_WIN_RATE_TARGET = 0.70
_rls_last_adjustment: dict[str, Any] = {}
_horizon_sentiment_cache: dict[str, dict[str, Any]] = {}
_news_alpha_veto_relax_active = False


def enable_synthetic_alpha_gate(active: bool = True) -> dict[str, Any]:
    """Relax 48-bar shadow-walk veto floor while synthetic hydration maintains rings."""
    global _synthetic_alpha_gate_active
    _synthetic_alpha_gate_active = bool(active)
    log_engine(
        f"ProbabilityEngine: synthetic alpha gate "
        f"{'ACTIVE' if _synthetic_alpha_gate_active else 'OFF'}"
    )
    return {
        "ok": True,
        "synthetic_alpha_gate_active": _synthetic_alpha_gate_active,
        "shadow_walk_veto_floor": (
            _FORWARD_WALK_VETO_FLOOR_SYNTHETIC
            if _synthetic_alpha_gate_active
            else _FORWARD_WALK_VETO_FLOOR
        ),
    }


def synthetic_alpha_gate_active() -> bool:
    return bool(_synthetic_alpha_gate_active)


@dataclass(frozen=True)
class ProbabilityVerdict:
    win_probability: float
    model_verdict: str
    veto: bool
    promote: bool
    threshold_relief: float
    ml_veto_token: str = ""
    trailing_sensitivity_scale: float = 1.0
    forward_walk_win_prob: float = 0.0
    news_countdown_norm: float = 0.0


def compute_news_trailing_sensitivity(*, epic: str = "", market: str = "") -> float:
    """
    Scale trailing stop-loss sensitivity as a function of upcoming news proximity.

    Returns 1.0 (baseline) up to ~1.85 when high-impact release is imminent.
    """
    key = str(epic or market or "").strip()
    if not key:
        return 1.0
    try:
        from system.calendar_gate import news_proximity_features

        feats = news_proximity_features(key)
        return float(feats.get("trailing_sensitivity_scale") or 1.0)
    except Exception:
        return 1.0


def _resolve_execution_path(epic: str) -> str:
    try:
        from runtime.master_orchestrator import get_strategy_route

        route = get_strategy_route(str(epic or "").strip())
        if route:
            return str(route.get("execution_path") or "")
    except Exception:
        pass
    return ""


def _direction_vector_from_features(vector: np.ndarray, *, horizon_scale: float) -> float:
    """Signed trend vector in [-1, 1] — positive favors BUY."""
    if vector.size < 8:
        return 0.0
    rsi_bias = float(vector[0] - 0.5) * 2.0
    macd_bias = float(vector[5] - vector[6])
    trend = float(vector[7] - 0.5) if vector.size > 7 else 0.0
    raw = 0.45 * macd_bias + 0.35 * rsi_bias + 0.20 * trend
    return float(max(-1.0, min(1.0, raw * horizon_scale)))


def compute_multi_horizon_curves(
    *,
    epic: str,
    direction: str,
    feature_payload: dict[str, Any],
) -> dict[str, Any]:
    """
    Continuous prediction curves across 5-tick, 15-minute, and 4-hour horizons.
    """
    vector = np.asarray(feature_payload.get("vector"), dtype=np.float64)
    if vector.size != 128:
        vector = np.zeros(128, dtype=np.float64)
    dir_sign = 1.0 if str(direction).upper() == "BUY" else -1.0 if str(direction).upper() == "SELL" else 0.0

    h5 = _direction_vector_from_features(vector, horizon_scale=1.0)
    h15 = _direction_vector_from_features(vector, horizon_scale=0.85)
    h4h = _direction_vector_from_features(vector, horizon_scale=0.55)

    try:
        from runtime.regime_switch_engine import get_regime_transition_matrix

        trans = get_regime_transition_matrix()
        trend_bias = float(trans[1, 1] - trans[1, 2])
        h4h = float(max(-1.0, min(1.0, 0.6 * h4h + 0.4 * trend_bias * dir_sign)))
    except Exception:
        pass

    curves = {
        "5_tick": {
            "horizon": "5_tick",
            "bars": _HORIZON_5_TICK_BARS,
            "vector": round(h5, 4),
            "aligned": dir_sign * h5 >= 0,
            "win_prob": round(0.5 + 0.35 * dir_sign * h5, 4),
        },
        "15_min": {
            "horizon": "15_min",
            "bars": _HORIZON_15M_BARS,
            "vector": round(h15, 4),
            "aligned": dir_sign * h15 >= 0,
            "win_prob": round(0.5 + 0.32 * dir_sign * h15, 4),
        },
        "4_hour": {
            "horizon": "4_hour",
            "bars": _HORIZON_4H_BARS,
            "vector": round(h4h, 4),
            "aligned": dir_sign * h4h >= 0,
            "win_prob": round(0.5 + 0.28 * dir_sign * h4h, 4),
        },
    }
    conflict = bool(
        curves["5_tick"]["aligned"]
        and curves["4_hour"]["vector"] * dir_sign < -0.12
    )
    body = {
        "epic": str(epic or ""),
        "direction": str(direction or "").upper(),
        "curves": curves,
        "macro_conflict": conflict,
        "ts": time.time(),
    }
    _multi_horizon_cache[str(epic or "")] = body
    return body


def cross_horizon_veto_limit_chase(
    *,
    epic: str,
    direction: str,
    feature_payload: dict[str, Any],
) -> tuple[bool, str]:
    """Veto HF scalps when 5-tick vector conflicts with 4-hour structural trend."""
    curves = compute_multi_horizon_curves(
        epic=epic, direction=direction, feature_payload=feature_payload
    )
    if not curves.get("macro_conflict"):
        return False, ""
    macro = curves["curves"]["4_hour"]
    micro = curves["curves"]["5_tick"]
    return (
        True,
        f"multi_horizon_conflict micro={micro['vector']:.3f} macro={macro['vector']:.3f}",
    )


def get_multi_horizon_matrix_snapshot() -> dict[str, Any]:
    return {"ok": True, "epics": dict(_multi_horizon_cache)}


def build_cognitive_risk_heatmap() -> dict[str, Any]:
    """Serialize covariance + equilibrium weights into cockpit heat-map cells."""
    cells: list[dict[str, Any]] = []
    try:
        from runtime.portfolio_exploration_engine import get_portfolio_covariance_snapshot

        cov = get_portfolio_covariance_snapshot()
        for row in cov.get("matrix_cells") or []:
            corr = abs(float(row.get("correlation") or 0.0))
            cells.append(
                {
                    "epic_a": row.get("epic_a"),
                    "epic_b": row.get("epic_b"),
                    "intensity": round(corr, 4),
                    "risk_band": (
                        "critical" if corr >= 0.75 else "elevated" if corr >= 0.55 else "normal"
                    ),
                }
            )
        collective = float(cov.get("collective_coefficient") or 0.0)
        compression = float(cov.get("compression_factor") or 1.0)
    except Exception:
        collective = 0.0
        compression = 1.0

    weights: dict[str, float] = {}
    try:
        from execution.risk_manager import get_equilibrium_risk_snapshot

        eq = get_equilibrium_risk_snapshot()
        weights = dict(eq.get("weights") or {})
    except Exception:
        pass

    asset_rows = []
    for epic, weight in sorted(weights.items(), key=lambda kv: -kv[1])[:24]:
        asset_rows.append(
            {
                "epic": epic,
                "allocation_weight": round(float(weight), 4),
                "heat": round(min(1.0, float(weight) * 4.0), 3),
            }
        )

    return {
        "ok": True,
        "collective_coefficient": round(collective, 4),
        "compression_factor": round(compression, 4),
        "pair_cells": cells[:64],
        "asset_weights": asset_rows,
        "ts": time.time(),
    }


def reset_multi_horizon_cache_for_tests() -> None:
    _multi_horizon_cache.clear()


def _alpha_decay_key(epic: str, direction: str = "") -> str:
    return f"{str(epic or '').strip()}:{str(direction or 'BUY').upper()}"


def register_limit_chase_alpha(
    *,
    epic: str,
    direction: str,
    expectation_score: float,
    ts: float | None = None,
) -> dict[str, Any]:
    """Track HF limit-chase alpha for exponential decay after 1500ms without fill."""
    key = _alpha_decay_key(epic, direction)
    now = float(ts if ts is not None else time.time())
    row = {
        "epic": str(epic or "").strip(),
        "direction": str(direction or "BUY").upper(),
        "expectation_score": float(expectation_score),
        "registered_ts": now,
        "last_ts": now,
        "filled": False,
        "killed": False,
    }
    _alpha_decay_orders[key] = row
    return dict(row)


def mark_limit_chase_alpha_filled(*, epic: str, direction: str) -> None:
    key = _alpha_decay_key(epic, direction)
    row = _alpha_decay_orders.get(key)
    if row:
        row["filled"] = True


def compute_alpha_decayed_score(
    *,
    epic: str,
    direction: str,
    now: float | None = None,
) -> tuple[float, float, bool]:
    """
    Return (decayed_score, elapsed_ms, should_kill).

    Exponential decay begins after ALPHA_DECAY_FILL_MS; kill when below strict floor.
    """
    key = _alpha_decay_key(epic, direction)
    row = _alpha_decay_orders.get(key)
    if not row or bool(row.get("filled")) or bool(row.get("killed")):
        return float(row.get("expectation_score") or 0.0) if row else 0.0, 0.0, False
    t_now = float(now if now is not None else time.time())
    elapsed_ms = max(0.0, (t_now - float(row["registered_ts"])) * 1000.0)
    base = float(row.get("expectation_score") or 0.0)
    if elapsed_ms <= _ALPHA_DECAY_FILL_MS:
        return base, elapsed_ms, False
    decay_ms = elapsed_ms - _ALPHA_DECAY_FILL_MS
    decayed = base * math.exp(-decay_ms / max(1.0, _ALPHA_DECAY_HALF_LIFE_MS))
    should_kill = decayed < _ALPHA_DECAY_STRICT_FLOOR
    if should_kill:
        row["killed"] = True
    row["last_decayed_score"] = decayed
    row["elapsed_ms"] = elapsed_ms
    return float(decayed), elapsed_ms, should_kill


def evaluate_limit_chase_alpha_decay(
    *,
    epic: str,
    direction: str,
    expectation_score: float | None = None,
) -> dict[str, Any]:
    """Unified alpha decay evaluation for execution routers."""
    key = _alpha_decay_key(epic, direction)
    if key not in _alpha_decay_orders and expectation_score is not None:
        register_limit_chase_alpha(
            epic=epic, direction=direction, expectation_score=float(expectation_score)
        )
    decayed, elapsed_ms, kill = compute_alpha_decayed_score(epic=epic, direction=direction)
    return {
        "epic": str(epic or "").strip(),
        "direction": str(direction or "BUY").upper(),
        "decayed_score": round(decayed, 4),
        "elapsed_ms": round(elapsed_ms, 1),
        "kill_order": kill,
        "strict_floor": _ALPHA_DECAY_STRICT_FLOOR,
        "fill_deadline_ms": _ALPHA_DECAY_FILL_MS,
    }


def get_alpha_decay_snapshot() -> dict[str, Any]:
    rows = []
    for key, row in list(_alpha_decay_orders.items())[-32:]:
        decayed, elapsed_ms, kill = compute_alpha_decayed_score(
            epic=str(row.get("epic") or ""),
            direction=str(row.get("direction") or "BUY"),
        )
        rows.append(
            {
                "key": key,
                "epic": row.get("epic"),
                "direction": row.get("direction"),
                "base_score": round(float(row.get("expectation_score") or 0.0), 4),
                "decayed_score": round(decayed, 4),
                "elapsed_ms": round(elapsed_ms, 1),
                "filled": bool(row.get("filled")),
                "killed": kill or bool(row.get("killed")),
            }
        )
    return {
        "ok": True,
        "active_orders": len(_alpha_decay_orders),
        "strict_floor": _ALPHA_DECAY_STRICT_FLOOR,
        "fill_deadline_ms": _ALPHA_DECAY_FILL_MS,
        "orders": rows,
    }


def reset_alpha_decay_for_tests() -> None:
    _alpha_decay_orders.clear()


def _rolling_48h_win_rate(route: str) -> float | None:
    key = str(route or "unknown").strip() or "unknown"
    hist = _ml_route_outcomes_ts.get(key)
    if not hist:
        return None
    cutoff = time.time() - _RLS_WINDOW_SEC
    recent = [won for ts, won in hist if ts >= cutoff]
    if len(recent) < 5:
        return None
    return sum(1 for w in recent if w) / len(recent)


def run_rls_calibration_pass(
    *,
    route: str = "",
    feature_vector: np.ndarray | None = None,
) -> dict[str, Any]:
    """
    Background RLS bias adjustment when 48h win rate < 70% and slots 98-111 drift.
    """
    global _RLS_THETA, _RLS_P, WIN_VETO_FLOOR
    route_key = str(route or "limit_chase_hf").strip() or "limit_chase_hf"
    win_rate = _rolling_48h_win_rate(route_key)
    drifted = detect_sentiment_news_feature_drift()
    if win_rate is None or win_rate >= _RLS_WIN_RATE_TARGET or not drifted:
        return {
            "ok": True,
            "adjusted": False,
            "win_rate_48h": win_rate,
            "drift_epics": drifted,
        }

    vec = np.asarray(feature_vector, dtype=np.float64) if feature_vector is not None else np.zeros(128)
    if vec.size < 112:
        vec = np.zeros(128, dtype=np.float64)
    phi = float(np.mean(vec[98:112]))
    target = _RLS_WIN_RATE_TARGET
    prediction = target + _RLS_THETA * phi
    error = target - prediction
    denom = _RLS_LAMBDA + phi * _RLS_P * phi
    gain = (_RLS_P * phi) / max(1e-9, denom)
    _RLS_THETA = _RLS_THETA + gain * error
    _RLS_P = (_RLS_P - gain * phi * _RLS_P) / _RLS_LAMBDA
    bump = min(0.08, max(0.01, abs(error) * 0.35))
    WIN_VETO_FLOOR = float(min(WIN_VETO_FLOOR_CAP, WIN_VETO_FLOOR + bump))
    body = {
        "ok": True,
        "adjusted": True,
        "route": route_key,
        "win_rate_48h": round(win_rate, 4),
        "drift_epics": drifted,
        "rls_theta": round(_RLS_THETA, 6),
        "veto_floor": round(WIN_VETO_FLOOR, 4),
        "ts": time.time(),
    }
    _rls_last_adjustment.clear()
    _rls_last_adjustment.update(body)
    log_engine(
        f"RLS calibrator: route={route_key} win_rate={win_rate:.2%} "
        f"theta={_RLS_THETA:.4f} veto_floor={WIN_VETO_FLOOR:.3f}"
    )
    return body


def get_rls_calibrator_snapshot() -> dict[str, Any]:
    routes: dict[str, Any] = {}
    for route in _ml_route_outcomes_ts:
        wr = _rolling_48h_win_rate(route)
        if wr is not None:
            routes[route] = {"win_rate_48h": round(wr, 4)}
    return {
        "ok": True,
        "theta": round(_RLS_THETA, 6),
        "p_cov": round(_RLS_P, 6),
        "target_win_rate": _RLS_WIN_RATE_TARGET,
        "last_adjustment": dict(_rls_last_adjustment),
        "routes": routes,
    }


def reset_rls_calibrator_for_tests() -> None:
    global _RLS_THETA, _RLS_P, WIN_VETO_FLOOR
    _RLS_THETA = 0.0
    _RLS_P = 1.0
    _rls_last_adjustment.clear()
    WIN_VETO_FLOOR = WIN_VETO_FLOOR_STRICT
    _ml_route_outcomes_ts.clear()


def run_48bar_shadow_walk_expectation(
    *,
    epic: str,
    direction: str,
    feature_payload: dict[str, Any],
) -> dict[str, Any]:
    """
    Internal 48-bar pseudo-Monte Carlo expectation curve using Markov regime matrix.

    Veto long-horizon trend holds when projected win-probability density < 0.65.
    """
    dir_u = str(direction or "").upper()
    if dir_u not in ("BUY", "SELL"):
        return {
            "projected_win_prob": 0.5,
            "veto": False,
            "density": [],
            "reason": "flat_direction",
        }

    vector = np.asarray(feature_payload.get("vector"), dtype=np.float64)
    if vector.size != 128:
        vector = np.zeros(128, dtype=np.float64)

    try:
        from runtime.regime_switch_engine import RegimeState, evaluate_epic_regime, get_regime_transition_matrix

        snap = evaluate_epic_regime(str(epic or ""))
        if str(getattr(snap, "reason", "") or "") == "insufficient_bars":
            return {
                "projected_win_prob": None,
                "veto": False,
                "density": [],
                "reason": "warming",
            }
        state = int(snap.state)
        conf = float(max(0.35, min(0.95, snap.confidence)))
    except Exception:
        state = 1
        conf = 0.5

    probs = np.array([0.2, 0.2, 0.2], dtype=np.float64)
    probs[state] = conf
    probs = probs / max(float(np.sum(probs)), 1e-9)

    try:
        transition = get_regime_transition_matrix()
    except Exception:
        transition = np.array(
            [[0.70, 0.15, 0.15], [0.10, 0.75, 0.15], [0.20, 0.15, 0.65]],
            dtype=np.float64,
        )

    dir_sign = 1.0 if dir_u == "BUY" else -1.0
    trend_bias = float(vector[4]) * 2.0 - 1.0
    sentiment_tail = float(vector[99]) - float(vector[100])
    news_pressure = float(vector[105])

    density: list[float] = []
    for step in range(_FORWARD_WALK_BARS):
        probs = probs @ transition
        regime_edge = float(np.dot(probs, _REGIME_STATE_PAYOFF))
        horizon_decay = 1.0 - (step / max(_FORWARD_WALK_BARS, 1)) * 0.35
        align = 0.5 + 0.28 * dir_sign * trend_bias * horizon_decay
        align += 0.08 * sentiment_tail
        align -= 0.12 * news_pressure * (step / max(_FORWARD_WALK_BARS, 1))
        density.append(float(max(0.0, min(1.0, regime_edge * align))))

    projected = float(np.mean(density)) if density else 0.5
    veto_floor = (
        _FORWARD_WALK_VETO_FLOOR_SYNTHETIC
        if _synthetic_alpha_gate_active
        else _FORWARD_WALK_VETO_FLOOR
    )
    veto = projected < veto_floor
    return {
        "projected_win_prob": round(projected, 4),
        "veto": veto,
        "density": [round(x, 4) for x in density[-8:]],
        "reason": (
            "synthetic_hydration_continuity"
            if _synthetic_alpha_gate_active and not veto
            else ("shadow_walk_below_floor" if veto else "shadow_walk_pass")
        ),
        "regime_state": state,
        "veto_floor": veto_floor,
    }


def _shadow_brain_near_miss_active() -> bool:
    """True when shadow brain published an ml_veto near-miss tolerance recently."""
    try:
        from system.identity.live_tolerance_bridge import load_live_tolerance_manifest

        manifest = load_live_tolerance_manifest()
        if not isinstance(manifest, dict):
            return False
        gate = str(manifest.get("near_miss_gate") or "")
        if gate != "ml_veto":
            return False
        published = float(manifest.get("published_at_epoch") or 0)
        if published <= 0 or (time.time() - published) > 3600.0:
            return False
        floors = manifest.get("live_floors") or manifest.get("adjustments") or {}
        if isinstance(floors, dict):
            try:
                ml_floor = float(floors.get("ml_veto_min_probability") or 0)
                if 0 < ml_floor <= WIN_VETO_FLOOR_RELAXED:
                    return True
            except (TypeError, ValueError):
                pass
        return True
    except Exception:
        return False


def _shadow_counterfactual_win_streak(*, epic: str = "", market: str = "") -> bool:
    """
    Positive win-rate streak from 48-bar forward-walk shadow counterfactuals.

  Used to authorize relaxed ML veto floor (0.45) when shadow brain near-miss
    historically would have won.
    """
    try:
        from data.learning_store import LearningStore
        from system.config_loader import get_config

        cfg = get_config()
        db_path = str(getattr(cfg, "learning_db", "") or "")
        if not db_path:
            return False
        store = LearningStore(db_path)
        key_prefix = str(market or epic or "").strip()
        if not key_prefix:
            return False
        row = store.setup_stats(key_prefix)
        if row is None:
            for suffix in ("|BUY", "|SELL"):
                row = store.setup_stats(f"{key_prefix}{suffix}")
                if row is not None:
                    break
        if row is None:
            return False
        wins = int(row.get("wins") or 0)
        trades = int(row.get("trades") or 0)
        winrate = float(row.get("winrate") or 0.0)
        if trades < _SHADOW_STREAK_MIN_WINS:
            return False
        if winrate >= _SHADOW_STREAK_MIN_WINRATE:
            return True
        return wins >= _SHADOW_STREAK_MIN_WINS and (wins / max(trades, 1)) >= _SHADOW_STREAK_MIN_WINRATE
    except Exception:
        return False


def resolve_scoreboard_veto_floor() -> float:
    """PlatformScoreboard PP tiers drive aggressive expansion vs defensive contraction."""
    try:
        from runtime.master_orchestrator import (
            PP_DEFENSE_THRESHOLD,
            PP_EXPANSION_THRESHOLD,
            TELEMETRY_TIER_EMERALD,
            get_platform_scoreboard,
        )

        sb = get_platform_scoreboard()
        pp = int(sb.total_pp)
        if sb.telemetry_tier_unlocked() == TELEMETRY_TIER_EMERALD or pp >= PP_EXPANSION_THRESHOLD:
            return VETO_FLOOR_EXPANSION_PP
        if pp <= PP_DEFENSE_THRESHOLD:
            return VETO_FLOOR_DEFENSIVE_PP
    except Exception:
        pass
    return WIN_VETO_FLOOR_STRICT


def resolve_dynamic_veto_floor(*, epic: str = "", market: str = "") -> float:
    """
    Aggressive 70%+ hit-rate target: scoreboard tiers set 0.50 / 0.55 / 0.62 floors.

    Relaxes to 0.45 only when shadow brain ml_veto near-miss pairs with a
    positive 48-bar counterfactual win streak in setup_stats.
    News-alpha path relaxes to 0.48 when sentiment acceleration aligns with regime 0/1.
    Cognitive healer may raise veto floor dynamically (capped at 0.65).
    """
    global _news_alpha_veto_relax_active
    base = resolve_scoreboard_veto_floor()
    if _cognitive_veto_bump > 0:
        base = min(WIN_VETO_FLOOR_CAP, base + _cognitive_veto_bump)
    key = str(epic or market or "").strip()
    if key and sentiment_regime_alpha_aligned(key):
        _news_alpha_veto_relax_active = True
        return min(base, WIN_VETO_FLOOR_NEWS_ALPHA)
    _news_alpha_veto_relax_active = False
    if _shadow_brain_near_miss_active() and _shadow_counterfactual_win_streak(
        epic=epic, market=market
    ):
        return min(base, WIN_VETO_FLOOR_RELAXED)
    return min(base, WIN_VETO_FLOOR_CAP)


def compute_horizon_sentiment_derivatives(
    epic: str,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """First and second derivatives of client sentiment across 5m / 15m / 1h horizons."""
    key = str(epic or "").strip()
    try:
        from trading.sentiment_momentum import sentiment_momentum_features

        feats = sentiment_momentum_features(key, now=now)
    except Exception:
        feats = {}
    body = {
        "epic": key,
        "delta_5m": float(feats.get("delta_5m") or 0.0),
        "delta_15m": float(feats.get("delta_15m") or 0.0),
        "delta_1h": float(feats.get("delta_1h") or 0.0),
        "accel_5m": float(feats.get("accel_5m") or 0.0),
        "accel_15m": float(feats.get("accel_15m") or 0.0),
        "accel_1h": float(feats.get("accel_1h") or 0.0),
        "flow_acceleration": round(
            0.5 * float(feats.get("accel_5m") or 0.0)
            + 0.35 * float(feats.get("accel_15m") or 0.0)
            + 0.15 * float(feats.get("accel_1h") or 0.0),
            6,
        ),
        "ts": time.time(),
    }
    if key:
        _horizon_sentiment_cache[key] = body
    return body


def sentiment_regime_alpha_aligned(epic: str) -> bool:
    """True when institutional flow acceleration aligns with mean-reversion or HV-trend regime."""
    key = str(epic or "").strip()
    if not key:
        return False
    try:
        from runtime.regime_switch_engine import evaluate_epic_regime

        snap = evaluate_epic_regime(key)
        state = int(snap.state)
    except Exception:
        return False
    if state not in (0, 1):
        return False
    deriv = compute_horizon_sentiment_derivatives(key)
    flow = float(deriv.get("flow_acceleration") or 0.0)
    try:
        from system.market_data_hub import get_headline_urgency_snapshot

        recent = get_headline_urgency_snapshot().get("epics", {}).get(key) or {}
        headline_accel = float(recent.get("acceleration") or 0.0)
    except Exception:
        headline_accel = 0.0
    combined = flow + headline_accel * 0.35
    return abs(combined) >= 0.0008 and (combined > 0 or state == 0)


def get_horizon_sentiment_snapshot() -> dict[str, Any]:
    return {"ok": True, "epics": dict(_horizon_sentiment_cache), "news_alpha_relax": _news_alpha_veto_relax_active}


def get_news_alpha_telemetry_snapshot() -> dict[str, Any]:
    headline: dict[str, Any] = {}
    try:
        from system.market_data_hub import get_headline_urgency_snapshot

        headline = get_headline_urgency_snapshot()
    except Exception:
        pass
    return {
        "ok": True,
        "horizon_sentiment": get_horizon_sentiment_snapshot(),
        "headline_urgency": headline,
        "veto_floor_news_alpha": WIN_VETO_FLOOR_NEWS_ALPHA,
        "news_alpha_relax_active": _news_alpha_veto_relax_active,
        "ts": time.time(),
    }


def apply_cognitive_self_correction(*, reason: str, veto_bump: float = 0.05) -> dict[str, Any]:
    """Raise veto floor and tighten Markov matrix when accuracy/drift triggers."""
    global WIN_VETO_FLOOR, _cognitive_veto_bump
    step = max(0.01, min(0.08, float(veto_bump)))
    _cognitive_veto_bump = min(0.10, _cognitive_veto_bump + step)
    WIN_VETO_FLOOR = min(
        WIN_VETO_FLOOR_CAP,
        WIN_VETO_FLOOR_STRICT + _cognitive_veto_bump,
    )
    matrix_meta: dict[str, Any] = {}
    try:
        from runtime.regime_switch_engine import apply_transition_matrix_strictness

        matrix_meta = apply_transition_matrix_strictness(bump=step)
    except Exception:
        pass
    log_engine(
        f"probability_engine cognitive_correction reason={reason[:80]} "
        f"veto_floor={WIN_VETO_FLOOR:.3f}"
    )
    return {
        "ok": True,
        "reason": reason,
        "win_veto_floor": WIN_VETO_FLOOR,
        "matrix": matrix_meta,
    }


def reset_cognitive_self_correction_for_tests() -> None:
    global WIN_VETO_FLOOR, _cognitive_veto_bump, _feature_baseline_slots, _ml_route_outcomes
    global _synthetic_alpha_gate_active
    _cognitive_veto_bump = 0.0
    WIN_VETO_FLOOR = WIN_VETO_FLOOR_STRICT
    _synthetic_alpha_gate_active = False
    _feature_baseline_slots.clear()
    _ml_route_outcomes.clear()
    reset_alpha_decay_for_tests()
    reset_rls_calibrator_for_tests()
    _horizon_sentiment_cache.clear()
    global _news_alpha_veto_relax_active
    _news_alpha_veto_relax_active = False


def record_strategy_route_outcome(route: str, won: bool) -> None:
    key = str(route or "unknown").strip() or "unknown"
    if key not in _ml_route_outcomes:
        _ml_route_outcomes[key] = deque(maxlen=20)
    _ml_route_outcomes[key].append(bool(won))
    if key not in _ml_route_outcomes_ts:
        _ml_route_outcomes_ts[key] = deque(maxlen=512)
    now = time.time()
    _ml_route_outcomes_ts[key].append((now, bool(won)))
    cutoff = now - _RLS_WINDOW_SEC
    hist = _ml_route_outcomes_ts[key]
    while hist and hist[0][0] < cutoff:
        hist.popleft()
    try:
        from signals.feature_state import compile_current_feature_state

        compiled = compile_current_feature_state(epic="", market="")
        vec = np.asarray(compiled.get("vector"), dtype=np.float64)
    except Exception:
        vec = None
    run_rls_calibration_pass(route=key, feature_vector=vec)


def get_ml_accuracy_metrics() -> dict[str, Any]:
    routes: dict[str, Any] = {}
    for route, outcomes in _ml_route_outcomes.items():
        if not outcomes:
            continue
        wins = sum(1 for w in outcomes if w)
        routes[route] = {
            "trades": len(outcomes),
            "wins": wins,
            "win_rate": round(wins / len(outcomes), 4),
        }
    return {
        "win_veto_floor": float(WIN_VETO_FLOOR),
        "cognitive_veto_bump": float(_cognitive_veto_bump),
        "routes": routes,
        "rls": get_rls_calibrator_snapshot(),
        "alpha_decay": get_alpha_decay_snapshot(),
    }


def detect_sentiment_news_feature_drift(*, threshold: float = 0.22) -> list[str]:
    """
    Compare slots 98-111 (sentiment/news vectors) against rolling baseline.

    Returns epic keys with material drift for autonomic healer.
    """
    drifted: list[str] = []
    try:
        from signals.feature_state import compile_current_feature_state
        from system.market_data_hub import NIGHT_MATRIX_EPICS
    except Exception:
        return drifted

    for epic in NIGHT_MATRIX_EPICS:
        try:
            compiled = compile_current_feature_state(epic=epic, market=epic)
            vec = np.asarray(compiled.get("vector"), dtype=np.float64)
            if vec.size < 112:
                continue
            tail = vec[98:112].copy()
            base = _feature_baseline_slots.get(epic)
            if base is None:
                _feature_baseline_slots[epic] = tail
                continue
            delta = float(np.mean(np.abs(tail - base)))
            if delta >= threshold:
                drifted.append(str(epic))
            _feature_baseline_slots[epic] = 0.85 * base + 0.15 * tail
        except Exception:
            continue
    return drifted


def compile_cognitive_reasoning(*, epic: str = "") -> dict[str, Any]:
    """
    Plain-English strategic counsel — vetoes, spreads, correlation, news proximity.

    Returns text + severity for Flight Deck Cognitive Reasoner HUD.
    """
    try:
        from runtime.portfolio_exploration_engine import get_last_rotation_counsel

        rotation_counsel = str(get_last_rotation_counsel() or "").strip()
        if rotation_counsel:
            trade_ready = "TRADE READY" in rotation_counsel.upper()
            return {
                "text": rotation_counsel,
                "severity": "execution_window" if trade_ready else "near_miss",
                "rotation_active": True,
                "epic": epic,
            }
    except Exception:
        pass

    key = str(epic or "").strip()
    parts: list[str] = []
    severity = "normal"
    adaptive_ceiling = 0.0
    spread_pts = 0.0
    ml_score = 0.0

    try:
        from runtime.portfolio_exploration_engine import (
            adaptive_spread_telemetry,
            get_exploration_state_snapshot,
            vet_order_spread,
        )

        explore = get_exploration_state_snapshot()
        rankings = list(explore.get("market_rankings") or [])
        if not key and rankings:
            key = str(rankings[0].get("epic") or "")
        if key:
            row = next((r for r in rankings if r.get("epic") == key), {})
            ml_score = float(row.get("score") or 0.0)
            tel = adaptive_spread_telemetry(key, expectation_score=ml_score)
            spread_pts = float(tel.get("spread_pts") or 0.0)
            adaptive_ceiling = float(tel.get("adaptive_ceiling_pts") or 0.0)
            spread_ok, spread_reason, _ = vet_order_spread(
                key, spread_pts, expectation_score=ml_score
            )
            if spread_ok and ml_score >= WIN_PROMOTE_FLOOR:
                severity = "execution_window"
                parts.append(
                    f"EXECUTION WINDOW — {key} ML expectation {ml_score:.0%} with spread "
                    f"{spread_pts:.2f}pts inside adaptive ceiling {adaptive_ceiling:.2f}pts"
                )
            elif not spread_ok:
                severity = "near_miss"
                parts.append(
                    f"NEAR MISS — {key} spread {spread_pts:.2f}pts exceeds adaptive ceiling "
                    f"{adaptive_ceiling:.2f}pts ({spread_reason})"
                )
            elif adaptive_ceiling > 0 and spread_pts >= adaptive_ceiling * 0.85:
                severity = "near_miss"
                parts.append(
                    f"NEAR MISS — {key} spread {spread_pts:.2f}pts approaching ceiling "
                    f"{adaptive_ceiling:.2f}pts (ML {ml_score:.0%})"
                )
            else:
                parts.append(
                    f"HOLD — {key} spread {spread_pts:.2f}pts within ceiling "
                    f"{adaptive_ceiling:.2f}pts"
                )

        corr_rows = list(explore.get("correlation_exposures") or [])
        if corr_rows:
            top = corr_rows[0]
            parts.append(
                f"correlation {float(top.get('correlation', 0)):.2f} between "
                f"{top.get('epic_a')} and {top.get('epic_b')}"
            )
        elif explore.get("entry_frozen"):
            parts.append("margin entry frozen — portfolio at utilization ceiling")
    except Exception as exc:
        parts.append(f"portfolio telemetry warming ({type(exc).__name__})")

    metrics = get_ml_accuracy_metrics()
    veto_floor = float(metrics.get("win_veto_floor") or WIN_VETO_FLOOR)
    veto_bump = float(metrics.get("cognitive_veto_bump") or 0.0)
    if veto_bump > 0:
        severity = "near_miss" if severity == "normal" else severity
        parts.append(f"cognitive veto bump +{veto_bump:.2f} (floor {veto_floor:.0%})")

    news_norm = 0.0
    news_label = ""
    if key:
        try:
            from system.calendar_gate import news_proximity_features

            feats = news_proximity_features(key)
            news_norm = float(feats.get("countdown_norm") or 0.0)
            secs = int(feats.get("seconds_to_next") or 0)
            if news_norm >= 0.35:
                severity = "near_miss" if severity == "normal" else severity
                news_label = f"macro news T-{secs}s (proximity {news_norm:.0%})"
                parts.append(news_label)
        except Exception:
            pass

    try:
        from runtime.master_orchestrator import (
            PP_EXPANSION_THRESHOLD,
            TELEMETRY_TIER_EMERALD,
            get_platform_scoreboard,
        )

        sb = get_platform_scoreboard()
        tier = sb.telemetry_tier_unlocked()
        pp = int(sb.total_pp)
        if tier == TELEMETRY_TIER_EMERALD or pp >= PP_EXPANSION_THRESHOLD:
            if severity != "execution_window":
                severity = "near_miss" if severity == "normal" and ml_score >= 0.55 else severity
            parts.append(f"Emerald expansion tier active (PP {pp}) — flash scalp lane eligible")
    except Exception:
        pass

    try:
        from system.autonomic_healer import get_ai_diagnostics_snapshot

        diag = get_ai_diagnostics_snapshot() or {}
        override = str(diag.get("cognitive_override_reason") or "").strip()
        if override:
            severity = "near_miss" if severity == "normal" else severity
            parts.append(f"autonomic override: {override}")
    except Exception:
        pass

    if not parts:
        return {
            "text": "Strategic counsel warming — awaiting live veto and spread telemetry.",
            "severity": "normal",
            "adaptive_spread_ceiling": adaptive_ceiling,
            "spread_pts": spread_pts,
            "epic": key,
        }

    text = "; ".join(parts)
    if severity == "normal" and not text.upper().startswith("HOLD"):
        text = f"HOLD — {text}"
    return {
        "text": text,
        "severity": severity,
        "adaptive_spread_ceiling": adaptive_ceiling,
        "spread_pts": spread_pts,
        "epic": key,
        "ml_expectation_score": ml_score,
        "news_countdown_norm": news_norm,
    }


def get_cognitive_reasoning_string(*, epic: str = "") -> str:
    """Async-safe plain-English trade-hold counsel for orchestrator + Flight Deck HUD."""
    return str(compile_cognitive_reasoning(epic=epic).get("text") or "")


def _extract_ml_features(sig: SignalResult, vector: np.ndarray) -> dict[str, float]:
    snap = sig.snapshot or {}
    last = snap.get("last") or {}
    stop = 1.0
    try:
        from system.config_loader import get_config

        stop = max(1.0, float(get_config().stop_distance_points))
    except Exception:
        pass
    atr = float(last.get("atr", 0) or 0)
    return {
        "adjusted_score": float(snap.get("adjusted_confidence") or vector[7] * 100.0),
        "raw_score": float(snap.get("raw_confidence") or vector[8] * 100.0),
        "rsi": float(last.get("rsi", 0) or vector[0] * 100.0),
        "atr_ratio": atr / stop if stop > 0 else 0.0,
    }


def compute_win_probability(
    *,
    sig: SignalResult,
    feature_payload: dict[str, Any],
    epic: str = "",
    market: str = "",
) -> float:
    """Blend Pillar 4 ML scorer with online continuous-optimization weights."""
    vector = np.asarray(feature_payload.get("vector"), dtype=np.float64)
    if vector.size != 128:
        vector = np.zeros(128, dtype=np.float64)

    ml_prob = 0.5
    try:
        from trading.ml_scorer import get_ml_scorer

        scorer = get_ml_scorer()
        if scorer.is_trained():
            feats = _extract_ml_features(sig, vector)
            if all(k in feats for k in scorer.feature_names):
                ml_prob = float(scorer.predict(feats))
    except Exception as exc:
        log_engine(f"probability_engine ml_scorer: {type(exc).__name__}: {exc}")

    opt_prob = 0.5
    try:
        from trading.continuous_optimization_worker import get_continuous_optimization_worker

        opt_prob = float(get_continuous_optimization_worker().predict(vector))
    except Exception as exc:
        log_engine(f"probability_engine continuous opt: {type(exc).__name__}: {exc}")

    if ml_prob != 0.5 or opt_prob != 0.5:
        blended = 0.55 * ml_prob + 0.45 * opt_prob
    else:
        # Heuristic from technical vector when models cold
        directional = float(vector[5] - vector[6])
        rsi_bias = float(vector[0] - 0.5)
        blended = 0.5 + 0.25 * directional + 0.15 * rsi_bias
    if abs(_RLS_THETA) > 1e-6 and vector.size >= 112:
        phi = float(np.mean(vector[98:112]))
        blended = blended + _RLS_THETA * phi * 0.15
    return float(max(0.0, min(1.0, blended)))


def apply_hierarchical_probability_gate(
    *,
    sig: SignalResult,
    feature_payload: dict[str, Any],
    peak_score: float,
    threshold: float,
    epic: str = "",
    market: str = "",
    execution_path: str = "",
) -> ProbabilityVerdict:
    """
    Run ML selection brain when technical setup clears 42% ingestion floor.
    """
    if float(peak_score) < STRATEGY_THRESHOLD_LOW_PCT:
        return ProbabilityVerdict(
            win_probability=0.5,
            model_verdict="NEUTRAL",
            veto=False,
            promote=False,
            threshold_relief=0.0,
        )

    raw_dir = str((sig.snapshot or {}).get("raw_signal") or sig.signal or "").strip()
    if raw_dir not in ("BUY", "SELL"):
        return ProbabilityVerdict(
            win_probability=0.5,
            model_verdict="NEUTRAL",
            veto=False,
            promote=False,
            threshold_relief=0.0,
        )

    win_probability = compute_win_probability(
        sig=sig,
        feature_payload=feature_payload,
        epic=epic,
        market=market,
    )

    trail_scale = compute_news_trailing_sensitivity(epic=epic, market=market)
    news_norm = 0.0
    try:
        from system.calendar_gate import news_proximity_features

        news_norm = float(news_proximity_features(str(epic or market or "")).get("countdown_norm") or 0.0)
    except Exception:
        pass

    route_path = str(execution_path or _resolve_execution_path(str(epic or "")))
    if route_path == "limit_chase_hf":
        mh_veto, mh_reason = cross_horizon_veto_limit_chase(
            epic=str(epic or ""),
            direction=raw_dir,
            feature_payload=feature_payload,
        )
        if mh_veto:
            return ProbabilityVerdict(
                win_probability=win_probability,
                model_verdict="MULTI_HORIZON_VETO",
                veto=True,
                promote=False,
                threshold_relief=0.0,
                ml_veto_token="MULTI_HORIZON_VETO",
                trailing_sensitivity_scale=trail_scale,
                forward_walk_win_prob=0.0,
                news_countdown_norm=news_norm,
            )

    forward_walk_prob = 0.0
    if route_path == "momentum_breakout":
        walk = run_48bar_shadow_walk_expectation(
            epic=str(epic or ""),
            direction=raw_dir,
            feature_payload=feature_payload,
        )
        forward_walk_prob = float(walk.get("projected_win_prob") or 0.0)
        if bool(walk.get("veto")):
            return ProbabilityVerdict(
                win_probability=win_probability,
                model_verdict="SHADOW_WALK_VETO",
                veto=True,
                promote=False,
                threshold_relief=0.0,
                ml_veto_token="SHADOW_WALK_VETO",
                trailing_sensitivity_scale=trail_scale,
                forward_walk_win_prob=forward_walk_prob,
                news_countdown_norm=news_norm,
            )
        win_probability = float(
            max(0.0, min(1.0, 0.65 * win_probability + 0.35 * forward_walk_prob))
        )

    if win_probability >= WIN_PROMOTE_FLOOR:
        return ProbabilityVerdict(
            win_probability=win_probability,
            model_verdict="PROMOTE",
            veto=False,
            promote=True,
            threshold_relief=PROMOTE_THRESHOLD_RELIEF_PCT,
            trailing_sensitivity_scale=trail_scale,
            forward_walk_win_prob=forward_walk_prob,
            news_countdown_norm=news_norm,
        )

    veto_floor = resolve_dynamic_veto_floor(epic=epic, market=market)
    veto_floor = float(min(0.60, veto_floor + news_norm * 0.04))
    if win_probability < veto_floor:
        return ProbabilityVerdict(
            win_probability=win_probability,
            model_verdict="ML_VETO_REJECTION",
            veto=True,
            promote=False,
            threshold_relief=0.0,
            ml_veto_token="ML_VETO_REJECTION",
            trailing_sensitivity_scale=trail_scale,
            forward_walk_win_prob=forward_walk_prob,
            news_countdown_norm=news_norm,
        )

    return ProbabilityVerdict(
        win_probability=win_probability,
        model_verdict="NEUTRAL",
        veto=False,
        promote=False,
        threshold_relief=0.0,
        trailing_sensitivity_scale=trail_scale,
        forward_walk_win_prob=forward_walk_prob,
        news_countdown_norm=news_norm,
    )


def annotate_signal_with_probability(
    sig: SignalResult,
    verdict: ProbabilityVerdict,
    feature_payload: dict[str, Any],
) -> SignalResult:
    """Attach probability metadata to signal snapshot for downstream gates."""
    snap = dict(sig.snapshot or {})
    snap["win_probability"] = verdict.win_probability
    snap["model_verdict"] = verdict.model_verdict
    snap["feature_state_ts_ms"] = feature_payload.get("ts_ms")
    snap["feature_state_dim"] = feature_payload.get("dim")
    if verdict.ml_veto_token:
        snap["ml_veto_token"] = verdict.ml_veto_token
    snap["trailing_sensitivity_scale"] = float(verdict.trailing_sensitivity_scale)
    snap["forward_walk_win_prob"] = float(verdict.forward_walk_win_prob)
    snap["news_countdown_norm"] = float(verdict.news_countdown_norm)
    return SignalResult(
        signal=sig.signal,
        raw_confidence=float(sig.raw_confidence),
        adjusted_confidence=float(sig.adjusted_confidence),
        learning_delta=float(sig.learning_delta),
        setup_key=str(sig.setup_key),
        notes=str(sig.notes),
        snapshot=snap,
    )
