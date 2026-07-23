"""Probabilistic micro-sniper gate — sigmoid P(Success) off the tick lane.

``QuantumSniperMLCore.evaluate_entry_probability`` blends OBI velocity,
spread elasticity, tick acceleration, ATR velocity, and the 1s-cached
``grok_macro_bias`` string into a calibrated success probability. Asset-class
adaptive thresholds replace the flat 68% gate (tighter for Gold, optimized for
indices / FX). Dynamic v34 vol vector tightens Gold/FX gates toward 0.82 when
30-tick spread elasticity expands (MM liquidity withdrawal).
Extreme elasticity collapses P toward a ~10% safety baseline (chop isolation).
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

SNIPER_THRESHOLD = 0.68  # index default (backward-compatible)
SAFETY_BASELINE = 0.10

# Asset-class adaptive gates — Gold premium spreads need higher conviction.
THRESHOLD_INDEX = 0.68
THRESHOLD_FX = 0.70
THRESHOLD_GOLD = 0.74
THRESHOLD_LIQUIDITY_STRESS_CEILING = 0.82

# Rolling vol feature plane (30 ticks)
_VOL_TICK_WINDOW = 30
_MIN_VOL_TICKS_FOR_DYNAMIC = 10

# Reweighted feature plane: penalize wide premium, reward clean momentum,
# de-emphasize depthless Mini OBI noise.
W_BIAS = 0.50
W_OBI = 1.15
W_VELOCITY = 0.75
W_ELASTICITY = 2.35
W_ATR_VELOCITY = 0.55
W_GROK_BULL_BEAR = 0.40

_lock = threading.RLock()
_last_by_epic: dict[str, dict[str, Any]] = {}
_last_global: dict[str, Any] = {
    "p_success": None,
    "approved": False,
    "threshold": SNIPER_THRESHOLD,
    "ts": 0.0,
    "epic": "",
    "features": {},
}
_vol_tick_history: dict[str, deque[tuple[float, float]]] = {}


@dataclass(frozen=True)
class SniperProbabilityResult:
    p_success: float
    approved: bool
    threshold: float
    logit: float
    features: dict[str, Any]
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "p_success": self.p_success,
            "approved": self.approved,
            "threshold": self.threshold,
            "logit": self.logit,
            "features": dict(self.features),
            "reason": self.reason,
        }


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _sigmoid(x: float) -> float:
    # Numerically stable logistic
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _macro_logit(bias: str) -> float:
    b = str(bias or "NEUTRAL").strip().upper()
    if b == "VETO":
        return -3.5
    if b == "BULL":
        return W_GROK_BULL_BEAR
    if b == "BEAR":
        return W_GROK_BULL_BEAR
    return 0.0


def asset_class_for_epic(epic: str) -> str:
    """Return GOLD | FX | INDEX for adaptive sniper thresholds."""
    key = str(epic or "").upper()
    if "GOLD" in key or "XAU" in key or "CFPGOLD" in key:
        return "GOLD"
    if "IX.D." in key or "IFM.IP" in key:
        return "INDEX"
    if any(fx in key for fx in ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "EURGBP")):
        return "FX"
    if key.startswith("CS.D.") and "CFD.IP" in key and "CRUDE" not in key:
        return "FX"
    return "INDEX"


def sniper_threshold_for_epic(epic: str = "") -> float:
    cls = asset_class_for_epic(epic)
    if cls == "GOLD":
        return THRESHOLD_GOLD
    if cls == "FX":
        return THRESHOLD_FX
    return THRESHOLD_INDEX


def reset_volatility_feature_history_for_tests() -> None:
    global _vol_tick_history
    with _lock:
        _vol_tick_history = {}


def observe_volatility_features(
    epic: str,
    *,
    spread_elasticity: float,
    atr_velocity: float,
) -> None:
    """Record rolling 30-tick spread elasticity + ATR velocity (no I/O)."""
    key = str(epic or "").strip()
    if not key:
        return
    elast = max(0.0, float(spread_elasticity or 1.0))
    atr_v = float(atr_velocity or 0.0)
    with _lock:
        hist = _vol_tick_history.get(key)
        if hist is None:
            hist = deque(maxlen=_VOL_TICK_WINDOW)
            _vol_tick_history[key] = hist
        hist.append((elast, atr_v))


def _liquidity_stress_blend(epic: str) -> tuple[float, dict[str, float]]:
    """
    When 30-tick spread elasticity expands on Gold / FX, tighten threshold
    from class base (~0.68–0.74) toward ``THRESHOLD_LIQUIDITY_STRESS_CEILING``.
    """
    cls = asset_class_for_epic(epic)
    base = sniper_threshold_for_epic(epic)
    meta = {
        "base_threshold": base,
        "dynamic_threshold": base,
        "liquidity_stress_blend": 0.0,
        "spread_elasticity_delta": 0.0,
        "atr_velocity_mean": 0.0,
    }
    if cls not in ("GOLD", "FX"):
        return base, meta

    with _lock:
        hist = list(_vol_tick_history.get(str(epic or ""), ()))

    if len(hist) < _MIN_VOL_TICKS_FOR_DYNAMIC:
        return base, meta

    elast_vals = [row[0] for row in hist]
    atr_vals = [row[1] for row in hist]
    recent_n = min(10, len(elast_vals))
    prior_n = min(20, max(0, len(elast_vals) - recent_n))
    recent_mean = sum(elast_vals[-recent_n:]) / float(recent_n)
    prior_mean = (
        sum(elast_vals[-recent_n - prior_n : -recent_n]) / float(prior_n)
        if prior_n > 0
        else recent_mean
    )
    delta = recent_mean - prior_mean
    meta["spread_elasticity_delta"] = round(delta, 6)
    meta["atr_velocity_mean"] = round(
        sum(atr_vals[-recent_n:]) / float(recent_n), 6
    )

    # MM liquidity withdrawal: elasticity rising + positive ATR velocity tailwind
    if delta <= 0.05 or recent_mean <= 1.05:
        return base, meta

    expansion = min(1.0, delta / max(prior_mean * 0.35, 0.05))
    atr_tail = max(0.0, meta["atr_velocity_mean"])
    if atr_tail > 0:
        expansion = min(1.0, expansion + min(0.25, atr_tail * 0.08))

    dynamic = base + (THRESHOLD_LIQUIDITY_STRESS_CEILING - base) * expansion
    dynamic = _clamp(dynamic, base, THRESHOLD_LIQUIDITY_STRESS_CEILING)
    meta["liquidity_stress_blend"] = round(expansion, 6)
    meta["dynamic_threshold"] = round(dynamic, 6)
    return dynamic, meta


def dynamic_sniper_threshold(epic: str = "") -> float:
    """Asset-class base threshold with optional Gold/FX liquidity-stress tighten."""
    thr, _ = _liquidity_stress_blend(epic)
    return thr


class QuantumSniperMLCore:
    """Lightweight probabilistic sniper — no network I/O, thread-safe cache."""

    threshold: float = SNIPER_THRESHOLD
    safety_baseline: float = SAFETY_BASELINE

    def evaluate_entry_probability(
        self,
        *,
        obi_velocity: float,
        spread_elasticity: float,
        tick_acceleration: float,
        grok_macro_bias: str,
        epic: str = "",
        direction: str = "",
        atr_velocity: float = 0.0,
    ) -> SniperProbabilityResult:
        """
        Return sigmoid P(Success).

        Parameters
        ----------
        obi_velocity:
            ΔOBI / short horizon (positive = growing bid pressure).
        spread_elasticity:
            current_spread / 1h_MA (≥1). Extreme values crush P toward ~10%.
        tick_acceleration:
            Mid / tick second derivative proxy (signed).
        grok_macro_bias:
            Cached macro string ∈ {BULL, BEAR, NEUTRAL, VETO}.
        """
        obi_v = _clamp(float(obi_velocity or 0.0), -3.0, 3.0)
        elast = max(0.0, float(spread_elasticity or 1.0))
        accel = _clamp(float(tick_acceleration or 0.0), -5.0, 5.0)
        atr_v = _clamp(float(atr_velocity or 0.0), -5.0, 5.0)
        bias = str(grok_macro_bias or "NEUTRAL").strip().upper() or "NEUTRAL"
        dir_u = str(direction or "").strip().upper()
        observe_volatility_features(
            str(epic or ""),
            spread_elasticity=elast,
            atr_velocity=atr_v,
        )
        thr, thr_meta = _liquidity_stress_blend(str(epic or ""))

        # Directional OBI alignment: BUY wants +velocity, SELL wants −velocity.
        if dir_u == "SELL":
            obi_aligned = -obi_v
        elif dir_u == "BUY":
            obi_aligned = obi_v
        else:
            obi_aligned = abs(obi_v)

        # Elasticity penalty: at MA (~1.0) negligible; ≥4–5× → near floor.
        elast_excess = max(0.0, elast - 1.0)

        logit = (
            W_BIAS
            + W_OBI * obi_aligned
            + W_VELOCITY * accel
            + W_ATR_VELOCITY * atr_v
            - W_ELASTICITY * elast_excess
            + _macro_logit(bias)
        )
        raw = _sigmoid(logit)

        # Blend toward safety baseline as spreads blow out (chop / flash widen).
        # elast=1 → weight 0; elast≥5 → weight 1 → P ≈ SAFETY_BASELINE (~10%).
        extreme_w = _clamp((elast - 1.15) / 3.5, 0.0, 1.0)
        p = raw * (1.0 - extreme_w) + self.safety_baseline * extreme_w
        p = _clamp(p, self.safety_baseline, 0.99)

        approved = p >= float(thr)
        reason = (
            f"sniper_ml_ok p={p:.3f} thr={thr:.2f}"
            if approved
            else f"sniper_ml_chop_isolation p={p:.3f}<{thr:.2f}"
        )
        features = {
            "obi_velocity": round(obi_v, 6),
            "spread_elasticity": round(elast, 6),
            "tick_acceleration": round(accel, 6),
            "atr_velocity": round(atr_v, 6),
            "grok_macro_bias": bias,
            "direction": dir_u,
            "elast_excess": round(elast_excess, 6),
            "extreme_weight": round(extreme_w, 6),
            "raw_sigmoid": round(raw, 6),
            "asset_class": asset_class_for_epic(epic),
            "w_obi": W_OBI,
            "w_velocity": W_VELOCITY,
            "w_elasticity": W_ELASTICITY,
            "w_atr_velocity": W_ATR_VELOCITY,
            "vol_tick_window": _VOL_TICK_WINDOW,
            **thr_meta,
        }
        result = SniperProbabilityResult(
            p_success=round(p, 6),
            approved=approved,
            threshold=float(thr),
            logit=round(logit, 6),
            features=features,
            reason=reason,
        )
        self._publish_cache(epic=str(epic or ""), result=result)
        return result

    def _publish_cache(self, *, epic: str, result: SniperProbabilityResult) -> None:
        payload = result.as_dict()
        payload["epic"] = epic
        payload["ts"] = time.time()
        with _lock:
            global _last_global
            _last_global = dict(payload)
            if epic:
                _last_by_epic[epic] = dict(payload)


_CORE = QuantumSniperMLCore()


def get_sniper_ml_core() -> QuantumSniperMLCore:
    return _CORE


def reset_sniper_ml_cache_for_tests() -> None:
    global _last_global, _last_by_epic
    with _lock:
        _last_by_epic = {}
        _last_global = {
            "p_success": None,
            "approved": False,
            "threshold": SNIPER_THRESHOLD,
            "ts": 0.0,
            "epic": "",
            "features": {},
        }
    reset_volatility_feature_history_for_tests()


def latest_sniper_ml_snapshot(*, epic: str | None = None) -> dict[str, Any]:
    """Non-blocking cache read for desk UI / ops_strip."""
    with _lock:
        if epic:
            row = _last_by_epic.get(str(epic))
            if row is not None:
                return dict(row)
        return dict(_last_global)


def evaluate_live_sniper_probability(
    epic: str,
    direction: str = "BUY",
    *,
    cfg: Any | None = None,
    quote: Any | None = None,
) -> SniperProbabilityResult:
    """Gather live features and score — fail-soft toward chop isolation."""
    obi_v = 0.0
    elast = 1.0
    accel = 0.0
    atr_v = 0.0
    bias = "NEUTRAL"
    thr = dynamic_sniper_threshold(epic)

    try:
        from execution.grok_macro_bias import resolve_grok_macro_bias

        bias = resolve_grok_macro_bias(cfg)
    except Exception:
        bias = "NEUTRAL"

    try:
        from apex.microkernel import get_microkernel

        mt = get_microkernel().micro_trend_for(str(epic or ""))
        if isinstance(mt, dict):
            if mt.get("ofi_delta") is not None:
                obi_v = float(mt.get("ofi_delta") or 0.0)
            elif mt.get("obi_ratio") is not None and mt.get("prior_obi_ratio") is not None:
                obi_v = float(mt.get("obi_ratio") or 0.0) - float(
                    mt.get("prior_obi_ratio") or 0.0
                )
            elif mt.get("obi_ratio") is not None:
                obi_v = float(mt.get("obi_ratio") or 0.0)
            if mt.get("tick_acceleration") is not None:
                accel = float(mt.get("tick_acceleration") or 0.0)
            elif mt.get("forecast_confidence") is not None:
                # Confidence × signed forecast as acceleration proxy
                conf = float(mt.get("forecast_confidence") or 0.0)
                fd = str(mt.get("forecast_direction") or "").upper()
                sign = 1.0 if fd in ("BUY", "BULL", "LONG", "UP") else (
                    -1.0 if fd in ("SELL", "BEAR", "SHORT", "DOWN") else 0.0
                )
                accel = conf * sign
    except Exception:
        pass

    bid = 0.0
    offer = 0.0
    try:
        if quote is not None:
            bid = float(getattr(quote, "bid", 0) or 0)
            offer = float(getattr(quote, "offer", 0) or 0)
        if bid <= 0 or offer <= bid:
            from system.market_data_hub import get_market_data_hub

            snap = get_market_data_hub().get_snapshot(str(epic or ""))
            if snap is not None:
                bid = float(getattr(snap, "bid", 0) or 0)
                offer = float(getattr(snap, "offer", 0) or 0)
                # Tick accel from mid history when microkernel silent
                if abs(accel) < 1e-12:
                    hist = getattr(snap, "mid_history", None) or getattr(
                        snap, "recent_mids", None
                    )
                    if hist and len(hist) >= 3:
                        a, b, c = float(hist[-3]), float(hist[-2]), float(hist[-1])
                        accel = (c - b) - (b - a)
                        if len(hist) >= 4:
                            d = float(hist[-4])
                            prior_accel = (b - a) - (a - d)
                            atr_v = accel - prior_accel
    except Exception:
        pass

    try:
        from execution.spread_elasticity import observe_spread, spread_elasticity_state

        if bid > 0 and offer > bid:
            observe_spread(str(epic or ""), bid, offer)
            st = spread_elasticity_state(str(epic or ""), bid, offer)
            elast = float(st.ratio or 1.0)
            if elast <= 0:
                elast = 1.0
    except Exception:
        elast = 1.0

    # Fail-open when feature plane is empty (no L2 OBI, no mid history, elast≈1).
    # Depthless Yahoo/rest_poll otherwise yields P≈0.5 forever → silent chop lock.
    # Crash/melt-up remains enforced by evaluate_obi_entry_filter / regime veto.
    features_unavailable = (
        abs(float(obi_v)) < 1e-12
        and abs(float(accel)) < 1e-12
        and float(elast) <= 1.0 + 1e-9
        and str(bias or "NEUTRAL").upper() in ("", "NEUTRAL")
    )
    if features_unavailable:
        result = SniperProbabilityResult(
            p_success=float(thr),
            approved=True,
            threshold=float(thr),
            logit=0.0,
            features={
                "obi_velocity": 0.0,
                "spread_elasticity": float(elast),
                "tick_acceleration": 0.0,
                "grok_macro_bias": str(bias or "NEUTRAL"),
                "direction": str(direction or "").upper(),
                "features_unavailable_fail_open": True,
                "asset_class": asset_class_for_epic(epic),
            },
            reason="sniper_ml_features_unavailable_fail_open",
        )
        get_sniper_ml_core()._publish_cache(epic=str(epic or ""), result=result)
        return result

    return get_sniper_ml_core().evaluate_entry_probability(
        obi_velocity=obi_v,
        spread_elasticity=elast,
        tick_acceleration=accel,
        grok_macro_bias=bias,
        epic=str(epic or ""),
        direction=str(direction or ""),
        atr_velocity=atr_v,
    )


def sniper_ml_desk_payload(*, epics: list[str] | None = None) -> dict[str, Any]:
    """API payload — live score hot-path epics (or return cache)."""
    universe = list(epics or [])
    if not universe:
        try:
            from runtime.dual_core_execution import ROTATION_UNIVERSE

            universe = list(ROTATION_UNIVERSE)[:8]
        except Exception:
            universe = [
                "IX.D.DOW.IFM.IP",
                "IX.D.FTSE.IFM.IP",
                "CS.D.CFPGOLD.CFP.IP",
                "CS.D.EURUSD.CFD.IP",
            ]

    by_epic: dict[str, Any] = {}
    thresholds: dict[str, float] = {}
    for epic in universe:
        try:
            result = evaluate_live_sniper_probability(epic, "BUY")
            by_epic[epic] = result.as_dict()
            thresholds[epic] = float(result.threshold)
        except Exception as exc:
            thr = dynamic_sniper_threshold(epic)
            by_epic[epic] = {
                "p_success": SAFETY_BASELINE,
                "approved": False,
                "threshold": thr,
                "reason": f"sniper_ml_eval_error:{type(exc).__name__}",
                "features": {},
            }
            thresholds[epic] = thr

    latest = latest_sniper_ml_snapshot()
    return {
        "ok": True,
        "threshold": SNIPER_THRESHOLD,
        "thresholds_by_class": {
            "INDEX": THRESHOLD_INDEX,
            "FX": THRESHOLD_FX,
            "GOLD": THRESHOLD_GOLD,
        },
        "thresholds_by_epic": thresholds,
        "safety_baseline": SAFETY_BASELINE,
        "by_epic": by_epic,
        "latest": latest,
        "ts": time.time(),
    }
