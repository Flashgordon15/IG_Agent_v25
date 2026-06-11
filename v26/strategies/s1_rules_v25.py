"""
S1 — v25 rules parity layer (Phase 1).

Reads v25 feeder ``signal_eval`` events and emits shadow intents tagged S1_rules_v25.
Phase 1 mirrors v25 gate outcome on signal_confidence (would_fire); independent
re-scoring via SignalEngine comes in Phase 2.
"""

from __future__ import annotations

from typing import Any

from strategies.base import ShadowIntent

STRATEGY_ID = "S1_rules_v25"


class S1RulesV25:
    strategy_id = STRATEGY_ID

    def evaluate_feeder_event(self, row: dict[str, Any]) -> ShadowIntent | None:
        if str(row.get("event_type") or "") != "signal_eval":
            return None
        payload = row.get("payload") or {}
        direction = str(payload.get("direction") or "WAIT").upper()
        would_fire = bool(payload.get("would_fire"))
        try:
            confidence = float(payload.get("adjusted_score") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        would_trade = would_fire and direction in ("BUY", "SELL")
        parity_mode = "feeder_signal_eval"
        reason = str(payload.get("reason") or "")
        if would_trade:
            reason = reason or "S1 parity: v25 signal_confidence passed"
        elif would_fire and direction not in ("BUY", "SELL"):
            reason = "S1: would_fire but direction WAIT"
        else:
            reason = reason or "S1: v25 signal_confidence blocked"

        # Phase 2: independent shadow learning when rules score is strong but v25 blocked.
        try:
            import sys
            from pathlib import Path

            src = Path(__file__).resolve().parents[2] / "src"
            if str(src) not in sys.path:
                sys.path.insert(0, str(src))
            from system.v26_config import s1_settings

            s1 = s1_settings()
            thr = float(s1.get("independent_threshold") or 72.0)
            if (
                s1.get("independent_enabled")
                and not would_trade
                and direction in ("BUY", "SELL")
                and confidence >= thr
            ):
                would_trade = True
                parity_mode = "independent_rescore"
                reason = f"S1 Phase2: score {confidence:.1f} ≥ {thr:.0f} (v25 blocked)"
        except Exception:
            pass

        return ShadowIntent(
            strategy_id=self.strategy_id,
            epic=str(row.get("epic") or ""),
            market=str(row.get("market") or ""),
            session=str(row.get("session") or ""),
            direction=direction,
            would_trade=would_trade,
            confidence=confidence,
            setup_key=str(payload.get("setup_key") or ""),
            source_ts=str(row.get("ts") or ""),
            reason=reason,
            payload={
                "gates_passed": payload.get("gates_passed") or [],
                "ml_probability": payload.get("ml_probability"),
                "parity_mode": parity_mode,
            },
        )
