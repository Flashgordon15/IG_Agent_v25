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
_BIAS_KEY = "feature_weight_bias"
_WEIGHTS_KEY = "feature_weights_v128"


class ContinuousOptimizationWorker:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._weights = np.zeros(FEATURE_STATE_DIM, dtype=_FLOAT64)
        self._bias = 0.0
        self._pending: dict[str, dict[str, Any]] = {}
        self._started = False
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
        _worker = None
