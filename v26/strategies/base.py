"""Strategy plugin contract for v26."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ShadowIntent:
    """v26 shadow output — no broker orders."""

    strategy_id: str
    epic: str
    market: str
    session: str
    direction: str
    would_trade: bool
    confidence: float
    setup_key: str
    source_ts: str
    reason: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_event_row(self) -> dict[str, Any]:
        return {
            "contract_version": "1.0",
            "event_type": "shadow_intent",
            "strategy_id": self.strategy_id,
            "ts": self.source_ts,
            "epic": self.epic,
            "market": self.market,
            "session": self.session,
            "payload": {
                "direction": self.direction,
                "would_trade": self.would_trade,
                "confidence": self.confidence,
                "setup_key": self.setup_key,
                "reason": self.reason,
                **self.payload,
            },
        }


class StrategyPlugin(Protocol):
    strategy_id: str

    def evaluate_feeder_event(self, row: dict[str, Any]) -> ShadowIntent | None: ...
