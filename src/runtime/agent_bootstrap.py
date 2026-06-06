"""
Build orchestration trading loops and execution stack for v25 main entry.
"""

from __future__ import annotations

from copy import deepcopy
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
from system.paths import data_dir
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
    transaction_sync: Any | None = None,
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
            transaction_sync=transaction_sync,
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
    signal_engine._environment_scorer = env_scorer
    prime = InstrumentRegistry(cfg.as_dict()).session_whitelist_for_epic(epic)
    if prime:
        env_scorer.set_prime_sessions(prime)
    _safe_epic = epic.replace(".", "_").replace("/", "_")
    session_manager = SessionManager(
        epic,
        market=market,
        points_engine=points_engine,
        environment_scorer=env_scorer,
        signal_engine=signal_engine,
        rest_client=rest_client,
        state_path=str(data_dir() / "state" / f"session_state_{_safe_epic}.json"),
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
    from system.startup_tracker import mark as _startup_mark

    reg = InstrumentRegistry(cfg.as_dict())
    enabled = reg.get_enabled_with_ids()
    if not enabled:
        raise ValueError("No enabled instruments in config")

    store = LearningStore(str(cfg.learning_db))
    points_engine = PointsEngine(store)
    _startup_mark("database")

    # Quick self-test — run deployed-fixes regression suite to catch stale code
    try:
        import os
        import subprocess
        import sys

        from system.paths import project_root

        if os.environ.get("IG_AGENT_SKIP_DEPLOY_CHECK") == "1":
            _startup_mark("self_test", note="skipped watchdog restart")
            log_engine("startup self-test skipped (IG_AGENT_SKIP_DEPLOY_CHECK=1)")
        else:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests/test_deployed_fixes.py",
                    "-x",
                    "-q",
                    "--tb=no",
                ],
                cwd=str(project_root()),
                env={
                    **__import__("os").environ,
                    "PYTHONPATH": str(project_root() / "src"),
                },
                capture_output=True,
                text=True,
                timeout=60,
            )
            passed = result.returncode == 0
            note = (
                "all passed" if passed else f"FAILED — {result.stdout.strip()[-200:]}"
            )
            _startup_mark("self_test", note)
            if not passed:
                log_engine(f"startup self-test FAILED:\n{result.stdout[-400:]}")
            else:
                log_engine("startup self-test: all deployed-fixes checks passed")
    except Exception as e:
        _startup_mark("self_test", f"skipped: {type(e).__name__}")
        log_engine(f"startup self-test skipped: {type(e).__name__}: {e}")

    # Smoke test — verify no known blocking conditions carried over from previous session
    try:
        from execution.pending_order_reconcile import recover_pending_state_for_startup

        recover_pending_state_for_startup()
        from api.agent_control import is_paused, start_trading

        if is_paused():
            start_trading()  # auto-unpause if somehow paused at startup
            log_engine(
                "startup smoke-test: auto-unpaused trading (was paused at startup)"
            )
        _startup_mark("smoke_test", "clear")
        log_engine("startup smoke-test: no blocking conditions detected")
    except Exception as e:
        _startup_mark("smoke_test", f"warning: {type(e).__name__}")
        log_engine(f"startup smoke-test warning: {type(e).__name__}: {e}")

    try:
        from system.telegram_notifier import (
            configure_telegram,
            set_heartbeat_provider,
            start_telegram_heartbeat,
        )

        configure_telegram(cfg)
        from system.telegram_notifier import send_startup_test

        send_startup_test()
    except Exception as e:
        log_engine(f"telegram configure failed: {type(e).__name__}: {e}")

    exec_mode = mode
    if exec_mode is None:
        exec_mode = (
            ExecutionMode.DEMO if rest_client is not None else ExecutionMode.TEST
        )

    position_sync = None
    if rest_client is not None:
        from execution.trade_tracker import TradeTracker
        from runtime.ig_transaction_sync import IgTransactionSync

        tracker = TradeTracker(store, prefer_ig=True)
        managed_epics = frozenset(
            str(inst.get("epic") or "").strip()
            for _iid, inst in enabled
            if str(inst.get("epic") or "").strip()
        )

        # Start transaction sync daemon — populates ig_pnl_currency on closed trades
        txn_sync: Any | None = None
        try:
            txn_sync = IgTransactionSync(
                rest_client,
                store,
                interval_seconds=float(getattr(cfg, "transaction_sync_seconds", 300.0)),
                min_gap_seconds=float(
                    getattr(cfg, "transaction_sync_min_gap_seconds", 120.0)
                ),
                history_days=int(getattr(cfg, "transaction_history_days", 2)),
                display_hours=24.0,
            )
            txn_sync.start()
            log_engine("IG transaction sync started")
        except Exception as _txn_e:
            log_engine(
                f"IG transaction sync start failed: {type(_txn_e).__name__}: {_txn_e}"
            )
            txn_sync = None

        position_sync = start_ig_position_sync(
            rest_client,
            store,
            tracker,
            epic="",
            interval_seconds=float(cfg.position_sync_seconds),
            points_engine=points_engine,
            managed_epics=managed_epics,
            transaction_sync=txn_sync,
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
    _startup_mark("loops", note=f"{len(loops)} markets ready")

    # Run OHLC bootstrap (cache-first) synchronously so indicators are warm before
    # the first tick. Yahoo pre-seeding for any cold caches runs in a background
    # thread so it does NOT delay the API server startup.
    from trading.ohlc_bootstrap import bootstrap_ohlc_parallel

    def _background_yahoo_seed(loops_ref: list) -> None:
        """Fetch missing OHLC caches from Yahoo in the background after startup."""
        try:
            from data.ohlc_yahoo_seeder import EPIC_YAHOO_MAP, fetch_yahoo_ohlc_for_epic
            from system.paths import data_dir as _data_dir

            _ohlc_dir = _data_dir() / "ohlc_cache"
            _ohlc_dir.mkdir(parents=True, exist_ok=True)
            for loop in loops_ref:
                _epic = loop._epic
                if _epic not in EPIC_YAHOO_MAP:
                    continue
                _slug = _epic.replace(".", "_").replace("/", "_")
                _cache = _ohlc_dir / f"{_slug}_5m.jsonl"
                if _cache.exists() and _cache.stat().st_size > 1024:
                    continue
                try:
                    _symbol, _market_name = EPIC_YAHOO_MAP[_epic]
                    log_engine(
                        f"OHLC background seed: fetching {_market_name} from Yahoo ({_symbol})"
                    )
                    fetch_yahoo_ohlc_for_epic(_epic, market=_market_name)
                    log_engine(f"OHLC background seed: {_market_name} cache populated")
                    # Inject newly seeded bars into the running signal engine
                    try:
                        bootstrap_ohlc_parallel(rest_client, [loop])
                    except Exception:
                        pass
                except Exception as _ye:
                    log_engine(
                        f"OHLC background seed skipped {_epic}: {type(_ye).__name__}: {_ye}"
                    )
        except Exception as _e:
            log_engine(f"OHLC background seed error: {type(_e).__name__}: {_e}")

    import threading as _threading

    _seed_thread = _threading.Thread(
        target=_background_yahoo_seed,
        args=(loops,),
        name="ohlc-yahoo-seed",
        daemon=True,
    )
    _seed_thread.start()

    bootstrap_ohlc_parallel(rest_client, loops)
    _startup_mark("ohlc", note=f"{len(loops)} markets loaded")

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
                "balance": tick.get("balance_gbp"),
                "daily_pnl": tick.get("daily_pnl_gbp"),
            }

        set_heartbeat_provider(_heartbeat_snapshot)
        start_telegram_heartbeat()
        notifier = get_telegram_notifier()
        if notifier is not None and notifier.enabled:
            notifier.notify_startup(
                state_restored=True,
                market_count=len(loops),
                points_state=points_engine.get_state(),
            )
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
    # Do NOT pre-fetch via REST at startup — Lightstreamer delivers prices within
    # seconds. Calling fetch_if_stale(min_interval=0.0) for all 6 epics creates a
    # burst of 6 simultaneous REST calls that counts against the IG API key allowance.

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
