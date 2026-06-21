"""
AI Meta-Reviewer — autonomous pillar evaluation loop for 15-minute daemon cycles.

Critiques cycle outcomes, applies explicit risk-reduction scalars on loss/zero-trade
cycles, and triggers incremental twin-engine weight tuning on successful cycles.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception

_FEATURE_KEYS = ("adjusted_score", "rsi", "atr_ratio")


@dataclass
class MetaReviewResult:
    """Structured outcome of one pillar evaluation pass."""

    cycle: int
    outcome: str
    trades: int
    pnl_delta_gbp: float
    risk_scalar: float = 1.0
    vol_threshold_multiplier: float = 1.0
    size_scalar: float = 1.0
    stop_tighten_scalar: float = 1.0
    top_indicators: list[dict[str, Any]] = field(default_factory=list)
    weight_deltas: dict[str, float] = field(default_factory=dict)
    critique: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "cycle": self.cycle,
            "outcome": self.outcome,
            "trades": self.trades,
            "pnl_delta_gbp": round(float(self.pnl_delta_gbp), 4),
            "risk_scalar": round(float(self.risk_scalar), 6),
            "vol_threshold_multiplier": round(float(self.vol_threshold_multiplier), 6),
            "size_scalar": round(float(self.size_scalar), 6),
            "stop_tighten_scalar": round(float(self.stop_tighten_scalar), 6),
            "top_indicators": list(self.top_indicators),
            "weight_deltas": dict(self.weight_deltas),
            "critique": self.critique,
        }


class MetaReviewer:
    """
    Autonomous application auditor — pillar evaluation loop.

    Loss / zero-trade cycles widen volatility gates, shrink sizing, tighten stops.
    Successful cycles run incremental ``partial_fit`` on the live twin-engine model.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._risk_scalar = 1.0
        self._vol_threshold_multiplier = 1.0
        self._size_scalar = 1.0
        self._stop_tighten_scalar = 1.0
        self._last_weight_deltas: dict[str, float] = {}
        self._cycles_reviewed = 0

    @property
    def risk_scalar(self) -> float:
        with self._lock:
            return self._risk_scalar

    @property
    def size_scalar(self) -> float:
        with self._lock:
            return self._size_scalar

    def evaluate_pillar_cycle(
        self,
        stats: dict[str, Any],
        *,
        pnl_delta_gbp: float = 0.0,
    ) -> MetaReviewResult:
        """
        Run pillar evaluation for one daemon heartbeat.

        *stats* expects keys from ``execute_trading_ml_cycle`` (orders_attempted, etc.).
        """
        cycle = int(stats.get("cycle") or 0)
        trades = int(stats.get("orders_attempted") or stats.get("gates_passed") or 0)
        pnl = float(pnl_delta_gbp)

        with self._lock:
            self._cycles_reviewed += 1
            if trades <= 0 or pnl < 0.0:
                result = self._apply_risk_reduction_locked(
                    cycle=cycle,
                    trades=trades,
                    pnl_delta_gbp=pnl,
                    reason="zero_trades" if trades <= 0 else "cycle_loss",
                )
            else:
                result = self._apply_success_finetune_locked(
                    cycle=cycle,
                    trades=trades,
                    pnl_delta_gbp=pnl,
                )

        try:
            from system.identity.state_cache import get_live_state_cache

            get_live_state_cache().apply_meta_review(result.as_dict())
        except Exception as exc:
            log_guarded_exception("meta_reviewer_state_cache", exc)

        log_engine(
            "MetaReviewer: pillar evaluation "
            f"cycle={cycle} outcome={result.outcome} trades={trades} "
            f"pnl={pnl:.2f} risk={result.risk_scalar:.4f} "
            f"vol×={result.vol_threshold_multiplier:.4f} "
            f"size×={result.size_scalar:.4f} stop×={result.stop_tighten_scalar:.4f}"
        )
        if result.weight_deltas:
            delta_parts = ", ".join(
                f"{k}={v:+.6f}" for k, v in sorted(result.weight_deltas.items())
            )
            log_engine(f"MetaReviewer: partial_fit weight deltas — {delta_parts}")
        if result.critique:
            log_engine(f"MetaReviewer: critique — {result.critique}")

        return result

    def _apply_risk_reduction_locked(
        self,
        *,
        cycle: int,
        trades: int,
        pnl_delta_gbp: float,
        reason: str,
    ) -> MetaReviewResult:
        """Widen volatility thresholds, reduce sizing, tighten stop limits."""
        self._risk_scalar = max(0.50, self._risk_scalar * 0.92)
        self._vol_threshold_multiplier = min(2.50, self._vol_threshold_multiplier * 1.08)
        self._size_scalar = max(0.40, self._size_scalar * 0.90)
        self._stop_tighten_scalar = max(0.60, self._stop_tighten_scalar * 0.95)

        critique = (
            f"Pillar FAIL ({reason}): zero trades or negative P&L — "
            f"widening vol gate ×{self._vol_threshold_multiplier:.3f}, "
            f"size scalar → {self._size_scalar:.3f}, "
            f"stop tighten → {self._stop_tighten_scalar:.3f}"
        )

        top = self._build_indicator_snapshot(self._last_weight_deltas)

        return MetaReviewResult(
            cycle=cycle,
            outcome=reason,
            trades=trades,
            pnl_delta_gbp=pnl_delta_gbp,
            risk_scalar=self._risk_scalar,
            vol_threshold_multiplier=self._vol_threshold_multiplier,
            size_scalar=self._size_scalar,
            stop_tighten_scalar=self._stop_tighten_scalar,
            top_indicators=top,
            weight_deltas=dict(self._last_weight_deltas),
            critique=critique,
        )

    def _apply_success_finetune_locked(
        self,
        *,
        cycle: int,
        trades: int,
        pnl_delta_gbp: float,
    ) -> MetaReviewResult:
        """Incremental online weight adjustment on successful cycles."""
        self._risk_scalar = min(1.25, self._risk_scalar * 1.02)
        self._size_scalar = min(1.20, self._size_scalar * 1.01)

        gradients = self._success_gradients(trades=trades, pnl_delta_gbp=pnl_delta_gbp)
        weight_deltas: dict[str, float] = {}
        try:
            from system.ml.twin_engine_core import get_twin_engine_core

            weight_deltas = get_twin_engine_core().live.partial_fit(
                gradients,
                learning_rate=0.008,
            )
            self._last_weight_deltas = dict(weight_deltas)
        except Exception as exc:
            log_guarded_exception("meta_reviewer_partial_fit", exc)

        top = self._build_indicator_snapshot(weight_deltas)
        critique = (
            f"Pillar PASS: {trades} trade(s), P&L {pnl_delta_gbp:+.2f} GBP — "
            f"incremental partial_fit applied"
        )

        return MetaReviewResult(
            cycle=cycle,
            outcome="success_finetune",
            trades=trades,
            pnl_delta_gbp=pnl_delta_gbp,
            risk_scalar=self._risk_scalar,
            vol_threshold_multiplier=self._vol_threshold_multiplier,
            size_scalar=self._size_scalar,
            stop_tighten_scalar=self._stop_tighten_scalar,
            top_indicators=top,
            weight_deltas=weight_deltas,
            critique=critique,
        )

    @staticmethod
    def _success_gradients(*, trades: int, pnl_delta_gbp: float) -> dict[str, float]:
        scale = min(1.0, max(0.05, float(trades) * 0.05 + max(0.0, pnl_delta_gbp) * 0.01))
        return {
            "adjusted_score": 0.12 * scale,
            "rsi": 0.06 * scale,
            "atr_ratio": -0.03 * scale,
        }

    def _build_indicator_snapshot(self, deltas: dict[str, float]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            from system.ml.twin_engine_core import get_twin_engine_core

            live = get_twin_engine_core().live.weights_snapshot()
            coeffs = dict(live.coeffs)
        except Exception:
            coeffs = {k: 0.0 for k in _FEATURE_KEYS}

        for key in _FEATURE_KEYS:
            delta = float(deltas.get(key) or 0.0)
            weight = float(coeffs.get(key) or 0.0)
            direction = "up" if delta > 0 else ("down" if delta < 0 else "flat")
            rows.append(
                {
                    "name": key,
                    "weight": round(weight, 6),
                    "delta": round(delta, 6),
                    "direction": direction,
                    "magnitude": round(abs(delta), 6),
                }
            )
        rows.sort(key=lambda r: float(r.get("magnitude") or 0.0), reverse=True)
        return rows[:5]

    def telemetry_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "cycles_reviewed": self._cycles_reviewed,
                "risk_scalar": self._risk_scalar,
                "vol_threshold_multiplier": self._vol_threshold_multiplier,
                "size_scalar": self._size_scalar,
                "stop_tighten_scalar": self._stop_tighten_scalar,
                "last_weight_deltas": dict(self._last_weight_deltas),
            }


_reviewer_singleton: MetaReviewer | None = None
_reviewer_lock = threading.Lock()


def get_meta_reviewer() -> MetaReviewer:
    global _reviewer_singleton
    with _reviewer_lock:
        if _reviewer_singleton is None:
            _reviewer_singleton = MetaReviewer()
        return _reviewer_singleton


def reset_meta_reviewer() -> None:
    """Tests only."""
    global _reviewer_singleton
    with _reviewer_lock:
        _reviewer_singleton = None
