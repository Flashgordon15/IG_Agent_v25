"""
Build orchestration trading loops and execution stack for v25 main entry.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime
from typing import Any, Callable

from data.journal import DecisionJournal
from data.learning_store import LearningStore
from data.models import Quote
from execution.execution_engine import ExecutionEngine
from execution.trading_loop import TradingLoop as ExecutionTickLoop
from execution.types import ExecutionMode
from runtime.market_orchestrator import MarketOrchestrator, attach_snapshot_handlers
from signals.signal_engine import SignalEngine
from system.config import Config
from system.config_validator import apply_config_defaults
from system.engine_log import log_engine
from system.market_data_hub import get_market_data_hub
from trading.environment_scorer import EnvironmentScorer
from trading.instrument_registry import InstrumentRegistry
from trading.points_engine import PointsEngine
from trading.session_manager import SessionManager
from trading.trading_loop import TradingLoop as AgentTradingLoop

_stream_client: Any | None = None
_position_sync: Any | None = None


_INSTRUMENT_CFG_KEYS = (
    "signal_threshold",
    "max_spread_pts",
    "stop_distance_points",
    "default_stop_distance_points",
    "adaptive_min_risk_points",
    "adaptive_max_risk_points",
    "min_atr_points",
    "ig_point_value_gbp",
    "trade_size",
    "risk_cap_gbp",
    "stale_threshold_seconds",
)


def _config_for_instrument(cfg: Config, inst: dict[str, Any]) -> Config:
    data = deepcopy(cfg.as_dict())
    for key in _INSTRUMENT_CFG_KEYS:
        if inst.get(key) is not None:
            if key == "max_spread_pts":
                data["max_spread_points"] = float(inst[key])
            else:
                data[key] = inst[key]
    merged = apply_config_defaults(data)
    return Config(_data=merged)


def start_ig_position_sync(
    rest_client: Any,
    store: Any,
    tracker: Any,
    *,
    epic: str = "",
    interval_seconds: float,
    points_engine: Any | None = None,
    managed_epics: set[str] | frozenset[str] | None = None,
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
            points_engine=points_engine,
            managed_epics=managed_epics,
        )
        tracker.attach_sync(sync)
        sync.start()
        _position_sync = sync
        label = epic or "all"
        log_engine(f"IG position sync attached epic={label}")
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


def _build_single_loop(
    cfg: Config,
    *,
    instrument_id: str,
    inst: dict[str, Any],
    rest_client: Any | None,
    mode: ExecutionMode,
    store: LearningStore,
    points_engine: PointsEngine,
    position_sync: Any | None,
) -> AgentTradingLoop:
    market = str(inst.get("name") or instrument_id)
    epic = str(inst.get("epic") or cfg.epic)
    loop_cfg = _config_for_instrument(cfg, inst)
    signal_engine = SignalEngine(loop_cfg, store)
    env_scorer = EnvironmentScorer(
        signal_engine, config=loop_cfg, rest_client=rest_client, epic=epic
    )
    prime = InstrumentRegistry(cfg.as_dict()).session_whitelist_for_epic(epic)
    if prime:
        env_scorer.set_prime_sessions(prime)
    session_manager = SessionManager(
        epic,
        market=market,
        points_engine=points_engine,
        environment_scorer=env_scorer,
        signal_engine=signal_engine,
        rest_client=rest_client,
    )

    from execution.trade_tracker import TradeTracker

    tracker = TradeTracker(store, prefer_ig=rest_client is not None)
    if position_sync is not None:
        tracker.attach_sync(position_sync)

    exec_engine = ExecutionEngine(
        mode=mode,
        config=loop_cfg,
        store=store,
        rest_client=rest_client,
        trade_tracker=tracker,
        points_engine=points_engine,
        environment_scorer=env_scorer,
    )
    if position_sync is not None:
        exec_engine.attach_position_sync(position_sync)

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

    def quote_source(epic_key: str = epic) -> Callable[[], Quote | None]:
        def _source() -> Quote | None:
            # Use only the hub's in-memory snapshot from Lightstreamer.
            # REST fallback via fetch_if_stale() was causing trading-loop deadlocks
            # because REST calls to IG sometimes hang indefinitely, blocking all three
            # loop threads. The Lightstreamer stream provides live quotes every ~30s
            # which is sufficient for gate evaluation and order entry decisions.
            snap = hub.get_snapshot(epic_key)
            if snap is None or snap.bid <= 0:
                return None
            return snap.to_quote()

        return _source

    loop = AgentTradingLoop(
        loop_cfg,
        market=market,
        epic=epic,
        session_manager=session_manager,
        environment_scorer=env_scorer,
        points_engine=points_engine,
        signal_engine=signal_engine,
        execution_loop=execution_loop,
        quote_source=quote_source(),
        learning_store=store,
        position_sync=position_sync,
        publish_snapshots=True,
        instrument_id=instrument_id,
    )

    return loop


def build_market_orchestrator(
    cfg: Config,
    *,
    rest_client: Any | None = None,
    mode: ExecutionMode | None = None,
) -> MarketOrchestrator:
    """Phase A — one loop per enabled instrument, shared PointsEngine."""
    reg = InstrumentRegistry(cfg.as_dict())
    enabled = reg.get_enabled_with_ids()
    if not enabled:
        raise ValueError("No enabled instruments in config")

    store = LearningStore(str(cfg.learning_db))
    points_engine = PointsEngine(store)

    try:
        from system.telegram_notifier import (
            configure_telegram,
            set_heartbeat_provider,
            start_telegram_heartbeat,
        )

        configure_telegram(cfg)
    except Exception as e:
        log_engine(f"telegram configure failed: {type(e).__name__}: {e}")

    exec_mode = mode
    if exec_mode is None:
        exec_mode = ExecutionMode.DEMO if rest_client is not None else ExecutionMode.TEST

    position_sync = None
    if rest_client is not None:
        from execution.trade_tracker import TradeTracker

        tracker = TradeTracker(store, prefer_ig=True)
        managed_epics = frozenset(
            str(inst.get("epic") or "").strip()
            for _iid, inst in enabled
            if str(inst.get("epic") or "").strip()
        )
        position_sync = start_ig_position_sync(
            rest_client,
            store,
            tracker,
            epic="",
            interval_seconds=float(cfg.position_sync_seconds),
            points_engine=points_engine,
            managed_epics=managed_epics,
        )

    loops: list[AgentTradingLoop] = []
    for iid, inst in enabled:
        loops.append(
            _build_single_loop(
                cfg,
                instrument_id=iid,
                inst=inst,
                rest_client=rest_client,
                mode=exec_mode,
                store=store,
                points_engine=points_engine,
                position_sync=position_sync,
            )
        )

    from trading.ohlc_bootstrap import bootstrap_ohlc_parallel

    bootstrap_ohlc_parallel(rest_client, loops)

    if rest_client is None:
        from system.stream_ready import signal_stream_ready

        signal_stream_ready(source="test_mode_no_stream")

    primary_epic = str(enabled[0][1].get("epic") or cfg.epic)
    enabled_epics = [
        str(inst.get("epic") or "").strip()
        for _iid, inst in enabled
        if str(inst.get("epic") or "").strip()
    ]
    instrument_meta = {
        str(inst.get("epic") or "").strip(): {
            "name": str(inst.get("name") or iid),
            "instrument_id": iid,
        }
        for iid, inst in enabled
        if str(inst.get("epic") or "").strip()
    }
    orch = MarketOrchestrator(
        cfg,
        loops,
        primary_epic=primary_epic,
        enabled_epics=enabled_epics,
        instrument_meta=instrument_meta,
    )
    if len(loops) > 1:
        attach_snapshot_handlers(orch)
    log_engine(
        f"market_orchestrator built: {len(loops)} markets "
        f"({', '.join(l._epic for l in loops)})"
    )

    try:
        from api.snapshot_store import get_tick
        from system.telegram_notifier import (
            get_telegram_notifier,
            set_heartbeat_provider,
            start_telegram_heartbeat,
        )

        def _heartbeat_snapshot() -> dict[str, Any]:
            tick = get_tick()
            sig = tick.get("signal") or {}
            pts = tick.get("points") or {}
            positions = tick.get("positions") or []
            return {
                "fitness": float(sig.get("fitness") or tick.get("fitness_score") or 0),
                "signal": float(sig.get("confidence") or 0),
                "stream": str(tick.get("stream_status") or "DISCONNECTED"),
                "positions": len(positions) if isinstance(positions, list) else 0,
                "cumulative": float(pts.get("cumulative") or 0),
                "state": str(pts.get("state") or points_engine.get_state()),
            }

        set_heartbeat_provider(_heartbeat_snapshot)
        start_telegram_heartbeat()
        notifier = get_telegram_notifier()
        if notifier is not None and notifier.enabled:
            notifier.notify_startup(state_restored=True)
    except Exception as e:
        log_engine(f"telegram heartbeat setup failed: {type(e).__name__}: {e}")

    return orch


def build_trading_loop(
    cfg: Config,
    *,
    rest_client: Any | None = None,
    mode: ExecutionMode | None = None,
) -> AgentTradingLoop | MarketOrchestrator:
    """Backward-compatible entry — orchestrator when multiple markets enabled."""
    reg = InstrumentRegistry(cfg.as_dict())
    if len(reg.get_enabled()) > 1:
        return build_market_orchestrator(cfg, rest_client=rest_client, mode=mode)
    orch = build_market_orchestrator(cfg, rest_client=rest_client, mode=mode)
    loop = orch.primary
    if loop is None:
        raise ValueError("No trading loop built")
    return loop


def start_market_stream(cfg: Config, *, rest_client: Any | None) -> Any | None:
    """Connect IG price stream and subscribe all enabled epics."""
    global _stream_client
    if rest_client is None:
        return None

    from system.credentials_holder import get_credentials_holder

    creds = get_credentials_holder().credentials
    session = getattr(rest_client, "session", None)
    if creds is None or session is None or not getattr(session, "is_valid", False):
        log_engine("market stream skipped — no valid IG session")
        return None

    reg = InstrumentRegistry(cfg.as_dict())
    epics = [str(inst.get("epic") or "") for _iid, inst in reg.get_enabled_with_ids()]
    epics = [e for e in epics if e]
    if not epics:
        epics = [str(cfg.epic)]

    from system.stream_ready import reset_stream_ready

    reset_stream_ready()

    hub = get_market_data_hub()
    hub.attach_rest(rest_client)
    hub.set_min_fetch_interval(float(cfg.refresh_seconds))
    for epic in epics:
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
    for epic in epics:
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
    log_engine(f"market stream started epics={epics} transport={label}")
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
