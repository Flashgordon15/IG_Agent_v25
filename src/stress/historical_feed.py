"""
Isolated historical tick generator — 5-decimal FX precision + volatility shocks.

Feeds raw bid/offer streams into microstructure engines without IG REST.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterator


class ScenarioKind(str, Enum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    FLASH_CRASH = "flash_crash"
    EXTREME_CHOP = "extreme_chop"
    GAP_OPEN = "gap_open"


@dataclass(frozen=True)
class ScenarioTick:
    """Single playback tick with broker-grade FX precision."""

    seq: int
    epic: str
    bid: float
    offer: float
    ts: float
    spread: float

    def __post_init__(self) -> None:
        if self.bid <= 0 or self.offer <= 0:
            raise ValueError(f"invalid prices bid={self.bid} offer={self.offer}")
        if self.offer < self.bid:
            raise ValueError(f"crossed market bid={self.bid} offer={self.offer}")


@dataclass
class HistoricalScenario:
    """Named price-action scenario for regression replay."""

    kind: ScenarioKind
    epic: str
    base_mid: float = 1.16000
    spread_half: float = 0.00005
    tick_dt: float = 0.2
    length: int = 120

    def ticks(self, *, t0: float = 1_700_000_000.0) -> Iterator[ScenarioTick]:
        mid = float(self.base_mid)
        for i in range(self.length):
            mid = self._next_mid(mid, i)
            bid = round(mid - self.spread_half, 5)
            offer = round(mid + self.spread_half, 5)
            yield ScenarioTick(
                seq=i,
                epic=self.epic,
                bid=bid,
                offer=offer,
                ts=t0 + i * self.tick_dt,
                spread=round(offer - bid, 5),
            )

    def _next_mid(self, mid: float, i: int) -> float:
        if self.kind == ScenarioKind.TREND_UP:
            return round(mid + 0.00003 + (i % 7) * 0.00001, 5)
        if self.kind == ScenarioKind.TREND_DOWN:
            return round(mid - 0.00003 - (i % 5) * 0.00001, 5)
        if self.kind == ScenarioKind.FLASH_CRASH:
            if 40 <= i < 50:
                return round(mid - 0.00120, 5)
            if i >= 50:
                return round(mid + 0.00015, 5)
            return round(mid + 0.00002, 5)
        if self.kind == ScenarioKind.EXTREME_CHOP:
            sign = 1 if i % 2 == 0 else -1
            amp = 0.00080 if i % 10 == 0 else 0.00025
            return round(mid + sign * amp, 5)
        if self.kind == ScenarioKind.GAP_OPEN:
            if i == 60:
                return round(mid + 0.00250, 5)
            return round(mid + 0.00001 * (i % 3), 5)
        return mid
