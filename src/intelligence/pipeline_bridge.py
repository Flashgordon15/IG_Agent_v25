"""
Intelligence layer pipeline bridge — plugin façade for unified QMM execution.

Read-only snapshots for trading_loop / execution_engine integration.
Does not modify boot coordinator or agent_bootstrap initialization.
"""

from __future__ import annotations

import threading
from typing import Any

from intelligence.alpha_trail import AlphaOptimisedTrailEngine, AlphaTrailPosition
from intelligence.intelligence_worker import get_intelligence_worker
from intelligence.types import (
    AlphaTrailVerdict,
    MicrostructureVerdict,
    SpreadForecastVerdict,
)

_layer_lock = threading.Lock()
_layer: IntelligenceLayer | None = None


class IntelligenceLayer:
    """
    Plugin façade — inject intelligence verdicts into gate/execution layers.

    Usage (future bind):
        layer = get_intelligence_layer()
        spread = layer.spread_verdict(epic)
        if spread.blocked: ...
        micro = layer.microstructure_verdict(epic)
        trail = layer.alpha_trail(position, bid, offer, micro_regime=micro.regime)
    """

    def __init__(self, worker: Any | None = None) -> None:
        self._worker = worker or get_intelligence_worker()

    def on_hub_tick(
        self,
        epic: str,
        *,
        bid: float,
        offer: float,
        ts: float | None = None,
    ) -> None:
        """Ingress hub tick, flush stale cache, and recompute micro-regime immediately."""
        self._worker.enqueue_tick(epic, bid=bid, offer=offer, ts=ts)
        self._worker.refresh_epic(epic, ts=ts)

    def refresh(self, epic: str | None = None) -> int:
        """Force compute pass — safe from background threads only."""
        if epic:
            self._worker.enqueue_tick(
                epic,
                bid=1.0,
                offer=1.0,
                ts=None,
            )
        return self._worker.tick_once()

    def spread_verdict(self, epic: str) -> SpreadForecastVerdict:
        snap = self._worker.get_snapshot()
        cached = snap.spread.get(str(epic or "").strip())
        if cached is not None:
            return cached
        return self._worker.spread_model.compute(epic)

    def microstructure_verdict(self, epic: str) -> MicrostructureVerdict:
        key = str(epic or "").strip()
        if key and self._worker.micro_model.tick_count(key) > 0:
            return self._worker.micro_model.classify(key)
        snap = self._worker.get_snapshot()
        cached = snap.microstructure.get(key)
        if cached is not None:
            return cached
        return self._worker.micro_model.classify(epic)

    def alpha_trail(
        self,
        pos: AlphaTrailPosition,
        *,
        bid: float,
        offer: float,
        session_profit_pts: float = 0.0,
        trigger_atr_mult: float = 0.35,
    ) -> AlphaTrailVerdict:
        micro = self.microstructure_verdict(pos.epic)
        target_factor = 1.0
        capital_preservation = False
        try:
            from intelligence.target_engine import get_target_engine, target_engine_enabled
            from system.config_loader import get_config

            cfg = get_config()
            if target_engine_enabled(cfg):
                te = get_target_engine()
                te.refresh()
                target_factor = te.risk_compression_factor()
                capital_preservation = te.capital_preservation_mode()
        except Exception:
            pass
        return self._worker.trail_engine.compute(
            pos,
            bid=bid,
            offer=offer,
            micro_regime=micro.regime,
            session_profit_pts=session_profit_pts,
            trigger_atr_mult=trigger_atr_mult,
            risk_compression_factor=target_factor,
            capital_preservation=capital_preservation,
        )

    def execution_adjustments(self, epic: str) -> dict[str, Any]:
        """
        Unified plugin payload for execution router (offset widen + throttle).

        Returns dict safe to merge into gate_execution_params when bound live.
        """
        spread = self.spread_verdict(epic)
        micro = self.microstructure_verdict(epic)
        return {
            "intelligence_spread_blocked": spread.blocked,
            "intelligence_throttle_factor": spread.throttle_factor,
            "intelligence_offset_widen_pts": spread.offset_widen_pts,
            "intelligence_spread_z": spread.z_score,
            "intelligence_micro_regime": micro.regime,
            "intelligence_micro_confidence": micro.confidence,
        }

    def snapshot(self) -> dict[str, Any]:
        snap = self._worker.get_snapshot()
        return snap.as_dict()


def get_intelligence_layer() -> IntelligenceLayer:
    global _layer
    with _layer_lock:
        if _layer is None:
            _layer = IntelligenceLayer()
        return _layer


def reset_intelligence_layer_for_tests() -> None:
    global _layer
    with _layer_lock:
        _layer = None
