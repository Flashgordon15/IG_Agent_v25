"""Shared domain models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class Quote:
    """Bid/offer snapshot for a market."""

    time: datetime
    bid: float
    offer: float

    @property
    def mid(self) -> float:
        return (self.bid + self.offer) / 2

    @property
    def spread(self) -> float:
        return self.offer - self.bid


@dataclass
class TradeRecord:
    """Open or closed trade row."""

    id: int | None
    market: str
    epic: str
    side: str
    entry: float
    exit: float | None
    size: float
    stop: float
    target: float
    pnl_points: float | None
    result: str | None
    confidence: float
    adjusted_confidence: float
    setup_key: str
    dry_run: bool
    deal_reference: str | None
    notes: str
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    extra: dict[str, Any] | None = None
