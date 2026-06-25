"""
ContinuousOptimizationWorker — triage_v30.db feature persistence + online back-learning.

Serializes 128-dim vectors on fill; gradient descent devalues weights on closed losses.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import numpy as np

from signals.feature_state import FEATURE_STATE_DIM
from system.engine_log import log_engine

_FLOAT64 = np.float64
_LEARNING_RATE = 0.05
_SGD_LR = 0.02
_BIAS_KEY = "feature_weight_bias"
_WEIGHTS_KEY = "feature_weights_v128"
_SGD_SLIPPAGE_KEY = "sgd_slippage_weight"
_SGD_LATENCY_KEY = "sgd_latency_weight"
_SGD_BIAS_KEY = "sgd_execution_bias"
_FAILOVER_LOT = 1.0


class ContinuousOptimizationWorker:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._weights = np.zeros(FEATURE_STATE_DIM, dtype=_FLOAT64)
        self._bias = 0.0
        self._pending: dict[str, dict[str, Any]] = {}
        self._started = False
        self._sovereignty_active = False
        self._sgd_w = np.zeros(2, dtype=_FLOAT64)  # [slippage_pts, latency_ms_norm]
        self._sgd_bias = 0.0
        self._last_alpha_weight = 0.5
        self._load_weights()

    def start(self) -> None:
        with self._lock:
            self._started = True
        log_engine("ContinuousOptimizationWorker: online back-learning armed")

    def predict(self, vector: np.ndarray | list[float]) -> float:
        vec = np.asarray(vector, dtype=_FLOAT64).reshape(-1)
        if vec.size != FEATURE_STATE_DIM:
            vec = np.pad(vec, (0, max(0, FEATURE_STATE_DIM - vec.size)))[:FEATURE_STATE_DIM]
        with self._lock:
            logit = float(np.dot(self._weights, vec) + self._bias)
        return float(1.0 / (1.0 + np.exp(-logit)))

    def record_execution(
        self,
        *,
        deal_ref: str,
        epic: str,
        direction: str,
        win_probability: float,
        feature_vector: np.ndarray | list[float],
        model_verdict: str = "",
    ) -> None:
        """Persist feature vector + model confidence at fill millisecond."""
        ref = str(deal_ref or f"anon-{time.time_ns()}").strip()
        vec = np.asarray(feature_vector, dtype=_FLOAT64).reshape(-1)
        if vec.size != FEATURE_STATE_DIM:
            vec = np.pad(vec, (0, max(0, FEATURE_STATE_DIM - vec.size)))[:FEATURE_STATE_DIM]

        payload = {
            "kind": "ml_feature_execution",
            "timestamp": time.time(),
            "deal_ref": ref,
            "epic": str(epic or ""),
            "direction": str(direction or "").upper(),
            "win_probability": float(win_probability),
            "model_verdict": str(model_verdict or ""),
            "feature_vector": vec.tobytes(),
        }
        try:
            from analytics.triage_logger import dispatch_triage_event

            dispatch_triage_event(payload)
        except Exception as exc:
            log_engine(
                f"ContinuousOptimizationWorker persist: {type(exc).__name__}: {exc}"
            )

        with self._lock:
            self._pending[ref] = {
                "vector": vec.copy(),
                "win_probability": float(win_probability),
                "epic": str(epic or ""),
                "direction": str(direction or "").upper(),
            }

    def on_trade_closed(
        self,
        *,
        deal_ref: str,
        result: str,
        net_pnl: float,
    ) -> None:
        """Gradient descent pass — devalue features that fooled the model on losses."""
        ref = str(deal_ref or "").strip()
        with self._lock:
            row = self._pending.pop(ref, None)
        if row is None:
            return

        vec = np.asarray(row["vector"], dtype=_FLOAT64)
        is_loss = str(result or "").upper() == "LOSS" or float(net_pnl) < 0.0
        target = 0.0 if is_loss else 1.0
        self._gradient_descent_step(vec, target=target)
        if is_loss:
            log_engine(
                f"ContinuousOptimizationWorker: loss back-learn deal={ref} "
                f"pnl={float(net_pnl):.2f} — feature weights adjusted"
            )
        self._save_weights()

    def on_certification_cycle_closed(
        self,
        cycle: int,
        *,
        win: bool,
        epic: str = "",
        net_pnl: float = 0.0,
    ) -> float:
        """
        Certification harness hook — shift entry Z-score proxy (bias) from cycle feedback.
        """
        target = 1.0 if win or float(net_pnl) > 0 else 0.0
        with self._lock:
            pred = float(1.0 / (1.0 + np.exp(-self._bias)))
            error = target - pred
            self._bias += _LEARNING_RATE * error
            self._bias = float(max(-2.0, min(2.0, self._bias)))
            z_threshold = float(0.5 - self._bias * 0.15)
            z_threshold = max(0.35, min(0.95, z_threshold))
        self._save_weights()
        try:
            from analytics.triage_logger import write_triage_meta

            write_triage_meta("entry_z_score_threshold", json.dumps(z_threshold))
        except Exception:
            pass
        log_engine(
            f"[ML OPTIMIZER SHIFT] Cycle {int(cycle)} closed. "
            f"Updating entry Z-Score threshold to {z_threshold:.3f} "
            f"based on learning feedback."
        )
        return z_threshold

    def seize_strategy_sovereignty(
        self,
        epic: str,
        *,
        spread: float,
        volatility_z: float,
    ) -> dict[str, Any]:
        """ML seizes strategy lead — dynamic Z/TP/SL from live spread volatility."""
        from runtime.dual_core_execution import (
            MICRO_SL_POINTS,
            MICRO_TP_POINTS,
            MICRO_Z_THRESHOLD,
            apply_ml_cognitive_overrides,
            epic_display_name,
        )

        spread_norm = min(1.0, max(0.0, float(spread)))
        with self._lock:
            entry_z = float(0.5 - self._bias * 0.2 + float(volatility_z) * 0.08)
            entry_z = max(0.35, min(float(MICRO_Z_THRESHOLD), entry_z))
        tp_pts = max(1.0, float(MICRO_TP_POINTS) * (1.0 + spread_norm * 0.5))
        sl_pts = max(1.5, float(MICRO_SL_POINTS) * (1.0 + spread_norm * 0.35))
        overrides = {
            "micro_z_threshold": round(entry_z, 4),
            "micro_tp_points": round(tp_pts, 3),
            "micro_sl_points": round(sl_pts, 3),
            "spread_volatility_norm": round(spread_norm, 4),
        }
        apply_ml_cognitive_overrides(epic, overrides)
        label = epic_display_name(epic)
        log_engine(
            f"[ML COGNITIVE CONTROLLER] Seizing strategy lead for asset {label}. "
            f"Dynamically adjusting entry parameter thresholds."
        )
        return overrides

    def ingest_execution_metrics(
        self,
        *,
        slippage_pts: float,
        latency_ms: float,
        spread_pts: float = 0.0,
    ) -> float:
        """Live SGD regressor — slippage + latency → spread-volatility target."""
        latency_norm = min(1.0, max(0.0, float(latency_ms) / 500.0))
        slip_norm = min(1.0, max(0.0, float(slippage_pts) / max(float(spread_pts), 1e-6)))
        features = np.array([slip_norm, latency_norm], dtype=_FLOAT64)
        spread_target = min(1.0, max(0.0, float(spread_pts) / 10.0))
        with self._lock:
            pred = float(np.dot(self._sgd_w, features) + self._sgd_bias)
            pred_sig = float(1.0 / (1.0 + np.exp(-pred)))
            error = spread_target - pred_sig
            self._sgd_w += _SGD_LR * error * features
            self._sgd_bias += _SGD_LR * error
            self._sgd_w = np.clip(self._sgd_w, -2.0, 2.0)
            self._sgd_bias = float(max(-2.0, min(2.0, self._sgd_bias)))
            self._last_alpha_weight = round(pred_sig, 4)
        self._save_sgd_weights()
        return self._last_alpha_weight

    def get_ml_alpha_weight(self) -> float:
        with self._lock:
            return round(float(self._last_alpha_weight), 4)

    def is_sovereignty_active(self) -> bool:
        with self._lock:
            return bool(self._sovereignty_active)

    def run_failover_sovereignty(
        self,
        *,
        epic: str,
        spread: float,
        slippage_pts: float = 0.0,
        latency_ms: float = 0.0,
        volatility_z: float = 0.0,
    ) -> dict[str, Any]:
        """Complete execution-parameter control when forex failover is locked."""
        from runtime.dual_core_execution import (
            FAILOVER_BOTTOM_EPIC,
            FAILOVER_TOP_EPIC,
            is_forex_failover_active,
        )

        if not is_forex_failover_active():
            return {}

        alpha = self.ingest_execution_metrics(
            slippage_pts=slippage_pts,
            latency_ms=latency_ms,
            spread_pts=spread,
        )
        spread_norm = min(1.0, max(0.0, float(spread)))
        with self._lock:
            entry_z = float(0.5 - self._bias * 0.2 + float(volatility_z) * 0.08 - self._sgd_bias * 0.1)
            entry_z = max(0.35, min(2.44, entry_z))
            tp_pts = max(1.0, 1.5 * (1.0 + spread_norm * 0.5))
            sl_pts = max(1.5, 2.0 * (1.0 + spread_norm * 0.35 + self._sgd_bias * 0.05))
            self._sovereignty_active = True

        overrides = {
            "micro_z_threshold": round(entry_z, 4),
            "micro_tp_points": round(tp_pts, 3),
            "micro_sl_points": round(sl_pts, 3),
            "spread_volatility_norm": round(spread_norm, 4),
            "ml_alpha_weight": alpha,
            "target_lot": _FAILOVER_LOT,
            "failover_forex_locked": True,
        }
        from runtime.dual_core_execution import apply_ml_cognitive_overrides, epic_display_name

        apply_ml_cognitive_overrides(epic or FAILOVER_TOP_EPIC, overrides)
        # Mirror overrides to bottom slot for stacked parity
        apply_ml_cognitive_overrides(FAILOVER_BOTTOM_EPIC, overrides)
        label = epic_display_name(epic or FAILOVER_TOP_EPIC)
        log_engine(
            f"[ML SOVEREIGNTY] Forex failover locked — {label} "
            f"Z={entry_z:.3f} TP={tp_pts:.2f} SL={sl_pts:.2f} α={alpha:.4f} lot={_FAILOVER_LOT}"
        )
        try:
            from analytics.triage_logger import write_triage_meta

            write_triage_meta("entry_z_score_threshold", json.dumps(entry_z))
            write_triage_meta("ml_alpha_weight", json.dumps(alpha))
        except Exception:
            pass
        return overrides

    def _gradient_descent_step(self, vector: np.ndarray, *, target: float) -> None:
        vec = np.asarray(vector, dtype=_FLOAT64).reshape(-1)
        with self._lock:
            pred = float(1.0 / (1.0 + np.exp(-(np.dot(self._weights, vec) + self._bias))))
            error = float(target) - pred
            self._weights += _LEARNING_RATE * error * vec
            self._bias += _LEARNING_RATE * error
            # Keep weights bounded
            self._weights = np.clip(self._weights, -2.0, 2.0)
            self._bias = float(max(-2.0, min(2.0, self._bias)))

    def _load_weights(self) -> None:
        try:
            from analytics.triage_logger import read_triage_meta

            raw_w = read_triage_meta(_WEIGHTS_KEY)
            raw_b = read_triage_meta(_BIAS_KEY)
            if raw_w:
                arr = json.loads(raw_w)
                w = np.asarray(arr, dtype=_FLOAT64)
                if w.size == FEATURE_STATE_DIM:
                    self._weights = w
            if raw_b:
                self._bias = float(json.loads(raw_b))
            raw_sw = read_triage_meta(_SGD_SLIPPAGE_KEY)
            raw_lw = read_triage_meta(_SGD_LATENCY_KEY)
            raw_sb = read_triage_meta(_SGD_BIAS_KEY)
            if raw_sw and raw_lw:
                self._sgd_w = np.array(
                    [float(json.loads(raw_sw)), float(json.loads(raw_lw))], dtype=_FLOAT64
                )
            if raw_sb:
                self._sgd_bias = float(json.loads(raw_sb))
        except Exception:
            pass

    def _save_sgd_weights(self) -> None:
        try:
            from analytics.triage_logger import write_triage_meta

            with self._lock:
                write_triage_meta(_SGD_SLIPPAGE_KEY, json.dumps(float(self._sgd_w[0])))
                write_triage_meta(_SGD_LATENCY_KEY, json.dumps(float(self._sgd_w[1])))
                write_triage_meta(_SGD_BIAS_KEY, json.dumps(float(self._sgd_bias)))
        except Exception:
            pass

    def _save_weights(self) -> None:
        try:
            from analytics.triage_logger import write_triage_meta

            with self._lock:
                write_triage_meta(
                    _WEIGHTS_KEY,
                    json.dumps([float(x) for x in self._weights.tolist()]),
                )
                write_triage_meta(_BIAS_KEY, json.dumps(float(self._bias)))
        except Exception as exc:
            log_engine(
                f"ContinuousOptimizationWorker save weights: {type(exc).__name__}: {exc}"
            )


_worker: ContinuousOptimizationWorker | None = None
_worker_lock = threading.Lock()


def get_continuous_optimization_worker() -> ContinuousOptimizationWorker:
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = ContinuousOptimizationWorker()
            _worker.start()
        return _worker


def reset_continuous_optimization_worker_for_tests() -> None:
    global _worker
    with _worker_lock:
        if _worker is not None:
            _worker._sovereignty_active = False
            _worker._sgd_w = np.zeros(2, dtype=_FLOAT64)
            _worker._sgd_bias = 0.0
            _worker._last_alpha_weight = 0.5
        _worker = None
