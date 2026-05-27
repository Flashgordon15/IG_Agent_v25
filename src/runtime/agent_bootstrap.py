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

_stream_client: Any | None = None
_position_sync: Any | None = None


def _resolve_instrument(cfg: Config) -> tuple[str, str]:
    reg = InstrumentRegistry(cfg.as_dict())
    enabled = reg.get_enabled()
    if enabled:
        inst = enabled[0]
        return str(inst.get("name") or cfg.market_search or "Market"), str(
            inst.get("epic") or cfg.epic
        )
    return str(cfg.market_search or "Market"), str(cfg.epic)


def start_ig_position_sync(
    rest_client: Any,
    store: Any,
    tracker: Any,
    *,
    epic: str,
    interval_seconds: float,
) -> Any | None:
    """Start background IG open-position sync and attach to trade tracker."""
    global _position_sync
    if rest_client is None or store is None:
        return None
    try:
        from runtime.ig_position_sync import IgPositionSync

        sync = IgPositionSync(
            rest_client,
            store,
            epic=epic,
            interval_seconds=float(interval_seconds),
        )
        tracker.attach_sync(sync)
        sync.start()
        _position_sync = sync
        log_engine(f"IG position sync attached epic={epic}")
        return sync
    except Exception as e:
        log_engine(f"IG position sync start failed: {type(e).__name__}: {e}")
        return None


def stop_ig_position_sync(sync: Any | None = None) -> None:
    """Stop background position sync (process shutdown)."""
    global _position_sync
    target = sync if sync is not None else _position_sync
    if target is None:
        return
    try:
        target.stop()
    except Exception as e:
        log_engine(f"IG position sync stop failed: {type(e).__name__}: {e}")
    if target is _position_sync:
        _position_sync = None


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
    env_scorer = EnvironmentScorer(
        signal_engine, config=cfg, rest_client=rest_client, epic=epic
    )
    session_manager = SessionManager(
        epic,
        market=market,
        points_engine=points_engine,
        environment_scorer=env_scorer,
        signal_engine=signal_engine,
        rest_client=rest_client,
    )

    exec_mode = mode
    if exec_mode is None:
        exec_mode = ExecutionMode.DEMO if rest_client is not None else ExecutionMode.TEST

    trade_tracker_store = store
    from execution.trade_tracker import TradeTracker

    tracker = TradeTracker(trade_tracker_store, prefer_ig=rest_client is not None)
    position_sync = None
    if rest_client is not None:
        position_sync = start_ig_position_sync(
            rest_client,
            store,
            tracker,
            epic=epic,
            interval_seconds=float(cfg.position_sync_seconds),
        )
    exec_engine = ExecutionEngine(
        mode=exec_mode,
        config=cfg,
        store=store,
        rest_client=rest_client,
        trade_tracker=tracker,
        points_engine=points_engine,
        environment_scorer=env_scorer,
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

    interval = float(cfg.refresh_seconds)

    def quote_source() -> Quote | None:
        if rest_client is not None:
            snap = hub.fetch_if_stale(epic, min_interval=interval)
        else:
            snap = hub.get_snapshot(epic)
        if snap is None or snap.bid <= 0:
            return None
        return snap.to_quote()

    loop = AgentTradingLoop(
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
    loop._position_sync = position_sync  # noqa: SLF001 — shutdown hook
    if rest_client is not None and session_manager.is_session_open():
        from trading.ohlc_bootstrap import bootstrap_ohlc_for_session

        bootstrap_ohlc_for_session(rest_client, signal_engine, epic, market)
    return loop


def start_market_stream(cfg: Config, *, rest_client: Any | None) -> Any | None:
    """Connect IG price stream (Lightstreamer or REST poll) into MarketDataHub."""
    global _stream_client
    if rest_client is None:
        return None

    from system.credentials_holder import get_credentials_holder

    creds = get_credentials_holder().credentials
    session = getattr(rest_client, "session", None)
    if creds is None or session is None or not getattr(session, "is_valid", False):
        log_engine("market stream skipped — no valid IG session")
        return None

    _market, epic = _resolve_instrument(cfg)
    hub = get_market_data_hub()
    hub.attach_rest(rest_client)
    hub.set_min_fetch_interval(float(cfg.refresh_seconds))
    hub.fetch_if_stale(epic, min_interval=0.0)

    try:
        from ig_api.streaming_factory import create_streaming_client

        client = create_streaming_client(
            creds,
            session,
            rest_client=rest_client,
            poll_interval_seconds=float(cfg.refresh_seconds),
            transport=cfg.streaming_transport,
        )
    except Exception as e:
        log_engine(f"market stream create failed: {type(e).__name__}: {e}")
        return None

    def on_price(update: Any) -> None:
        hub.publish(
            str(update.epic),
            float(update.bid),
            float(update.offer),
            source="stream",
        )

    if hasattr(client, "on_price"):
        client.on_price(on_price)
    client.subscribe_market(epic)
    try:
        client.connect()
    except Exception as e:
        log_engine(f"market stream connect failed: {type(e).__name__}: {e}")
        return None

    _stream_client = client
    label = getattr(client, "transport_label", "stream")
    if callable(label):
        label = label()
    log_engine(f"market stream started epic={epic} transport={label}")
    return client


def stop_market_stream(client: Any | None = None) -> None:
    """Disconnect IG price stream."""
    global _stream_client
    target = client if client is not None else _stream_client
    if target is None:
        return
    try:
        if hasattr(target, "disconnect"):
            target.disconnect()
    except Exception as e:
        log_engine(f"market stream disconnect failed: {type(e).__name__}: {e}")
    if target is _stream_client:
        _stream_client = None
