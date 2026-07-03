"""
Phase A — run one TradingLoop per enabled instrument; shared PointsEngine and dashboard tick.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

import numpy as np
import pandas as pd

from api.snapshot import _iso_now
from api.snapshot_store import publish_tick
from system.config import Config
from system.engine_log import log_engine
from system.paths import data_dir, project_root
from trading.trading_loop import TradingLoop

_ORCHESTRATOR_REF: "MarketOrchestrator | None" = None

# Layer 3 — £1k/day global rotation matrix (indices, FX, metals).
# Dashboard + routing monitor all epics; each TradingLoop thread stays epic-scoped.
GLOBAL_ROTATION_UNIVERSE: tuple[str, ...] = (
    # Major global indices
    "IX.D.NIKKEI.IFM.IP",
    "IX.D.DOW.IFM.IP",
    "IX.D.NASDAQ.IFM.IP",
    "IX.D.DAX.IFM.IP",
    "IX.D.FTSE.IFM.IP",
    "IX.D.CAC.IFM.IP",
    "IX.D.HSI.IFM.IP",
    "IX.D.ASX.IFM.IP",
    "IX.D.SPTRD.IFE.IP",
    # Cross-currency pairs
    "CS.D.EURUSD.CFD.IP",
    "CS.D.GBPUSD.CFD.IP",
    "CS.D.USDJPY.CFD.IP",
    "CS.D.EURGBP.CFD.IP",
    "CS.D.AUDUSD.CFD.IP",
    "CS.D.USDCAD.CFD.IP",
    "CS.D.USDCHF.CFD.IP",
    "CS.D.NZDUSD.CFD.IP",
    "CS.D.EURJPY.CFD.IP",
    "CS.D.GBPJPY.CFD.IP",
    # Precious metals & energy
    "CS.D.CFPGOLD.CFP.IP",
    "CS.D.CFPSILVER.CFP.IP",
    "CS.D.CFPPLAT.CFP.IP",
    "CS.D.CRUDE.CFD.IP",
)

TOP_ROTATION_SLOTS = 3
MAX_ROTATION_SLOTS = 5
ROTATION_EXPAND_THRESHOLD_PCT = 10.0
ROTATION_GRACE_CYCLES = 3
ROTATION_MIN_ONLINE_FOR_FILTER = 3
_ROTATION_ONLINE_MAX_AGE_SEC = 30.0
_ROTATION_RANK_FLOOR = 0.01
_FEED_STARVATION_MAX_AGE_SEC = 120.0
_FEED_RECOVERY_MAX_AGE_SEC = 30.0
_ROTATION_LOG_MIN_INTERVAL_SEC = 60.0
OFFLINE_BROKER_FEED_REJECTED = "OFFLINE_BROKER_FEED_REJECTED"

# V5 autonomic async bootstrap — priority hydration universe (7 assets).
V5_HYDRATION_EPICS: tuple[str, ...] = (
    "CS.D.CFPGOLD.CFP.IP",
    "IX.D.DOW.IFM.IP",
    "IX.D.NIKKEI.IFM.IP",
    "CS.D.EURUSD.CFD.IP",
    "CS.D.CRUDE.CFD.IP",
    "IX.D.FTSE.IFM.IP",
    "IX.D.DAX.IFM.IP",
)
_V5_HYDRATOR_THREAD: threading.Thread | None = None
_V5_HYDRATOR_LOCK = threading.Lock()

# V6 — native in-loop coroutine handoff (instant skeleton → post-bind asyncio hydration).
V6_HYDRATION_EPICS: tuple[str, ...] = V5_HYDRATION_EPICS
V6_HYDRATION_TIMEOUT_SEC = 3.0
V6_BROKER_HYDRATION_TIMEOUT_SEC = 2.0
V6_RAM_BACKFILL_TICKS = 43200
V6_ATR_WINDOW = 30
_V6_HANDOFF_TASKS: set[asyncio.Task[Any]] = set()
_V6_HANDOFF_LOCK = threading.Lock()

_AUDIT_PATHS = (
    project_root() / "src" / "data" / "logs" / "self_healing_audit.log",
    data_dir() / "logs" / "self_healing_audit.log",
)
_v6_audit_lock = threading.Lock()


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append_v6_audit(record: dict[str, Any]) -> None:
    payload = dict(record)
    payload.setdefault("ts", _utc_iso())
    payload.setdefault("component", "v6_coroutine_handoff")
    line = json.dumps(payload, separators=(",", ":"), default=str)
    for path in _AUDIT_PATHS:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with _v6_audit_lock:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception:
            pass


class SkeletonTradingLoop:
    """Zero-network RAM skeleton — full TradingLoop materialized post :8080 bind."""

    _skeleton = True

    def __init__(
        self,
        *,
        epic: str,
        market: str,
        instrument_id: str,
        inst: dict[str, Any] | None = None,
    ) -> None:
        self._epic = epic
        self._market = market
        self.instrument_id = instrument_id
        self._inst = dict(inst or {})
        self._signal_engine = None
        self._env = None
        self._execution_loop = None
        self._config = None
        self.last_context = None
        self._on_snapshot = None
        self._publish_snapshots = False
        self._store = None
        self.paused_at_boot = True

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


class V6InLoopCoroutineHandoff:
    """
    Post-bind asyncio worker — materialize loops, arm channels, hydrate OHLC.

    All broker/network hydration uses ``asyncio.create_task`` on the uvicorn loop;
    no raw ``threading.Thread`` for async API client paths.
    """

    def __init__(
        self,
        orchestrator: "MarketOrchestrator",
        *,
        cfg: Any | None = None,
        rest_client: Any | None = None,
    ) -> None:
        self._orch = orchestrator
        self._cfg = cfg or orchestrator.config
        self._rest = rest_client or getattr(orchestrator, "_v6_rest_client", None)

    async def run_full_handoff(self) -> None:
        await asyncio.to_thread(self.run_full_handoff_sync)

    def run_full_handoff_sync(self) -> None:
        log_engine("V6InLoopHandoff: run_full_handoff entered")
        with _V6_HANDOFF_LOCK:
            if not getattr(self._orch, "_v6_skeleton_mode", False):
                self._orch.start()
                return
            if getattr(self._orch, "_v6_materialized", False):
                if not self._orch.is_running():
                    self._orch._start_live_channels_impl()
                return
        try:
            self._materialize_full_loops_sync()
            with _V6_HANDOFF_LOCK:
                self._orch._v6_materialized = True
                self._orch._v6_skeleton_mode = False
            from api.agent_control import register_trading_loop

            register_trading_loop(self._orch)
            if len(self._orch._loops) > 1:
                attach_snapshot_handlers(self._orch)
            import os

            if os.environ.get("IG_TEST_HARNESS", "").strip() != "1":
                from apex.microkernel import schedule_array_warmup

                schedule_array_warmup(
                    self._rest,
                    list(self._orch._loops),
                    self._cfg,
                )
            sm = AutonomicBootstrapStateMachine(self._orch)
            sm.allocate_and_ready()
            self._orch._start_live_channels_impl()
            self._orch.instant_ram_bootstrap_all_epics()
            self._schedule_deferred_async_tail()
            log_engine(
                f"V6InLoopHandoff: complete — {len(self._orch._loops)} loops live "
                "(coroutine hydration armed)"
            )
        except Exception as exc:
            _append_v6_audit(
                {
                    "event": "handoff_fatal",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            log_engine(f"V6InLoopHandoff FATAL: {type(exc).__name__}: {exc}")
            try:
                from system.system_state import get_system_state

                snap = get_system_state().snapshot_model()
                get_system_state().update_state(
                    snap.phase,
                    snap.percent,
                    "Trading plane handoff failed",
                    loops={
                        "built": len(getattr(self._orch, "_loops", []) or []),
                        "running": False,
                        "accepting_ticks": False,
                    },
                    ready=False,
                )
            except Exception:
                pass
            raise

    def _schedule_deferred_async_tail(self) -> None:
        """Broker sync + OHLC hydrators — non-blocking tail after live loops start."""

        async def _tail() -> None:
            await self._arm_deferred_broker_sync()
            await self._spawn_hydration_tasks()

        from system.boot.boot_loop_holder import get_boot_loop, schedule_coro

        loop = get_boot_loop()
        if loop is not None and loop.is_running():
            schedule_coro(_tail())
            return
        threading.Thread(
            target=lambda: asyncio.run(_tail()),
            name="v6-handoff-tail",
            daemon=True,
        ).start()

    def _materialize_full_loops_sync(self) -> None:
        # Eager imports on the gate worker before loop construction — avoids Python
        # 3.14 import-lock stalls when post-bind threads import the same modules.
        import execution.execution_engine  # noqa: F401
        import execution.live_executor  # noqa: F401
        import trading.trading_loop  # noqa: F401

        ctx = getattr(self._orch, "_v6_build_ctx", None) or {}
        enabled: list[tuple[str, dict[str, Any]]] = list(ctx.get("enabled") or [])
        paused = bool(ctx.get("paused_at_boot", True))
        from data.learning_store import LearningStore
        from execution.types import ExecutionMode
        from runtime.agent_bootstrap import _build_single_loop
        from trading.points_engine import PointsEngine

        cfg = self._cfg
        store = LearningStore(str(cfg.learning_db))
        points_engine = PointsEngine(store)
        exec_mode = ctx.get("exec_mode")
        if exec_mode is None:
            exec_mode = (
                ExecutionMode.DEMO
                if self._rest is not None
                else ExecutionMode.TEST
            )
        rest = self._rest

        loops: list[Any] = []
        for iid, inst in enabled:
            epic = str(inst.get("epic") or cfg.epic)
            log_engine(f"V6 materialize: building loop {epic}")
            try:
                loops.append(
                    _build_single_loop(
                        cfg,
                        instrument_id=iid,
                        inst=inst,
                        rest_client=rest,
                        mode=exec_mode,
                        store=store,
                        points_engine=points_engine,
                        position_sync=None,
                        paused_at_boot=paused,
                    )
                )
            except Exception as exc:
                log_engine(
                    f"V6 materialize: loop build error {epic}: "
                    f"{type(exc).__name__}: {exc}"
                )
        if not loops:
            raise RuntimeError("V6 materialize: no trading loops built")
        self._orch._loops = loops
        self._orch.configure_v5_autonomic_bootstrap(rest_client=rest, defer_ohlc=True)
        self._orch._v6_store = store
        self._orch._v6_points_engine = points_engine
        log_engine(
            f"V6InLoopHandoff: materialized {len(loops)} full loops "
            f"({', '.join(str(lp._epic) for lp in loops)})"
        )

    async def _materialize_full_loops(self) -> None:
        await asyncio.to_thread(self._materialize_full_loops_sync)

    async def _arm_deferred_broker_sync(self) -> None:
        if self._rest is None:
            return

        def _sync() -> None:
            try:
                from runtime.agent_bootstrap import (
                    start_ig_position_sync,
                    start_order_reconciler_worker,
                )
                from system.ml_filter_overrides import load_filter_overrides

                load_filter_overrides(force=True)
            except Exception as exc:
                log_engine(f"V6 deferred ml_filter_overrides: {type(exc).__name__}")
            try:
                from execution.trade_tracker import TradeTracker
                from runtime.ig_transaction_sync import (
                    IgTransactionSync,
                    _set_transaction_sync_instance,
                )

                store = getattr(self._orch, "_v6_store", None)
                points_engine = getattr(self._orch, "_v6_points_engine", None)
                if store is None:
                    return
                cfg = self._cfg
                tracker = TradeTracker(store, prefer_ig=True)
                enabled = list(
                    (getattr(self._orch, "_v6_build_ctx", {}) or {}).get("enabled") or []
                )
                managed_epics = frozenset(
                    str(inst.get("epic") or "").strip()
                    for _iid, inst in enabled
                    if str(inst.get("epic") or "").strip()
                )
                txn_sync: Any | None = None
                try:
                    txn_sync = IgTransactionSync(
                        self._rest,
                        store,
                        interval_seconds=float(
                            getattr(cfg, "transaction_sync_seconds", 300.0)
                        ),
                        min_gap_seconds=float(
                            getattr(cfg, "transaction_sync_min_gap_seconds", 120.0)
                        ),
                        history_days=int(getattr(cfg, "transaction_history_days", 2)),
                        display_hours=24.0,
                    )
                    txn_sync.start()
                    _set_transaction_sync_instance(txn_sync)
                    log_engine("V6 deferred: IG transaction sync started")
                except Exception as exc:
                    log_engine(f"V6 deferred txn sync: {type(exc).__name__}")
                sync = start_ig_position_sync(
                    self._rest,
                    store,
                    tracker,
                    epic="",
                    interval_seconds=float(cfg.position_sync_seconds),
                    points_engine=points_engine,
                    managed_epics=managed_epics,
                    transaction_sync=txn_sync,
                )
                if sync is not None:
                    for loop in self._orch._loops:
                        try:
                            eng = loop._execution_loop.execution_engine
                            eng._trade_tracker.attach_sync(sync)  # noqa: SLF001
                            eng.attach_position_sync(sync)
                        except Exception:
                            pass
                start_order_reconciler_worker(self._rest, config=cfg)
            except Exception as exc:
                log_engine(f"V6 deferred broker sync failed: {type(exc).__name__}: {exc}")

        await asyncio.to_thread(_sync)

    async def _spawn_hydration_tasks(self) -> None:
        loops = [
            lp
            for lp in self._orch._loops
            if str(getattr(lp, "_epic", "") or "") in V6_HYDRATION_EPICS
        ]
        if not loops:
            loops = list(self._orch._loops)
        tasks = [
            asyncio.create_task(
                self._hydrate_epic_coroutine(lp),
                name=f"v6-hydrate-{getattr(lp, '_epic', 'unknown')}",
            )
            for lp in loops
        ]
        for task in tasks:
            _V6_HANDOFF_TASKS.add(task)
            task.add_done_callback(_V6_HANDOFF_TASKS.discard)
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._ensure_all_epics_ram_hydrated()

    async def _ensure_all_epics_ram_hydrated(self) -> None:
        """Guarantee 7/7 hydration — RAM gasket for any epic still cold after broker pass."""
        for loop in list(self._orch._loops):
            epic = str(getattr(loop, "_epic", "") or "")
            if not epic:
                continue
            if self._orch._hydration_registry.get(epic) == "HYDRATED":
                continue
            await asyncio.to_thread(
                self._orch.backfill_indicators_from_volatile_buffer,
                epic=epic,
                loop=loop,
            )
            self._orch._hydration_registry[epic] = "HYDRATED"
        self._orch.publish_hydration_registry_progress()
        ready = self._orch.hydration_ready_count()
        total = len(self._orch._loops)
        log_engine(
            f"V6 RAM bootstrap seed complete — ohlc {ready}/{total} HYDRATED"
        )

    async def _hydrate_epic_coroutine(self, loop: Any) -> None:
        epic = str(getattr(loop, "_epic", "") or "")
        market = str(getattr(loop, "_market", "") or "")
        if not epic:
            return
        rest = self._rest

        def _broker_hydrate() -> int:
            from trading.ohlc_bootstrap import bootstrap_ohlc_for_session

            return int(
                bootstrap_ohlc_for_session(
                    rest,
                    loop._signal_engine,
                    epic,
                    market,
                    environment_scorer=getattr(loop, "_env", None),
                    prefer_cache=True,
                )
                or 0
            )

        count = 0
        try:
            count = await asyncio.wait_for(
                asyncio.to_thread(_broker_hydrate),
                timeout=V6_BROKER_HYDRATION_TIMEOUT_SEC,
            )
        except (Exception, TimeoutError) as exc:
            err = f"{type(exc).__name__}: {exc}"
            status = getattr(getattr(exc, "response", None), "status_code", None)
            _append_v6_audit(
                {
                    "event": "hydration_timeout",
                    "epic": epic,
                    "status": status,
                    "error": err,
                    "ram_ticks": V6_RAM_BACKFILL_TICKS,
                }
            )
            log_engine(
                "[EMERGENCY HEAL] Broker rate-limited. "
                "Seeding matrices via 43k RAM ticks..."
            )
            count = await asyncio.to_thread(
                self._orch.backfill_indicators_from_volatile_buffer,
                epic=epic,
                loop=loop,
            )
            self._orch._hydration_registry[epic] = "HYDRATED"

        if count <= 0:
            log_engine(
                f"[EMERGENCY HEAL] {epic}: RAM gasket — volatile buffer ATR seed"
            )
            count = await asyncio.to_thread(
                self._orch.backfill_indicators_from_volatile_buffer,
                epic=epic,
                loop=loop,
            )
            self._orch._hydration_registry[epic] = "HYDRATED"
        elif self._orch._hydration_registry.get(epic) != "HYDRATED":
            self._orch._hydration_registry[epic] = "HYDRATED"
        self._orch.publish_hydration_registry_progress()
        if count > 0:
            log_engine(f"V6 hydrator: {epic} warm bars={count}")


def schedule_v6_coroutine_handoff(
    orchestrator: "MarketOrchestrator",
    *,
    cfg: Any | None = None,
    rest_client: Any | None = None,
) -> None:
    """Schedule V6 handoff on the uvicorn boot loop via ``asyncio.create_task``."""
    handoff = V6InLoopCoroutineHandoff(orchestrator, cfg=cfg, rest_client=rest_client)

    async def _runner() -> None:
        await handoff.run_full_handoff()

    from system.boot.boot_loop_holder import get_boot_loop, schedule_coro

    loop = get_boot_loop()
    if loop is not None and loop.is_running():
        schedule_coro(_runner())
        log_engine("V6InLoopHandoff: scheduled on boot event loop (post :8080)")
        return

    async def _standalone() -> None:
        await handoff.run_full_handoff()

    try:
        asyncio.run(_standalone())
    except RuntimeError:
        task = asyncio.create_task(_runner())
        _V6_HANDOFF_TASKS.add(task)
        task.add_done_callback(_V6_HANDOFF_TASKS.discard)
        log_engine("V6InLoopHandoff: scheduled on running loop (fallback)")


def ensure_v6_trading_plane_materialized(
    orchestrator: "MarketOrchestrator",
    *,
    cfg: Any | None = None,
    rest_client: Any | None = None,
    timeout_sec: float = 180.0,
) -> bool:
    """
    Block until skeleton loops are replaced and live TradingLoop threads run.

    Uses a single-threaded synchronous handoff on the gate worker — avoids
    fire-and-forget asyncio tasks that previously never completed materialization.
    """
    from system.trading_plane_readiness import is_trading_plane_live

    if is_trading_plane_live():
        return True

    resolved_cfg = cfg or orchestrator.config
    resolved_rest = rest_client or getattr(orchestrator, "_v6_rest_client", None)
    handoff = V6InLoopCoroutineHandoff(
        orchestrator,
        cfg=resolved_cfg,
        rest_client=resolved_rest,
    )
    remaining = max(30.0, float(timeout_sec))
    log_engine(
        f"V6InLoopHandoff: blocking materialization on gate worker "
        f"(timeout={remaining:.0f}s)"
    )
    try:
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="v6-handoff")
        fut = pool.submit(handoff.run_full_handoff_sync)
        try:
            fut.result(timeout=remaining)
        except FuturesTimeout:
            log_engine(
                f"V6InLoopHandoff: materialization timed out after {remaining:.0f}s "
                "— gate worker continuing (repair may retry)"
            )
            return is_trading_plane_live()
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    except Exception as exc:
        log_engine(
            f"V6InLoopHandoff: blocking materialization failed "
            f"{type(exc).__name__}: {exc}"
        )

    return is_trading_plane_live()


def build_market_orchestrator_instant(
    cfg: Config,
    *,
    rest_client: Any | None = None,
    paused_at_boot: bool = False,
) -> "MarketOrchestrator":
    """
    V6 instant RAM skeleton build — zero network, sub-10ms target.

  Returns MarketOrchestrator with ``SkeletonTradingLoop`` placeholders; full
    materialization runs via ``schedule_v6_coroutine_handoff`` after API bind.
    """
    from execution.types import ExecutionMode
    from trading.instrument_registry import InstrumentRegistry

    t0 = time.perf_counter()
    reg = InstrumentRegistry(cfg.as_dict())
    enabled = reg.get_enabled_with_ids()
    if not enabled:
        raise ValueError("No enabled instruments in config")

    skeletons: list[SkeletonTradingLoop] = []
    for iid, inst in enabled:
        epic = str(inst.get("epic") or cfg.epic)
        market = str(inst.get("name") or iid)
        sk = SkeletonTradingLoop(
            epic=epic,
            market=market,
            instrument_id=iid,
            inst=inst,
        )
        sk.paused_at_boot = paused_at_boot
        skeletons.append(sk)

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
    exec_mode = (
        ExecutionMode.DEMO if rest_client is not None else ExecutionMode.TEST
    )
    orch = MarketOrchestrator(
        cfg,
        skeletons,  # type: ignore[arg-type]
        primary_epic=primary_epic,
        enabled_epics=enabled_epics,
        instrument_meta=instrument_meta,
    )
    orch.configure_v6_handoff(
        rest_client=rest_client,
        enabled=enabled,
        paused_at_boot=paused_at_boot,
        exec_mode=exec_mode,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    log_engine(
        f"V6InstantBuild: {len(skeletons)} skeleton loops in {elapsed_ms:.2f}ms "
        f"({', '.join(s._epic for s in skeletons)})"
    )
    return orch


def preflight_v6_instant_bootstrap(config: Any | None = None) -> dict[str, Any]:
    """Gate 4 entry — V6 alias for RAM-bound preflight before skeleton build."""
    return preflight_v5_autonomic_bootstrap(config=config)


def preflight_v5_autonomic_bootstrap(config: Any | None = None) -> dict[str, Any]:
    """
    Gate 4 entry — allocate alpha ring + volatile RAM mirrors before orchestrator build.
    Non-blocking; safe to call from the boot thread before any broker REST hydration.
    """
    from intelligence.matrix_prebaker import MATRIX_COLS, TOTAL_CELLS
    from system.ipc.ring_buffer import get_alpha_ring_buffer

    ring = get_alpha_ring_buffer()
    mat = ring.matrix_view()
    if mat.shape != (TOTAL_CELLS, MATRIX_COLS) or mat.dtype != np.dtype(np.float32):
        raise RuntimeError(f"V5 preflight: matrix mismatch {mat.shape} {mat.dtype}")
    _ = float(mat[0, 0])
    _ = float(mat[-1, -1])
    try:
        from trading.cache_reaper import hydrate_volatile_caches_from_disk

        hydrate_volatile_caches_from_disk()
    except Exception as exc:
        log_engine(f"V5 preflight: volatile cache hydrate skipped: {type(exc).__name__}")
    report = {
        "cells": int(TOTAL_CELLS),
        "cols": int(MATRIX_COLS),
        "bytes": int(mat.nbytes),
    }
    log_engine(
        f"V5AutonomicBootstrap: preflight memory bound "
        f"cells={report['cells']} bytes={report['bytes']}"
    )
    return report


class AutonomicBootstrapStateMachine:
    """
    V5 non-blocking boot choreography for MarketOrchestrator.

    Sequence: allocate RAM → flip ready → open live channels → background OHLC.
    """

    def __init__(self, orchestrator: MarketOrchestrator) -> None:
        self._orch = orchestrator
        self._hydration_ready = 0
        self._hydration_total = 0

    def allocate_and_ready(self) -> None:
        from intelligence.matrix_prebaker import MATRIX_COLS, TOTAL_CELLS
        from system.ipc.ring_buffer import get_alpha_ring_buffer
        from system.system_state import BootPhase, get_system_state

        ring = get_alpha_ring_buffer()
        mat = ring.matrix_view()
        _ = float(mat[0, 0])
        try:
            if int(ring.telemetry().get("compile_generation") or 0) <= 0:
                ring.write_matrix_generation(mat.copy(), vector_density=1, cfg=None)
        except Exception as exc:
            log_engine(f"V5AutonomicBootstrap: compile gen bootstrap: {type(exc).__name__}")

        total = len(self._orch._loops)
        state = get_system_state()
        state.update_state(
            BootPhase.G4,
            90,
            "Autonomic Bootstrap Ready",
            gates_dict=None,
            hydration={"ohlc_epics_ready": 0, "ohlc_epics_total": total},
            loops={
                "built": total,
                "running": True,
                "accepting_ticks": False,
            },
            ready=True,
        )
        log_engine(
            f"V5AutonomicBootstrap: platform ready=True "
            f"matrix=({TOTAL_CELLS},{MATRIX_COLS}) loops={total}"
        )

    def start_live_channels(self) -> None:
        """Start loop threads and services without waiting for OHLC hydration."""
        orch = self._orch
        orch._start_live_channels_impl()

    def spawn_background_hydrator(self) -> None:
        if getattr(self._orch, "_v6_materialized", False):
            return
        global _V5_HYDRATOR_THREAD
        rest = getattr(self._orch, "_v5_rest_client", None)
        if rest is None or not getattr(self._orch, "_v5_defer_ohlc", True):
            return
        loops = [
            lp
            for lp in self._orch._loops
            if str(getattr(lp, "_epic", "") or "") in V5_HYDRATION_EPICS
        ]
        if not loops:
            loops = list(self._orch._loops)

        async def _async_hydrate() -> None:
            handoff = V6InLoopCoroutineHandoff(self._orch, rest_client=rest)
            await handoff._spawn_hydration_tasks()

        from system.boot.boot_loop_holder import get_boot_loop, schedule_coro

        boot_loop = get_boot_loop()
        if boot_loop is not None and boot_loop.is_running():
            self._hydration_total = len(loops)
            schedule_coro(_async_hydrate())
            log_engine(
                f"V6AutonomicBootstrap: asyncio OHLC hydrator armed epics={len(loops)}"
            )
            return

        def _worker() -> None:
            self._run_background_hydration(rest, loops)

        with _V5_HYDRATOR_LOCK:
            if _V5_HYDRATOR_THREAD is not None and _V5_HYDRATOR_THREAD.is_alive():
                return
            _V5_HYDRATOR_THREAD = threading.Thread(
                target=_worker,
                name="v5-ohlc-hydrator",
                daemon=True,
            )
            _V5_HYDRATOR_THREAD.start()
        log_engine(
            f"V5AutonomicBootstrap: background OHLC hydrator armed "
            f"epics={len(loops)}"
        )

    def _run_background_hydration(self, rest_client: Any, loops: list[Any]) -> None:
        from system.system_state import get_system_state
        from trading.ohlc_bootstrap import (
            bootstrap_ohlc_for_session,
            is_historical_allowance_lockout,
            local_cache_ready,
        )

        ready_count = 0
        for loop in loops:
            epic = str(getattr(loop, "_epic", "") or "")
            market = str(getattr(loop, "_market", "") or "")
            if not epic:
                continue
            count = 0
            try:
                count = bootstrap_ohlc_for_session(
                    rest_client,
                    loop._signal_engine,
                    epic,
                    market,
                    environment_scorer=loop._env,
                    prefer_cache=True,
                )
            except (Exception, TimeoutError) as exc:
                err = f"{type(exc).__name__}: {exc}"
                status = getattr(getattr(exc, "response", None), "status_code", None)
                log_engine(
                    "[EMERGENCY HEAL] Broker rate-limited. "
                    "Seeding matrices via 43k RAM ticks..."
                )
                log_engine(
                    f"V5 hydrator: {epic} broker hold ({status or err}) — RAM gasket"
                )
                count = self._orch.backfill_indicators_from_volatile_buffer(
                    epic=epic,
                    loop=loop,
                )
                self._orch._hydration_registry[epic] = "HYDRATED"

            if count <= 0:
                count = self._orch.backfill_indicators_from_volatile_buffer(
                    epic=epic,
                    loop=loop,
                )
                self._orch._hydration_registry[epic] = "HYDRATED"

            if count > 0 or local_cache_ready(epic, market):
                ready_count += 1
                self._orch._hydration_registry[epic] = "HYDRATED"
            try:
                self._orch.publish_hydration_registry_progress()
            except Exception:
                pass
            time.sleep(0.05)

        if ready_count < len(loops):
            ready_count = self._orch.instant_ram_bootstrap_all_epics()

        if is_historical_allowance_lockout():
            log_engine("V5 hydrator: IG historical lockout — continuing on local cache only")
        log_engine(
            f"V5AutonomicBootstrap: background hydration complete "
            f"{ready_count}/{len(loops)} epics warm"
        )


def compute_rotation_trend_cleanliness(
    row_15m: pd.Series,
    *,
    atr_15m: float = 0.0,
    atr_5m: float = 0.0,
) -> float:
    """
    Direction-neutral trend strength for rotation rank_score.

    Bull (fast > slow, RSI > 50) and bear (fast < slow, RSI < 50) alignment
    score equally via ``score_trend_factor``. Momentum scales by |EMA gap|/ATR;
    volatility adds a mild boost from absolute ATR (tradable movement, not direction).
    """
    from trading.environment_scorer import score_trend_factor

    alignment = score_trend_factor(row_15m)
    if alignment <= 0:
        return _ROTATION_RANK_FLOOR

    fast = float(row_15m.get("fast_ema", 0))
    slow = float(row_15m.get("slow_ema", 0))
    atr_ref = float(atr_5m or atr_15m or row_15m.get("atr", 0) or 0)
    ema_gap = abs(fast - slow)

    if atr_ref > 0:
        momentum_mult = 0.5 + 0.5 * min(1.0, ema_gap / (2.0 * atr_ref))
        vol_mult = 1.0 + min(0.25, atr_ref / 80.0)
    else:
        momentum_mult = 0.5 + 0.5 * min(1.0, ema_gap / 40.0)
        vol_mult = 1.0

    return max(alignment * momentum_mult * vol_mult, _ROTATION_RANK_FLOOR)


def select_active_rotation_epics(
    ranked_assets: list[tuple[str, float]],
    *,
    base_slots: int = TOP_ROTATION_SLOTS,
    max_slots: int = MAX_ROTATION_SLOTS,
    expand_threshold_pct: float = ROTATION_EXPAND_THRESHOLD_PCT,
    min_online: int = ROTATION_MIN_ONLINE_FOR_FILTER,
) -> list[str]:
    """Build the active rotation window from pre-sorted volatility rank scores.

    Ranking source (same as ``MarketOrchestrator.refresh_active_epics``):
    ``rank_score = trend_cleanliness / relative_spread_cost``, where
    ``trend_cleanliness`` is 15m EMA+RSI alignment (bull or bear), momentum
    from |EMA gap|/ATR, and an ATR volatility boost (see
    ``compute_rotation_trend_cleanliness`` and ``_rotation_rank_score``).

    Default: top ``base_slots`` (3) epics. Expands up to ``max_slots`` (5) when
    4th/5th ranked assets have rank_score within ``expand_threshold_pct`` of the
    3rd-ranked score (score >= third * (1 - pct/100)).
    """
    if len(ranked_assets) < min_online:
        return [epic for epic, _ in ranked_assets]

    third_score = ranked_assets[2][1]
    threshold = third_score * (1.0 - expand_threshold_pct / 100.0)
    slot_count = base_slots
    for i in range(base_slots, min(max_slots, len(ranked_assets))):
        if ranked_assets[i][1] >= threshold:
            slot_count = i + 1
        else:
            break
    selected = [epic for epic, _ in ranked_assets[:slot_count]]
    try:
        from system.instrument_class import filter_rotation_epics

        selected = filter_rotation_epics(selected)
    except Exception:
        pass
    return selected


class MarketOrchestrator:
    """Starts/stops per-epic loops and publishes a merged multi-market tick."""

    def __init__(
        self,
        config: Config,
        loops: list[TradingLoop],
        *,
        primary_epic: str = "",
        enabled_epics: list[str] | None = None,
        instrument_meta: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self._config = config
        self._loops = list(loops)
        self._primary_epic = primary_epic or (loops[0]._epic if loops else "")
        loop_epics = [str(loop._epic) for loop in loops if getattr(loop, "_epic", "")]
        passed_enabled = list(enabled_epics or loop_epics)
        universe: list[str] = list(GLOBAL_ROTATION_UNIVERSE)
        for epic in passed_enabled:
            key = str(epic or "").strip()
            if key and key not in universe:
                universe.append(key)
        self._enabled_epics = universe
        self._instrument_meta = dict(instrument_meta or {})
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._running = False
        self._active_epics: list[str] = list(passed_enabled)
        self._active_epics_updated_at: float = 0.0
        self._feed_offline_epics: set[str] = set()
        self._last_rotation_log_key: tuple[Any, ...] | None = None
        self._last_rotation_log_ts: float = 0.0
        self._last_rotation_rank_snapshot: list[dict[str, Any]] = []
        self._v5_rest_client: Any | None = None
        self._v5_defer_ohlc = False
        self._v6_skeleton_mode = False
        self._v6_materialized = False
        self._v6_rest_client: Any | None = None
        self._v6_build_ctx: dict[str, Any] = {}
        self._v6_store: Any | None = None
        self._v6_points_engine: Any | None = None
        self._hydration_registry: dict[str, str] = {}

    def hydration_ready_count(self) -> int:
        return sum(1 for v in self._hydration_registry.values() if v == "HYDRATED")

    def publish_hydration_registry_progress(self) -> None:
        """Publish ohlc_epics_ready from RAM hydration registry (not broker ACK)."""
        try:
            from system.system_state import get_system_state

            total = len(self._loops)
            ready = self.hydration_ready_count()
            snap = get_system_state().snapshot_model()
            loops_patch: dict[str, Any] = {
                "built": total,
                "running": self._running,
            }
            if ready >= total and total > 0:
                loops_patch["accepting_ticks"] = True
            get_system_state().update_state(
                snap.phase,
                snap.percent,
                snap.phase_label,
                hydration={
                    "ohlc_epics_ready": ready,
                    "ohlc_epics_total": total,
                },
                loops=loops_patch,
            )
        except Exception as exc:
            log_engine(
                f"hydration registry publish skipped: {type(exc).__name__}: {exc}"
            )

    def backfill_indicators_from_volatile_buffer(
        self,
        *,
        epic: str,
        loop: Any,
    ) -> int:
        """
        RAM-Fallback Gasket — seed 30-period ATR from volatile tick tape / ring quotes.

        Un-bypassable when broker REST is rate-limited or unreachable.
        """
        market = str(getattr(loop, "_market", "") or epic)
        signal_engine = getattr(loop, "_signal_engine", None)
        env = getattr(loop, "_env", None)
        if signal_engine is None:
            self._hydration_registry[epic] = "HYDRATED"
            return 0
        from trading.ohlc_bootstrap import (
            MIN_CACHE_BARS_FOR_BOOTSTRAP,
            _bootstrap_from_cache,
            local_cache_max_bars,
            local_cache_ready,
        )

        max_bars = min(local_cache_max_bars(), V6_RAM_BACKFILL_TICKS)
        count = int(
            _bootstrap_from_cache(
                epic,
                market,
                signal_engine,
                env,
                max(V6_ATR_WINDOW, MIN_CACHE_BARS_FOR_BOOTSTRAP),
                max_bars=max_bars,
            )
            or 0
        )
        if count < V6_ATR_WINDOW:
            count = max(count, self._seed_signal_engine_from_ram_ticks(epic, market, loop))
        self._hydration_registry[epic] = "HYDRATED"
        if count > 0:
            _append_v6_audit(
                {
                    "event": "ram_atr_backfill",
                    "epic": epic,
                    "bars": count,
                    "atr_window": V6_ATR_WINDOW,
                    "ram_ticks": V6_RAM_BACKFILL_TICKS,
                    "cache_ready": local_cache_ready(epic, market),
                }
            )
            log_engine(
                f"RAM gasket: {epic} indicators warm bars={count} "
                f"(atr_window={V6_ATR_WINDOW})"
            )
        return count

    def _seed_signal_engine_from_ram_ticks(
        self,
        epic: str,
        market: str,
        loop: Any,
    ) -> int:
        """Build OHLC history from in-memory tick FIFO and live quote ring."""
        from datetime import datetime, timedelta

        from data.models import Quote

        signal_engine = loop._signal_engine
        env = getattr(loop, "_env", None)
        quotes: list[Quote] = []

        try:
            from trading.cache_reaper import volatile_tick_slots_for_epic

            for row in volatile_tick_slots_for_epic(epic):
                ts = float(row.get("ts") or time.time())
                bid = float(row.get("bid") or 0)
                offer = float(row.get("offer") or 0)
                if bid <= 0 or offer <= 0:
                    continue
                quotes.append(
                    Quote(
                        time=datetime.fromtimestamp(ts),
                        bid=bid,
                        offer=offer,
                    )
                )
        except Exception:
            pass

        if len(quotes) < V6_ATR_WINDOW:
            try:
                from system.ipc.ring_buffer import get_alpha_ring_buffer

                ring = get_alpha_ring_buffer()
                live = ring.read_quote_for_epic(epic)
                if live is not None:
                    bid, offer, _seq = live
                    if bid > 0 and offer > 0:
                        base = datetime.now()
                        need = max(100, V6_ATR_WINDOW + 1)
                        for i in range(need):
                            quotes.append(
                                Quote(
                                    time=base - timedelta(minutes=5 * (need - i)),
                                    bid=float(bid),
                                    offer=float(offer),
                                )
                            )
                if not quotes:
                    mat = ring.matrix_view()
                    _ = float(mat[0, 0])
            except Exception:
                pass

        if not quotes:
            return 0
        tail = quotes[-max(100, V6_ATR_WINDOW + 1) :]
        count = int(signal_engine.seed_ohlc_history(market, tail, aliases=[epic]) or 0)
        if count > 0 and env is not None:
            try:
                env.on_ohlc_bootstrapped(market)
            except Exception:
                pass
        return count

    def instant_ram_bootstrap_all_epics(self) -> int:
        """Synchronous RAM-only burst — drive hydration to 7/7 without broker REST."""
        warmed = 0
        for loop in self._loops:
            epic = str(getattr(loop, "_epic", "") or "")
            if not epic:
                continue
            self.backfill_indicators_from_volatile_buffer(epic=epic, loop=loop)
            self._hydration_registry[epic] = "HYDRATED"
            warmed += 1
        self.publish_hydration_registry_progress()
        total = len(self._loops)
        try:
            from system.system_state import get_system_state

            snap = get_system_state().snapshot_model()
            get_system_state().update_state(
                snap.phase,
                snap.percent,
                snap.phase_label,
                hydration={
                    "ohlc_epics_ready": warmed,
                    "ohlc_epics_total": total,
                },
            )
        except Exception:
            pass
        log_engine(
            f"instant_ram_bootstrap_all_epics: {warmed}/{total} HYDRATED"
        )
        return warmed

    def configure_v6_handoff(
        self,
        *,
        rest_client: Any | None = None,
        enabled: list[tuple[str, dict[str, Any]]] | None = None,
        paused_at_boot: bool = False,
        exec_mode: Any | None = None,
    ) -> None:
        """Arm V6 instant skeleton — hydration via asyncio after API bind."""
        self._v6_skeleton_mode = True
        self._v6_rest_client = rest_client
        self._v5_rest_client = rest_client
        self._v5_defer_ohlc = True
        self._v6_build_ctx = {
            "enabled": list(enabled or []),
            "paused_at_boot": paused_at_boot,
            "exec_mode": exec_mode,
        }

    def configure_v5_autonomic_bootstrap(
        self,
        *,
        rest_client: Any | None = None,
        defer_ohlc: bool = True,
    ) -> None:
        """Arm V5 async OHLC hydrator — call before ``start()`` when ``defer_ohlc``."""
        self._v5_rest_client = rest_client
        self._v5_defer_ohlc = bool(defer_ohlc)

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

    def _loop_providing_live_data(self, epic: str, loop: TradingLoop) -> bool:
        """True when epic has a fresh quote or a recent loop snapshot."""
        try:
            from system.market_data_hub import get_market_data_hub

            snap = get_market_data_hub().get_snapshot(epic)
            if (
                snap is not None
                and snap.bid > 0
                and snap.offer > 0
                and snap.age_seconds() <= _ROTATION_ONLINE_MAX_AGE_SEC
            ):
                return True
        except Exception:
            pass
        with self._lock:
            payload = self._snapshots.get(epic)
        if isinstance(payload, dict):
            bid = payload.get("bid")
            offer = payload.get("offer")
            if bid and offer and float(bid) > 0 and float(offer) > 0:
                return True
        return bool(getattr(loop, "_env", None) is not None)

    def _quote_age_seconds(self, epic: str) -> float | None:
        """Last hub quote age in seconds, or None when no live bid/offer."""
        try:
            from system.market_data_hub import get_market_data_hub

            snap = get_market_data_hub().get_snapshot(epic)
            if snap is None or snap.bid <= 0 or snap.offer <= 0:
                return None
            return float(snap.age_seconds())
        except Exception:
            return None

    def _loop_connection_active(self, epic: str, loop: TradingLoop) -> bool:
        """Instrument loop is running with an established (possibly stale) quote path."""
        if not loop.is_running() or loop._env is None:
            return False
        return self._quote_age_seconds(epic) is not None

    def _loop_for_epic(self, epic: str) -> TradingLoop | None:
        key = str(epic or "").strip()
        if not key:
            return None
        for loop in self._loops:
            if str(getattr(loop, "_epic", "") or "") == key:
                return loop
        return None

    def _apply_feed_circuit_breakers(self) -> set[str]:
        """
        Autonomous feed-stale interceptor — isolate starving epics in RAM only.

        Never stops sibling loops; only flags entry gates and ejects from rotation pool.
        """
        offline_now: set[str] = set()
        for loop in self._loops:
            epic = str(getattr(loop, "_epic", "") or "")
            if not epic:
                continue
            age = self._quote_age_seconds(epic)
            if not self._loop_connection_active(epic, loop):
                if epic in self._feed_offline_epics:
                    loop.clear_entry_circuit_breaker()
                continue
            if age is not None and age > _FEED_STARVATION_MAX_AGE_SEC:
                offline_now.add(epic)
                if epic not in self._feed_offline_epics:
                    log_engine(
                        f"CIRCUIT_BREAKER_ACTIVE | epic={epic} quote_age={age:.0f}s "
                        f"(>{_FEED_STARVATION_MAX_AGE_SEC:.0f}) — {OFFLINE_BROKER_FEED_REJECTED}"
                    )
                loop.set_entry_circuit_breaker(OFFLINE_BROKER_FEED_REJECTED)
            elif epic in self._feed_offline_epics and age is not None:
                if age <= _FEED_RECOVERY_MAX_AGE_SEC:
                    loop.clear_entry_circuit_breaker()
                    log_engine(
                        f"CIRCUIT_BREAKER_CLEARED | epic={epic} quote_age={age:.1f}s — feed restored"
                    )
                else:
                    offline_now.add(epic)
                    loop.set_entry_circuit_breaker(OFFLINE_BROKER_FEED_REJECTED)

        self._feed_offline_epics = offline_now
        return set(offline_now)

    def get_feed_offline_epics(self) -> list[str]:
        with self._lock:
            return sorted(self._feed_offline_epics)

    def _relative_spread_cost(self, epic: str, loop: TradingLoop) -> float:
        """Broker spread deviation — prefer env-scorer factor, else live quote ratio."""
        env = loop._env
        if env is not None and hasattr(env, "get_factors"):
            try:
                from trading.environment_scorer import FACTOR_SPREAD_MAX

                factors = env.get_factors()
                spread_factor = float(factors.get("spread") or 0.0)
                if spread_factor > 0:
                    return max(
                        FACTOR_SPREAD_MAX / spread_factor,
                        _ROTATION_RANK_FLOOR,
                    )
            except Exception:
                pass

        current_spread: float | None = None
        normal_spread: float | None = None
        try:
            from system.market_data_hub import get_market_data_hub

            snap = get_market_data_hub().get_snapshot(epic)
            if snap is not None and snap.bid > 0 and snap.offer > 0:
                current_spread = float(snap.offer) - float(snap.bid)
        except Exception:
            pass
        if current_spread is None:
            with self._lock:
                payload = self._snapshots.get(epic) or {}
            bid = payload.get("bid")
            offer = payload.get("offer")
            if bid is not None and offer is not None:
                try:
                    current_spread = float(offer) - float(bid)
                except (TypeError, ValueError):
                    current_spread = None
        if normal_spread is None or normal_spread <= 0:
            meta = self._instrument_meta.get(epic, {})
            cfg_pts = meta.get("max_spread_pts")
            if cfg_pts is not None:
                try:
                    normal_spread = float(cfg_pts)
                except (TypeError, ValueError):
                    normal_spread = None
            if normal_spread is None or normal_spread <= 0:
                normal_spread = max(
                    current_spread or _ROTATION_RANK_FLOOR, _ROTATION_RANK_FLOOR
                )
        if current_spread is None or current_spread <= 0:
            return max(normal_spread, _ROTATION_RANK_FLOOR)
        return max(
            current_spread / max(normal_spread, _ROTATION_RANK_FLOOR),
            _ROTATION_RANK_FLOOR,
        )

    def _trend_cleanliness(self, loop: TradingLoop) -> float:
        """15m EMA+RSI alignment (bull or bear) × |EMA gap| momentum × ATR vol."""
        engine = getattr(loop, "_signal_engine", None)
        market = str(getattr(loop, "_market", "") or "")
        if engine is not None and market:
            try:
                df = engine.quote_df(market)
                c15 = engine.candles(df, 15)
                if len(c15) >= 2:
                    c15i = engine.add_indicators(c15)
                    row15 = c15i.iloc[-2]
                    atr_15 = float(row15.get("atr", 0) or 0)
                    atr_5 = 0.0
                    c5 = engine.candles(df, 5)
                    if len(c5) >= 2:
                        c5i = engine.add_indicators(c5)
                        atr_5 = float(c5i.iloc[-2].get("atr", 0) or 0)
                    return compute_rotation_trend_cleanliness(
                        row15,
                        atr_15m=atr_15,
                        atr_5m=atr_5,
                    )
            except Exception:
                pass

        env = loop._env
        if env is None:
            return _ROTATION_RANK_FLOOR
        try:
            if hasattr(env, "get_factors"):
                factors = env.get_factors()
                trend = float(factors.get("trend") or 0.0)
                if trend > 0:
                    atr_pts = float(factors.get("atr") or 0.0)
                    vol_mult = 1.0 + min(0.25, atr_pts / 80.0) if atr_pts > 0 else 1.0
                    return max(trend * vol_mult, _ROTATION_RANK_FLOOR)
            last = getattr(env, "_last", None)
            fitness = float(getattr(last, "total", 0.0) or 0.0)
            if fitness > 0:
                return max(fitness * 0.25, _ROTATION_RANK_FLOOR)
        except Exception:
            pass
        return _ROTATION_RANK_FLOOR

    def _rotation_rank_score(self, epic: str, loop: TradingLoop) -> float:
        trend_cleanliness = self._trend_cleanliness(loop)
        relative_spread_cost = self._relative_spread_cost(epic, loop)
        return trend_cleanliness / relative_spread_cost

    def _strategy_session_eligible(self, epic: str) -> bool:
        """True when epic's instrument whitelist includes the current strategy session."""
        try:
            from signals.indicators import session_name
            from trading.instrument_registry import InstrumentRegistry

            wl = InstrumentRegistry(self._config.as_dict()).session_whitelist_for_epic(
                epic
            )
            if not wl:
                wl = list(getattr(self._config, "trading_session_whitelist", []) or [])
            if not wl:
                return True
            return session_name() in wl
        except Exception:
            return True

    def refresh_active_epics(self) -> list[str]:
        """Layer 3 Hot Market Selector — rank_score = trend_cleanliness / relative_spread_cost.

        trend_cleanliness uses absolute 15m trend alignment (bull or bear) and
        momentum/volatility scaling, not upward direction alone.
        """
        import time

        feed_offline = self._apply_feed_circuit_breakers()

        ranked_assets: list[tuple[str, float]] = []
        qmm_candidates: list[tuple[str, TradingLoop, float, float]] = []
        for loop in self._loops:
            epic = str(getattr(loop, "_epic", "") or "")
            if not epic or loop._env is None:
                continue
            if epic in feed_offline:
                continue
            if not self._loop_providing_live_data(epic, loop):
                continue
            if not self._strategy_session_eligible(epic):
                continue
            try:
                trend = self._trend_cleanliness(loop)
                spread_cost = self._relative_spread_cost(epic, loop)
                rank_score = max(trend / spread_cost, _ROTATION_RANK_FLOOR)
            except Exception:
                continue
            ranked_assets.append((epic, rank_score))
            qmm_candidates.append((epic, loop, trend, spread_cost))

        try:
            from trading.qmm_asset_selector import rank_qmm_epics

            if qmm_candidates:
                ranked_assets = rank_qmm_epics(qmm_candidates)
            else:
                ranked_assets.sort(key=lambda item: item[1], reverse=True)
        except Exception as e:
            from system.engine_log import log_engine

            log_engine(f"QMM rank fallback to legacy rotation: {type(e).__name__}: {e}")
            ranked_assets.sort(key=lambda item: item[1], reverse=True)

        cfg = self._config.as_dict() if hasattr(self._config, "as_dict") else {}
        base_slots = int(cfg.get("rotation_base_slots") or TOP_ROTATION_SLOTS)
        max_slots = int(cfg.get("rotation_max_slots") or MAX_ROTATION_SLOTS)
        expand_pct = float(
            cfg.get("rotation_expand_threshold_pct") or ROTATION_EXPAND_THRESHOLD_PCT
        )
        if len(ranked_assets) < ROTATION_MIN_ONLINE_FOR_FILTER:
            active = [epic for epic, _ in ranked_assets]
        else:
            active = select_active_rotation_epics(
                ranked_assets,
                base_slots=base_slots,
                max_slots=max_slots,
                expand_threshold_pct=expand_pct,
            )

        from system.engine_log import log_engine

        active_set = set(active)
        rank_rows: list[tuple[str, int, float, str]] = []
        for rank, (epic, score) in enumerate(ranked_assets, start=1):
            if epic in feed_offline:
                status = "MUTED"
            elif epic in active_set:
                status = "IN_TOP_3"
            else:
                status = "RANKED_OUT"
            rank_rows.append((epic, rank, score, status))

        log_key = (tuple(active), tuple(rank_rows))
        now = time.time()
        should_log = (
            log_key != self._last_rotation_log_key
            or (now - self._last_rotation_log_ts) >= _ROTATION_LOG_MIN_INTERVAL_SEC
        )
        if should_log and rank_rows:
            for epic, rank, score, status in rank_rows:
                log_engine(
                    f"[ROTATION RANK] {epic} score={score:.2f} rank={rank} status={status}"
                )
            self._last_rotation_log_key = log_key
            self._last_rotation_log_ts = now

        rank_snapshot = [
            {
                "epic": epic,
                "rank": rank,
                "score": round(float(score), 2),
                "status": status,
            }
            for epic, rank, score, status in rank_rows
        ]

        with self._lock:
            self._active_epics = active
            self._active_epics_updated_at = time.time()
            self._last_rotation_rank_snapshot = rank_snapshot
        return list(self._active_epics)

    def get_active_epics(self) -> list[str]:
        with self._lock:
            return list(self._active_epics)

    @staticmethod
    def get_global_active_epics() -> list[str]:
        global _ORCHESTRATOR_REF
        if _ORCHESTRATOR_REF is None:
            return []
        return _ORCHESTRATOR_REF.get_active_epics()

    @staticmethod
    def get_global_rotation_rank_snapshot() -> list[dict[str, Any]]:
        global _ORCHESTRATOR_REF
        if _ORCHESTRATOR_REF is None:
            return []
        with _ORCHESTRATOR_REF._lock:
            return list(_ORCHESTRATOR_REF._last_rotation_rank_snapshot)

    @staticmethod
    def get_signal_engine_for_market(market: str) -> Any | None:
        """Resolve the per-market SignalEngine from a running orchestrator loop."""
        global _ORCHESTRATOR_REF
        key = str(market or "").strip()
        if not key or _ORCHESTRATOR_REF is None:
            return None
        for loop in _ORCHESTRATOR_REF._loops:
            if str(getattr(loop, "_market", "") or "") == key:
                return getattr(loop, "_signal_engine", None)
        return None

    @staticmethod
    def hot_reload_config(config: Config | None = None) -> int:
        """Push reloaded Config into orchestrator + all trading loops (in-memory)."""
        global _ORCHESTRATOR_REF
        if _ORCHESTRATOR_REF is None:
            return 0
        from system.config_loader import get_config

        cfg = config or get_config(reload=True)
        _ORCHESTRATOR_REF._config = cfg
        for loop in _ORCHESTRATOR_REF._loops:
            loop._config = cfg
        return len(_ORCHESTRATOR_REF._loops)

    def start(self) -> None:
        if self._running:
            return
        if self._v6_skeleton_mode and not self._v6_materialized:
            log_engine(
                "V6InstantBuild: start() deferred — awaiting coroutine handoff post-bind"
            )
            return
        sm = AutonomicBootstrapStateMachine(self)
        sm.allocate_and_ready()
        sm.start_live_channels()
        sm.spawn_background_hydrator()

    def _start_live_channels_impl(self) -> None:
        if self._running:
            return
        try:
            from trading.trading_loop import force_reset_session_correlation_counters

            force_reset_session_correlation_counters(reason="orchestrator_boot")
        except Exception as e:
            log_engine(
                f"market_orchestrator: correlation purge on boot failed: "
                f"{type(e).__name__}: {e}"
            )
        try:
            from trading.entry_protection import log_ml_insufficient_data_warning

            log_ml_insufficient_data_warning(self._config)
        except Exception:
            pass
        try:
            from system.market_data_hub import get_market_data_hub
            from system.stream_ready import is_stream_ready, signal_stream_ready

            if not is_stream_ready():
                hub = get_market_data_hub()
                for epic in self._enabled_epics:
                    snap = hub.get_snapshot(epic)
                    if (
                        snap is not None
                        and snap.bid > 0
                        and snap.offer > 0
                        and snap.age_seconds() <= 30.0
                    ):
                        signal_stream_ready(source=f"orchestrator_start:{epic}")
                        break
        except Exception as e:
            log_engine(
                f"market_orchestrator stream_ready preflight failed: "
                f"{type(e).__name__}: {e}"
            )
        self._stop.clear()
        self._running = True
        try:
            from system.market_watch.market_status_updater import (
                ensure_market_status_updater_started,
            )

            epics = [
                str(loop._epic) for loop in self._loops if getattr(loop, "_epic", "")
            ]
            rest_client = None
            primary = self.primary
            if primary is not None:
                rest_fn = getattr(primary, "_rest_client", None)
                if callable(rest_fn):
                    rest_client = rest_fn()
            ensure_market_status_updater_started(
                epics=epics,
                rest_client=rest_client,
            )
        except Exception as e:
            log_engine(
                f"market_status_updater start skipped: {type(e).__name__}: {e}"
            )
        for loop in self._loops:
            loop.start()
        log_engine(
            f"market_orchestrator started ({len(self._loops)} loops) "
            f"primary={self._primary_epic}"
        )
        self._health_monitor_thread = threading.Thread(
            target=self._loop_health_monitor,
            name="ig-orchestrator-health",
            daemon=True,
        )
        self._health_monitor_thread.start()
        try:
            rest_client = None
            primary = self.primary
            if primary is not None:
                rest_fn = getattr(primary, "_rest_client", None)
                if callable(rest_fn):
                    rest_client = rest_fn()
            from trading.cache_reaper import start_v2_cache_reaper

            start_v2_cache_reaper(rest_client, config=self._config)
        except Exception as e:
            log_engine(
                f"market_orchestrator: V2CacheReaper start failed: {type(e).__name__}: {e}"
            )
        try:
            from intelligence.telemetry_daemon import start_v2_telemetry_daemon

            start_v2_telemetry_daemon(config=self._config)
        except Exception as e:
            log_engine(
                f"market_orchestrator: V2TelemetryDaemon start failed: {type(e).__name__}: {e}"
            )

    def unpause_from_boot(self) -> None:
        """Release dormant loops after SystemState READY (Gate 5)."""
        for loop in self._loops:
            unpause = getattr(loop, "unpause_from_boot", None)
            if callable(unpause):
                unpause()
        log_engine(f"market_orchestrator: {len(self._loops)} loop(s) released from boot pause")

    def _loop_health_monitor(self) -> None:
        """Detect and respawn individual trading loops that stopped due to deadlock."""
        import time

        from system.engine_log import log_engine

        check_interval = 20.0
        rotation_interval = 60.0
        last_rotation_mono = 0.0
        respawn_cooldown: dict[str, float] = {}
        zombie_alert_sent = False

        while not self._stop.wait(check_interval):
            if not self._running:
                break
            now_mono = time.monotonic()
            if now_mono - last_rotation_mono >= rotation_interval:
                try:
                    self.refresh_active_epics()
                    last_rotation_mono = now_mono
                except Exception as e:
                    log_engine(
                        f"QMM rotation refresh failed: {type(e).__name__}: {e}"
                    )
            any_running = any(loop.is_running() for loop in self._loops)
            if self._running and self._loops and not any_running:
                if not zombie_alert_sent:
                    zombie_alert_sent = True
                    log_engine(
                        "CRITICAL: all trading loops stopped while orchestrator running"
                    )
                    try:
                        from system.telegram_notifier import send_critical_alert

                        send_critical_alert(
                            "⚠️ Trading loops STOPPED — no trades firing"
                        )
                    except Exception as e:
                        log_engine(
                            f"telegram zombie-loop alert failed: {type(e).__name__}: {e}"
                        )
            else:
                zombie_alert_sent = False
            for loop in self._loops:
                if self._stop.is_set():
                    break
                if loop.is_running():
                    continue
                epic = getattr(loop, "_epic", "?")
                market = getattr(loop, "_market", epic)
                last_respawn = respawn_cooldown.get(epic, 0.0)
                if time.monotonic() - last_respawn < 30.0:
                    continue
                respawn_cooldown[epic] = time.monotonic()
                log_engine(
                    f"Orchestrator health monitor: respawning stopped loop "
                    f"market={market} epic={epic}"
                )
                try:
                    from system.telegram_notifier import get_telegram_notifier

                    notifier = get_telegram_notifier()
                    if notifier is not None:
                        notifier.send_alert(
                            f"🔄 Auto-respawning {market} loop after deadlock",
                            dedupe_key=f"respawn:{epic}",
                        )
                except Exception:
                    pass
                try:
                    loop.start()
                except Exception as e:
                    log_engine(f"Orchestrator respawn failed for {epic}: {e}")

    def stop(self) -> None:
        self._stop.set()
        try:
            from intelligence.telemetry_daemon import stop_v2_telemetry_daemon

            stop_v2_telemetry_daemon()
        except Exception as e:
            log_engine(
                f"market_orchestrator: V2TelemetryDaemon stop failed: {type(e).__name__}: {e}"
            )
        try:
            from trading.cache_reaper import stop_v2_cache_reaper

            stop_v2_cache_reaper()
        except Exception as e:
            log_engine(f"market_orchestrator: V2CacheReaper stop failed: {type(e).__name__}: {e}")
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
        self.refresh_active_epics()
        self._publish_merged()

    def _placeholder_market_slice(self, epic: str) -> dict[str, Any]:
        """Minimal per-market tick when a loop has not published yet (tab stays visible)."""
        meta = self._instrument_meta.get(epic, {})
        label = str(meta.get("name") or epic)
        instrument_id = str(meta.get("instrument_id") or "")
        bid: float | None = None
        offer: float | None = None
        tick_age_s: float | None = None
        stream_status = "DISCONNECTED"
        try:
            from system.market_data_hub import get_market_data_hub

            snap = get_market_data_hub().get_snapshot(epic)
            if snap is not None and snap.bid > 0 and snap.offer > 0:
                bid = float(snap.bid)
                offer = float(snap.offer)
                tick_age_s = float(snap.age_seconds())
                stream_status = "LIVE"
        except Exception:
            pass
        spread = round(float(offer) - float(bid), 5) if bid and offer else None
        return {
            "type": "tick",
            "epic": epic,
            "market": label,
            "instrument_id": instrument_id,
            "ts": _iso_now(),
            "market_state": "OPEN" if bid and offer else "OFFLINE",
            "bid": bid,
            "offer": offer,
            "spread": spread,
            "tick_age_s": tick_age_s,
            "stream_status": stream_status,
            "health": {
                "badge": "WATCHING",
                "badge_text": "Awaiting loop snapshot",
                "gates": [],
                "summary": "Loop snapshot pending — stream may still be live",
            },
            "signal": {
                "direction": "WAIT",
                "confidence": 0,
                "fitness": 0,
                "setup": "",
            },
            "positions": [],
        }

    def _markets_for_dashboard(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            markets = {k: dict(v) for k, v in self._snapshots.items()}
        for epic in self._enabled_epics:
            if epic and epic not in markets:
                markets[epic] = self._placeholder_market_slice(epic)
        return markets

    def _publish_merged(self) -> None:
        markets = self._markets_for_dashboard()
        if not markets:
            return
        from trading.open_position_view import epic_market_label

        primary = markets.get(self._primary_epic) or next(iter(markets.values()))
        merged = dict(primary)
        merged["markets"] = markets
        enabled = list(self._enabled_epics or markets.keys())
        merged["enabled_epics"] = enabled
        merged["instrument_labels"] = {
            epic: epic_market_label(epic) for epic in enabled
        }
        # Union epic-scoped closed trades from each slice (dedupe by deal_id).
        closed_union: list[dict[str, Any]] = []
        seen_closed: set[str] = set()
        for epic_key in enabled:
            mslice = markets.get(epic_key) or {}
            for row in mslice.get("closed_trades") or []:
                if not isinstance(row, dict):
                    continue
                deal_key = str(
                    row.get("deal_id")
                    or row.get("ig_deal_id")
                    or f"{row.get('epic')}-{row.get('closed_at')}"
                )
                if deal_key in seen_closed:
                    continue
                seen_closed.add(deal_key)
                closed_union.append(row)
        closed_union.sort(
            key=lambda r: str(r.get("closed_at") or r.get("time") or ""),
            reverse=True,
        )
        merged["closed_trades"] = closed_union[:100]
        merged["selected_epic"] = self._primary_epic
        merged["orchestrator"] = {
            "loop_count": len(self._loops),
            "primary_epic": self._primary_epic,
            "active_epics": self.get_active_epics(),
            "feed_offline_epics": self.get_feed_offline_epics(),
        }
        try:
            from system.gate_relaxation import relaxation_snapshot

            merged["gate_relaxations"] = relaxation_snapshot()
        except Exception:
            pass
        try:
            publish_tick(merged)
        except Exception as e:
            log_engine(f"publish_tick merged failed: {type(e).__name__}: {e}")


def attach_snapshot_handlers(orchestrator: MarketOrchestrator) -> None:
    """Wire each loop to feed the orchestrator merge publisher."""
    global _ORCHESTRATOR_REF
    _ORCHESTRATOR_REF = orchestrator
    handler: Callable[[dict[str, Any]], None] = orchestrator.on_market_snapshot
    for loop in orchestrator.loops:
        loop._on_snapshot = handler
        loop._publish_snapshots = False
    orchestrator.refresh_active_epics()
