"""
Phase A — run one TradingLoop per enabled instrument; shared PointsEngine and dashboard tick.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from api.snapshot_store import publish_tick
from system.config import Config
from system.engine_log import log_engine
from trading.instrument_registry import InstrumentRegistry
from trading.trading_loop import TradingLoop


class MarketOrchestrator:
    """Starts/stops per-epic loops and publishes a merged multi-market tick."""

    def __init__(
        self,
        config: Config,
        loops: list[TradingLoop],
        *,
        primary_epic: str = "",
    ) -> None:
        self._config = config
        self._loops = list(loops)
        self._primary_epic = primary_epic or (loops[0]._epic if loops else "")
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._running = False

    @property
    def config(self) -> Config:
        return self._config

    @property
    def loops(self) -> list[TradingLoop]:
        return list(self._loops)

    @property
    def primary(self) -> TradingLoop | None:
        if not self._loops:
            return None
        for loop in self._loops:
            if loop._epic == self._primary_epic:
                return loop
        return self._loops[0]

    @property
    def last_context(self) -> Any:
        loop = self.primary
        return loop.last_context if loop is not None else None

    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._stop.clear()
        self._running = True
        for loop in self._loops:
            loop.start()
        log_engine(
            f"market_orchestrator started ({len(self._loops)} loops) "
            f"primary={self._primary_epic}"
        )

    def stop(self) -> None:
        self._stop.set()
        for loop in self._loops:
            loop.stop()
        self._running = False
        log_engine("market_orchestrator stopped")

    def run_once(self) -> None:
        """Run one tick on each loop (tests)."""
        for loop in self._loops:
            loop.run_once()
        self._publish_merged()

    def on_market_snapshot(self, payload: dict[str, Any]) -> None:
        epic = str(payload.get("epic") or "").strip()
        if not epic:
            return
        with self._lock:
            self._snapshots[epic] = payload
        self._publish_merged()

    def _publish_merged(self) -> None:
        with self._lock:
            markets = {k: dict(v) for k, v in self._snapshots.items()}
        if not markets:
            return
        primary = markets.get(self._primary_epic) or next(iter(markets.values()))
        merged = dict(primary)
        merged["markets"] = markets
        merged["enabled_epics"] = list(markets.keys())
        merged["selected_epic"] = self._primary_epic
        merged["orchestrator"] = {
            "loop_count": len(self._loops),
            "primary_epic": self._primary_epic,
        }
        try:
            publish_tick(merged)
        except Exception as e:
            log_engine(f"publish_tick merged failed: {type(e).__name__}: {e}")


def attach_snapshot_handlers(orchestrator: MarketOrchestrator) -> None:
    """Wire each loop to feed the orchestrator merge publisher."""
    handler: Callable[[dict[str, Any]], None] = orchestrator.on_market_snapshot
    for loop in orchestrator.loops:
        loop._on_snapshot = handler
        loop._publish_snapshots = False
