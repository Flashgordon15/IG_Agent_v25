"""
Build orchestration trading loop and execution stack for v25 main entry.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from data.journal import DecisionJournal
from data.learning_store import LearningStore
from data.models import Quote
from execution.execution_engine import ExecutionEngine
from execution.trading_loop import TradingLoop as ExecutionTickLoop
from execution.types import ExecutionMode
from signals.signal_engine import SignalEngine
from system.config import Config
from system.engine_log import log_engine
from system.market_data_hub import get_market_data_hub
from trading.environment_scorer import EnvironmentScorer
from trading.instrument_registry import InstrumentRegistry
from trading.points_engine import PointsEngine
from trading.session_manager import SessionManager
from trading.trading_loop import TradingLoop as AgentTradingLoop


def _resolve_instrument(cfg: Config) -> tuple[str, str]:
    reg = InstrumentRegistry(cfg.as_dict())
    enabled = reg.get_enabled()
    if enabled:
        inst = enabled[0]
        return str(inst.get("name") or cfg.market_search or "Market"), str(
            inst.get("epic") or cfg.epic
        )
    return str(cfg.market_search or "Market"), str(cfg.epic)


def build_trading_loop(
    cfg: Config,
    *,
    rest_client: Any | None = None,
    mode: ExecutionMode | None = None,
) -> AgentTradingLoop:
    """Wire orchestrator loop with execution process_tick (enabled Japan 225 only)."""
    market, epic = _resolve_instrument(cfg)
    store = LearningStore(str(cfg.learning_db))
    signal_engine = SignalEngine(cfg, store)
    points_engine = PointsEngine(store)
    env_scorer = EnvironmentScorer(cfg, signal_engine)
    session_manager = SessionManager(
        epic,
        market=market,
        points_engine=points_engine,
        environment_scorer=env_scorer,
        signal_engine=signal_engine,
    )

    exec_mode = mode
    if exec_mode is None:
        exec_mode = ExecutionMode.DEMO if rest_client is not None else ExecutionMode.TEST

    trade_tracker_store = store
    from execution.trade_tracker import TradeTracker

    tracker = TradeTracker(trade_tracker_store, prefer_ig=rest_client is not None)
    exec_engine = ExecutionEngine(
        mode=exec_mode,
        config=cfg,
        store=store,
        rest_client=rest_client,
        trade_tracker=tracker,
    )
    journal_path = str(cfg.get("decision_log_file", "") or "")
    journal = DecisionJournal(journal_path) if journal_path else None
    execution_loop = ExecutionTickLoop(
        signal_engine=signal_engine,
        execution_engine=exec_engine,
        journal=journal,
        auto_trade=cfg.auto_trade_enabled,
    )

    hub = get_market_data_hub()
    if rest_client is not None:
        hub.attach_rest(rest_client)

    def quote_source() -> Quote | None:
        snap = hub.get_snapshot(epic)
        if snap is None and rest_client is not None:
            snap = hub.fetch_if_stale(epic, min_interval=float(cfg.refresh_seconds))
        if snap is None or snap.bid <= 0:
            return None
        return snap.to_quote()

    return AgentTradingLoop(
        cfg,
        market=market,
        epic=epic,
        session_manager=session_manager,
        environment_scorer=env_scorer,
        points_engine=points_engine,
        signal_engine=signal_engine,
        execution_loop=execution_loop,
        quote_source=quote_source,
        learning_store=store,
    )
