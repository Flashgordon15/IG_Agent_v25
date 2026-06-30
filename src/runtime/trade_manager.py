"""
Closed-loop broker ledger reconciliation for trailing / profit-taking paths.

Every trailing PUT is preceded by a synchronous GET /positions/otc ledger pull.
Local cache or SQLite rows alone are never sufficient to authorize stop modification.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from analytics.triage_db import connect_triage_sqlite
from system.engine_log import log_engine
from trading.open_position_view import extract_broker_profit_and_loss, unrealized_from_quote

DRIFT_ADVISORY_PCT = 5.0
DRIFT_FATAL_PCT = 15.0
_TRIAGE_STATUS_ANOMALY = "CLOSED_ON_BROKER_ANOMALY"

_PRODUCTION_ORDERS_DDL = """
CREATE TABLE IF NOT EXISTS production_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_reference TEXT NOT NULL UNIQUE,
    deal_id TEXT,
    epic TEXT NOT NULL,
    direction TEXT NOT NULL,
    size REAL NOT NULL,
    status TEXT NOT NULL,
    broker_payload TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_production_orders_deal_id ON production_orders(deal_id);
CREATE INDEX IF NOT EXISTS idx_production_orders_status ON production_orders(status);
"""


def _persist_micro_production_order(
    *,
    deal_reference: str,
    deal_id: str | None,
    epic: str,
    direction: str,
    size: float,
    status: str,
    broker_payload: dict[str, Any],
) -> None:
    db = _triage_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_triage_sqlite(db)
    try:
        conn.executescript(_PRODUCTION_ORDERS_DDL)
        conn.execute(
            """
            INSERT OR REPLACE INTO production_orders
                (deal_reference, deal_id, epic, direction, size, status, broker_payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                deal_reference,
                deal_id,
                epic,
                direction.upper(),
                float(size),
                status,
                json.dumps(broker_payload, default=str),
            ),
        )
        conn.commit()
    finally:
        conn.close()


class PreflightVerdict(str, Enum):
    OK = "ok"
    MISSING_ON_BROKER = "missing_on_broker"
    DRIFT_ADVISORY = "drift_advisory"
    DRIFT_FATAL = "drift_fatal"
    LEDGER_UNAVAILABLE = "ledger_unavailable"


@dataclass(frozen=True)
class BrokerPositionSnapshot:
    deal_id: str
    epic: str
    direction: str
    level: float
    size: float
    stop_level: float | None
    limit_level: float | None
    broker_pnl_gbp: float | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class PreflightResult:
    verdict: PreflightVerdict
    deal_id: str
    broker_row: BrokerPositionSnapshot | None = None
    local_pnl_gbp: float | None = None
    broker_pnl_gbp: float | None = None
    drift_pct: float | None = None
    detail: str = ""


def _triage_db_path() -> Path:
    raw = os.environ.get("IG_TRIAGE_DB", "").strip()
    if raw:
        return Path(raw).resolve()
    return Path(__file__).resolve().parents[1] / "analytics" / "triage_v31.db"


def fetch_broker_ledger_sync(rest_client: Any) -> dict[str, BrokerPositionSnapshot]:
    """
    Mandatory synchronous GET /positions/otc — returns dealId-indexed broker truth.
    """
    if rest_client is None or not hasattr(rest_client, "open_positions"):
        return {}
    rows = rest_client.open_positions() or []
    ledger: dict[str, BrokerPositionSnapshot] = {}
    for item in rows:
        pos = item.get("position") or {}
        mkt = item.get("market") or {}
        deal_id = str(pos.get("dealId") or pos.get("dealID") or "").strip()
        if not deal_id:
            continue
        upl, _ = extract_broker_profit_and_loss(pos)
        if upl is None:
            upl, _ = extract_broker_profit_and_loss(item)
        try:
            level = float(pos.get("level") or pos.get("openLevel") or 0)
        except (TypeError, ValueError):
            level = 0.0
        try:
            size = float(pos.get("size") or 0)
        except (TypeError, ValueError):
            size = 0.0
        stop_raw = pos.get("stopLevel")
        limit_raw = pos.get("limitLevel")
        ledger[deal_id] = BrokerPositionSnapshot(
            deal_id=deal_id,
            epic=str(mkt.get("epic") or ""),
            direction=str(pos.get("direction") or "BUY").upper(),
            level=level,
            size=size,
            stop_level=float(stop_raw) if stop_raw not in (None, "", 0) else None,
            limit_level=float(limit_raw) if limit_raw not in (None, "", 0) else None,
            broker_pnl_gbp=float(upl) if upl is not None else None,
            raw=item,
        )
    return ledger


def _pnl_drift_pct(local_gbp: float, broker_gbp: float) -> float:
    denom = max(abs(float(broker_gbp)), abs(float(local_gbp)), 1.0)
    return abs(float(local_gbp) - float(broker_gbp)) / denom * 100.0


def mark_triage_closed_on_broker_anomaly(deal_id: str) -> None:
    if not deal_id:
        return
    db = _triage_db_path()
    if not db.is_file():
        return
    try:
        conn = connect_triage_sqlite(db)
        conn.execute(
            """
            UPDATE production_orders
            SET status = ?
            WHERE deal_id = ? AND status NOT IN ('CLOSED_ON_BROKER_ANOMALY', 'FAILED')
            """,
            (_TRIAGE_STATUS_ANOMALY, str(deal_id)),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        log_engine(
            f"broker_reconcile: triage mark failed deal={deal_id}: "
            f"{type(exc).__name__}: {exc}"
        )


def handle_critical_state_mismatch(
    *,
    deal_id: str,
    epic: str = "",
    trade_id: int | None = None,
    flush_callback: Any | None = None,
    store: Any | None = None,
) -> None:
    """Flush zombie local state, update triage ledger, trip master kill-switch."""
    mark_triage_closed_on_broker_anomaly(deal_id)
    if store is not None:
        try:
            from runtime.active_lifecycle_trades import close_lifecycle_deal

            close_lifecycle_deal(
                store,
                deal_id=str(deal_id),
                reason="CLOSED_ON_BROKER_ANOMALY",
            )
        except Exception as exc:
            log_engine(
                f"broker_reconcile: lifecycle close failed deal={deal_id}: "
                f"{type(exc).__name__}: {exc}"
            )
    if flush_callback is not None:
        try:
            flush_callback(deal_id=deal_id, epic=epic, trade_id=trade_id)
        except Exception as exc:
            log_engine(
                f"broker_reconcile: flush callback failed deal={deal_id}: "
                f"{type(exc).__name__}: {exc}"
            )
    from runtime.strategy_kill_switch import trip_master_strategy_kill_switch

    trip_master_strategy_kill_switch(
        deal_id=deal_id,
        reason="position_missing_on_broker_ledger",
    )


def preflight_trailing_cycle(
    *,
    rest_client: Any,
    deal_id: str,
    epic: str,
    side: str,
    entry: float,
    size: float,
    quote: Any,
    ledger: Mapping[str, BrokerPositionSnapshot] | None = None,
    flush_callback: Any | None = None,
    trade_id: int | None = None,
) -> PreflightResult:
    """
    Closed-loop audit immediately before trailing / profit-taking execution.

    When ``ledger`` is None a fresh GET /positions/otc is forced.
    """
    want = str(deal_id or "").strip()
    if not want:
        return PreflightResult(
            verdict=PreflightVerdict.LEDGER_UNAVAILABLE,
            deal_id="",
            detail="empty deal_id",
        )

    from runtime.strategy_kill_switch import is_strategy_kill_active

    if is_strategy_kill_active():
        return PreflightResult(
            verdict=PreflightVerdict.LEDGER_UNAVAILABLE,
            deal_id=want,
            detail="strategy_kill_switch_active",
        )

    try:
        active = ledger if ledger is not None else fetch_broker_ledger_sync(rest_client)
    except Exception as exc:
        log_engine(
            f"broker_reconcile: ledger fetch failed deal={want}: "
            f"{type(exc).__name__}: {exc}"
        )
        return PreflightResult(
            verdict=PreflightVerdict.LEDGER_UNAVAILABLE,
            deal_id=want,
            detail=str(exc),
        )

    row = active.get(want)
    if row is None:
        log_engine(
            f"broker_reconcile: CRITICAL STATE MISMATCH deal={want} epic={epic} "
            f"— absent from broker ledger ({len(active)} open)"
        )
        handle_critical_state_mismatch(
            deal_id=want,
            epic=epic,
            trade_id=trade_id,
            flush_callback=flush_callback,
        )
        return PreflightResult(
            verdict=PreflightVerdict.MISSING_ON_BROKER,
            deal_id=want,
            detail="deal_id not in broker GET /positions/otc",
        )

    local_pnl_gbp: float | None = None
    broker_pnl_gbp = row.broker_pnl_gbp
    drift_pct: float | None = None

    if quote is not None and broker_pnl_gbp is not None:
        try:
            _, _, local_pnl_gbp = unrealized_from_quote(
                side,
                float(entry),
                float(size),
                quote,
                epic=str(epic or row.epic),
            )
            drift_pct = _pnl_drift_pct(local_pnl_gbp, broker_pnl_gbp)
            if drift_pct > DRIFT_FATAL_PCT:
                log_engine(
                    f"[FINANCIAL DRIFT FATAL] dealId={want} PnL discrepancy "
                    f"{drift_pct:.1f}%. Local: {local_pnl_gbp:.2f}, "
                    f"Broker: {broker_pnl_gbp:.2f}"
                )
                from runtime.strategy_kill_switch import trip_master_strategy_kill_switch
                from system.telegram_notifier import send_critical_alert

                trip_master_strategy_kill_switch(
                    deal_id=want,
                    reason=f"pnl_drift_fatal_{drift_pct:.1f}pct",
                )
                send_critical_alert(
                    f"🚨 FINANCIAL DRIFT FATAL: dealId={want} "
                    f"Local PnL={local_pnl_gbp:.2f} Broker PnL={broker_pnl_gbp:.2f} "
                    f"({drift_pct:.1f}% variance). Strategy loop frozen.",
                    dedupe_key=f"pnl_drift_fatal:{want}",
                )
                return PreflightResult(
                    verdict=PreflightVerdict.DRIFT_FATAL,
                    deal_id=want,
                    broker_row=row,
                    local_pnl_gbp=local_pnl_gbp,
                    broker_pnl_gbp=broker_pnl_gbp,
                    drift_pct=drift_pct,
                )
            if drift_pct > DRIFT_ADVISORY_PCT:
                log_engine(
                    f"[FINANCIAL DRIFT WARNING] dealId={want} PnL discrepancy "
                    f"detected. Local: {local_pnl_gbp:.2f}, Broker: {broker_pnl_gbp:.2f} "
                    f"({drift_pct:.1f}%)"
                )
                return PreflightResult(
                    verdict=PreflightVerdict.DRIFT_ADVISORY,
                    deal_id=want,
                    broker_row=row,
                    local_pnl_gbp=local_pnl_gbp,
                    broker_pnl_gbp=broker_pnl_gbp,
                    drift_pct=drift_pct,
                )
        except Exception as exc:
            log_engine(
                f"broker_reconcile: pnl compare skipped deal={want}: "
                f"{type(exc).__name__}: {exc}"
            )

    return PreflightResult(
        verdict=PreflightVerdict.OK,
        deal_id=want,
        broker_row=row,
        local_pnl_gbp=local_pnl_gbp,
        broker_pnl_gbp=broker_pnl_gbp,
        drift_pct=drift_pct,
    )


def forensic_put_404_recovery(
    *,
    rest_client: Any,
    deal_id: str,
    payload_keys: list[str],
) -> dict[str, BrokerPositionSnapshot]:
    """
    Secondary ledger sync after unexpected PUT 404 despite pre-flight pass.
    """
    from system.shutdown_cleanup import notify_position_state_change

    log_engine(
        f"broker_reconcile: PUT 404 forensic deal={deal_id} "
        f"payload_keys={payload_keys} — forcing secondary ledger sync"
    )
    notify_position_state_change(reason=f"put_404_forensic:{deal_id}")
    try:
        lock = Path(__file__).resolve().parents[1] / "data" / ".ig_agent_v29.lock"
        if lock.is_file():
            log_engine(f"broker_reconcile: clearing stale instance lock {lock}")
            lock.unlink(missing_ok=True)
    except OSError as exc:
        log_engine(f"broker_reconcile: lock clear failed: {exc}")
    return fetch_broker_ledger_sync(rest_client)


# ── Dual-core parallel scalper track (ENGINE_B_MICRO_SCALPER) ─────────────────


class DualCoreCoordinator:
    """
    Non-blocking secondary execution track — mean-reversion micro scalper
    parallel to Macro Breakout Sentinel. Runs on a dedicated daemon thread
    with async order dispatch via a single-worker executor (REST-budget aware).
    """

    def __init__(
        self,
        *,
        rest_client: Any,
        config: Any | None = None,
        poll_interval_sec: float = 0.5,
        order_cadence_sec: float | None = None,
    ) -> None:
        self._rest = rest_client
        self._cfg = config
        self._poll_interval_sec = max(0.5, float(poll_interval_sec))
        if order_cadence_sec is None and config is not None:
            try:
                exec_raw = config.get("execution", {}) if hasattr(config, "get") else {}
                order_cadence_sec = float(
                    exec_raw.get("order_cadence_sec", 20.0)
                    if isinstance(exec_raw, dict)
                    else 20.0
                )
            except Exception:
                order_cadence_sec = 20.0
        raw_cadence = float(order_cadence_sec if order_cadence_sec is not None else 20.0)
        self._order_cadence_sec = 0.0 if raw_cadence <= 0 else max(5.0, raw_cadence)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_order_at: dict[str, float] = {}
        from concurrent.futures import ThreadPoolExecutor

        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="micro-scalper")

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="dual-core-micro-scalper",
            daemon=True,
        )
        self._thread.start()
        log_engine(
            f"DualCoreCoordinator: ENGINE_B_MICRO_SCALPER track started "
            f"(poll={self._poll_interval_sec}s cadence={self._order_cadence_sec}s)"
        )

    def stop(self) -> None:
        self._stop.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def apply_unlimited_order_cadence(self) -> None:
        """Disable per-epic order spacing for the current session."""
        self._order_cadence_sec = 0.0
        log_engine("DualCoreCoordinator: order cadence disabled (unlimited)")

    async def _async_loop(self) -> None:
        """Coordinator arms dispatch bridge — hardened sweep runs on stacked-dual thread."""
        while not self._stop.is_set():
            try:
                await asyncio.sleep(self._poll_interval_sec)
            except asyncio.CancelledError:
                break

    def _loop(self) -> None:
        """Legacy sync entry — retained for tests; production uses _async_loop."""
        from runtime.dual_core_execution import (
            get_stacked_snapshots,
            is_api_trading_paused,
            is_forex_failover_active,
            refresh_stacked_dual_assets,
        )

        while not self._stop.is_set():
            try:
                from runtime.dual_core_execution import validate_socket_heartbeat

                validate_socket_heartbeat()
                refresh_stacked_dual_assets(cfg=self._cfg)
                if not is_api_trading_paused() and is_forex_failover_active():
                    try:
                        from trading.continuous_optimization_worker import (
                            get_continuous_optimization_worker,
                        )

                        worker = get_continuous_optimization_worker()
                        if worker.is_sovereignty_active() or is_forex_failover_active():
                            from system.market_data_hub import get_market_data_hub
                            from runtime.dual_core_execution import FAILOVER_TOP_EPIC

                            hub = get_market_data_hub()
                            quote = hub.get_snapshot(FAILOVER_TOP_EPIC)
                            if quote is not None and quote.bid > 0 and quote.offer > 0:
                                spread = float(quote.offer) - float(quote.bid)
                                worker.run_failover_sovereignty(
                                    epic=FAILOVER_TOP_EPIC,
                                    spread=spread,
                                )
                    except Exception as exc:
                        log_engine(
                            f"DualCoreCoordinator: ML sovereignty cycle "
                            f"{type(exc).__name__}: {exc}"
                        )
                if not is_api_trading_paused():
                    for _epic, snap in get_stacked_snapshots().items():
                        if snap.core_b_micro_active:
                            self._scan_micro_entries(snap)
            except Exception as exc:
                log_engine(
                    f"DualCoreCoordinator: loop error {type(exc).__name__}: {exc}"
                )
            self._stop.wait(self._poll_interval_sec)

    def _scan_micro_entries(self, snap: Any) -> None:
        from runtime.dual_core_execution import evaluate_micro_scalp_signal
        from system.market_data_hub import get_market_data_hub

        hub = get_market_data_hub()
        quote = hub.get_snapshot(snap.epic)
        if quote is None or quote.bid <= 0 or quote.offer <= 0:
            return
        direction = evaluate_micro_scalp_signal(
            epic=snap.epic,
            bid=float(quote.bid),
            offer=float(quote.offer),
            snap=snap,
        )
        if direction is None:
            return
        from runtime.dual_core_execution import is_core_b_satellite_uncoupled, set_last_gate_suppression_reason

        if not is_core_b_satellite_uncoupled():
            from runtime.dual_core_execution import macro_15min_trend_allows_direction

            if not macro_15min_trend_allows_direction(direction, snap.epic):
                set_last_gate_suppression_reason("15m_macro_trend_lock")
                return
        else:
            set_last_gate_suppression_reason("")
        if self._order_cadence_sec > 0:
            now = time.time()
            last = self._last_order_at.get(snap.epic, 0.0)
            if now - last < self._order_cadence_sec:
                return
            self._last_order_at[snap.epic] = now
        self._executor.submit(self._dispatch_micro_order, snap.epic, direction)

    def _dispatch_micro_order(self, epic: str, direction: str) -> None:
        log_engine(
            f"DualCoreCoordinator: _dispatch_micro_order enter epic={epic} dir={direction}"
        )
        from runtime.dual_core_execution import (
            ENGINE_B_MICRO_SCALPER,
            canary_lot_size,
            resolve_micro_stop_limit_points,
            set_last_gate_suppression_reason,
        )
        from system.qmm_process_supervisor import process_entry_blocked

        def _block(code: str) -> None:
            set_last_gate_suppression_reason(code)
            log_engine(
                f"DualCoreCoordinator: dispatch blocked epic={epic} reason={code}"
            )

        from execution.ig_execution_guard import ig_execution_allowed, log_execution_paused_if_needed

        allowed, ig_reason = ig_execution_allowed()
        if not allowed:
            _block(ig_reason)
            log_execution_paused_if_needed(ig_reason)
            return

        from runtime.strategy_kill_switch import is_strategy_kill_active

        from runtime.strategy_controller import guard_micro_dispatch

        if not guard_micro_dispatch(epic):
            _block("blocked_by_strategy_controller")
            return

        from runtime.hard_enforcement import hard_guard_micro_dispatch, is_hard_enforcement_active

        if not hard_guard_micro_dispatch(epic):
            _block("hard_blocked_by_strategy_enforcement")
            return

        from runtime.strategy_enforcement import soft_guard_micro_dispatch

        if not is_hard_enforcement_active(epic) and not soft_guard_micro_dispatch(epic):
            _block("soft_blocked_by_strategy_enforcement")
            return

        from runtime.unified_execution import unified_guard_micro_dispatch

        if not unified_guard_micro_dispatch(epic):
            _block("blocked_by_unified_execution_route")
            return

        if is_strategy_kill_active():
            try:
                from system.demo_execution_plane import demo_throughput_active

                if not demo_throughput_active(self._cfg):
                    _block("BROKER_STATE_MISMATCH")
                    return
            except Exception:
                _block("BROKER_STATE_MISMATCH")
                return
        blocked, reason = process_entry_blocked()
        if blocked:
            _block(reason or "process_entry_blocked")
            return
        try:
            from runtime.broker_reject_guard import broker_reject_dispatch_blocked

            latched, latch_reason = broker_reject_dispatch_blocked()
            if latched:
                _block(latch_reason)
                return
        except Exception:
            pass
        try:
            from data.learning_store import LearningStore
            from runtime.live_canary_guards import canary_micro_dispatch_risk_ok

            _db = str(getattr(self._cfg, "learning_db", "") or "")
            _store = LearningStore(_db) if _db else None
            risk_ok, risk_reason = canary_micro_dispatch_risk_ok(_store, self._cfg)
            if not risk_ok:
                _block(risk_reason)
                return
        except Exception:
            pass
        try:
            from api.agent_control import is_paused

            if is_paused():
                _block("api_trading_paused")
                return
        except Exception:
            pass
        if self._rest is None:
            _block("rest_client_unavailable")
            return
        try:
            from runtime.agent_bootstrap import get_ig_position_sync

            sync = get_ig_position_sync()
            if sync is not None:
                if sync.total_open() >= 1:
                    _block("position_already_open")
                    return
        except Exception as exc:
            log_engine(
                f"DualCoreCoordinator: position sync check failed epic={epic}: "
                f"{type(exc).__name__}: {exc}"
            )
        try:
            from system.rest_api_budget import RestBudgetPausedError, get_rest_api_budget

            get_rest_api_budget().acquire(label="micro_scalper_place", priority=True)
        except RestBudgetPausedError:
            set_last_gate_suppression_reason("rest_budget_preemptive_pause")
            log_engine(f"DualCoreCoordinator: REST budget pause — micro scalp deferred epic={epic}")
            return
        except Exception as exc:
            log_engine(
                f"DualCoreCoordinator: REST acquire failed epic={epic}: "
                f"{type(exc).__name__}: {exc}"
            )
            return

        size = canary_lot_size(epic, self._cfg)
        from execution.broker_epic_resolver import resolve_account_product, resolve_order_epic

        broker_product = resolve_account_product(rest=self._rest, cfg=self._cfg)
        broker_epic = resolve_order_epic(epic, account_product=broker_product)
        deal_ref = f"MICRO-{epic[-8:]}-{int(time.time())}"
        from execution.ig_size_validator import pre_trade_check

        check = pre_trade_check(
            epic,
            size,
            direction,
            self._cfg,
            self._rest,
            broker_epic=broker_epic,
        )
        if check.get("status") == "blocked":
            set_last_gate_suppression_reason(f"size_validation:{check.get('reason')}")
            log_engine(
                f"DualCoreCoordinator: pre_trade blocked epic={epic} "
                f"reason={check.get('reason')}"
            )
            return
        size = float(check.get("adjusted_size") or size)
        tp_pts, sl_pts = resolve_micro_stop_limit_points(
            self._rest, broker_epic, size=size, cfg=self._cfg
        )
        log_engine(
            f"DualCoreCoordinator: {ENGINE_B_MICRO_SCALPER} {direction} epic={epic} "
            f"broker_product={broker_product} broker_epic={broker_epic} "
            f"size={size} tp={tp_pts} sl={sl_pts} z-mode=compressed"
        )
        try:
            from runtime.trade_lifecycle import begin_trade, transition, LifecycleState
            from system.unified_runtime_state import update_execution

            begin_trade(
                deal_id=deal_ref,
                epic=epic,
                direction=direction,
                size=size,
                ref=deal_ref,
            )
            update_execution(last_dispatch_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        except Exception:
            pass
        try:
            from system.market_data_hub import get_market_data_hub

            hub = get_market_data_hub()
            quote = hub.get_snapshot(epic)
            entry_mid = (
                (float(quote.bid) + float(quote.offer)) / 2.0
                if quote is not None and quote.bid > 0 and quote.offer > 0
                else 0.0
            )

            def _place_and_confirm(order_size: float) -> tuple[dict[str, Any], dict[str, Any], str]:
                res = self._rest.place_market_order(
                    epic=broker_epic,
                    direction=direction,
                    size=order_size,
                    stop_distance=sl_pts,
                    limit_distance=tp_pts,
                )
                ref_local = str(
                    res.get("dealReference") or res.get("dealId") or deal_ref
                ).strip()
                conf: dict[str, Any] = {}
                if ref_local and hasattr(self._rest, "confirm_deal"):
                    conf = self._rest.confirm_deal(ref_local) or {}
                return res, conf, ref_local

            result, confirm, ref = _place_and_confirm(size)
            deal_id = str(
                confirm.get("deal_id") or result.get("dealId") or ""
            ).strip() or None
            if confirm.get("rejected"):
                reject_reason = str(
                    confirm.get("reason")
                    or (confirm.get("raw") or {}).get("reason")
                    or ""
                ).strip()
                from execution.ig_size_validator import classify_size_rejection

                retried = False
                if classify_size_rejection(reject_reason) and float(check.get("ig_min_deal") or 0) > 0:
                    retry_size = max(size, float(check["ig_min_deal"]))
                    if retry_size > size:
                        log_engine(
                            f"DualCoreCoordinator: size self-correct epic={epic} "
                            f"{size}->{retry_size}"
                        )
                        size = retry_size
                        retried = True
                        result, confirm, ref = _place_and_confirm(size)
                        deal_id = str(
                            confirm.get("deal_id") or result.get("dealId") or ""
                        ).strip() or None
                if confirm.get("rejected"):
                    status = "REJECTED"
                    try:
                        from runtime.broker_reject_guard import record_rejection

                        trip = record_rejection(
                            epic=epic,
                            reason=reject_reason,
                            self_correction_attempted=retried,
                            broker_epic=broker_epic,
                        )
                        if trip.get("tripped"):
                            log_engine(
                                f"DualCoreCoordinator: broker reject latch "
                                f"reason={trip.get('reason')} epic={epic} "
                                f"broker_epic={broker_epic}"
                            )
                    except Exception:
                        pass
                    try:
                        transition(
                            deal_ref,
                            LifecycleState.REJECTED,
                            message=reject_reason,
                        )
                    except Exception:
                        pass
                else:
                    status = "CONFIRMED"
                    try:
                        from runtime.broker_reject_guard import record_broker_confirm_success

                        record_broker_confirm_success()
                    except Exception:
                        pass
            elif confirm.get("accepted"):
                status = "CONFIRMED"
                try:
                    from runtime.broker_reject_guard import record_broker_confirm_success

                    record_broker_confirm_success()
                except Exception:
                    pass
            else:
                status = "ACCEPTED"
            log_engine(
                f"DualCoreCoordinator: micro order confirm epic={epic} ref={ref} "
                f"dealId={deal_id or 'pending'} status={status}"
            )
            _persist_micro_production_order(
                deal_reference=ref or deal_ref,
                deal_id=deal_id,
                epic=epic,
                direction=direction,
                size=size,
                status=status,
                broker_payload={"place": result, "confirm": confirm},
            )
            from runtime.virtual_stop_loss import register_virtual_stop

            if entry_mid > 0 and status not in ("REJECTED",):
                lid = deal_ref
                try:
                    transition(
                        lid,
                        LifecycleState.ORDER_ACCEPTED,
                        message=f"IG {status}",
                        extra={"entry_level": entry_mid, "deal_id": deal_id or ref},
                    )
                    transition(
                        lid,
                        LifecycleState.TRAILING_STOP_ACTIVE,
                        message="Virtual stop armed",
                        extra={"entry_level": entry_mid},
                    )
                    transition(lid, LifecycleState.ACTIVE, message="Position tracking")
                    from runtime.dynamic_limit_engine import register_dynamic_limit

                    register_dynamic_limit(
                        deal_id=str(deal_id or ref or lid),
                        epic=epic,
                        direction=direction,
                        entry_level=entry_mid,
                        limit_pts=float(tp_pts),
                    )
                except Exception:
                    pass
                from execution.micro_risk_profile import resolve_micro_tp_sl_for_epic

                _, _, risk_profile = resolve_micro_tp_sl_for_epic(
                    epic, size, self._cfg, volatility_z=None
                )
                register_virtual_stop(
                    epic=epic,
                    direction=direction,
                    entry_level=entry_mid,
                    size=size,
                    deal_id=str(deal_id or ref or ""),
                    ceiling_pts=risk_profile.virtual_stop_ceiling_pts,
                )
        except Exception as exc:
            set_last_gate_suppression_reason(f"micro_order_failed:{type(exc).__name__}")
            try:
                from execution.broker_error_log import append_broker_rejection
                from execution.broker_wire_handshake import append_broker_wire_handshake

                append_broker_rejection(
                    source="DualCoreCoordinator._dispatch_micro_order",
                    epic=epic,
                    direction=direction,
                    exception_type=type(exc).__name__,
                    message=str(exc),
                    response_body=getattr(exc, "body", None) or str(exc),
                )
                append_broker_wire_handshake(
                    source="DualCoreCoordinator._dispatch_micro_order",
                    phase="place_exception",
                    epic=epic,
                    direction=direction,
                    response_text=str(getattr(exc, "body", None) or exc),
                    ok=False,
                    message=str(exc),
                )
            except Exception:
                pass
            log_engine(
                f"DualCoreCoordinator: micro order failed epic={epic}: "
                f"{type(exc).__name__}: {exc}"
            )
            return
        set_last_gate_suppression_reason("")


_coordinator: DualCoreCoordinator | None = None
_coordinator_lock = threading.Lock()
_coordinator_missing_logged_at: float = 0.0


def _ensure_coordinator_for_dispatch(cfg: Any | None = None) -> DualCoreCoordinator | None:
    """Lazy attach when post-ready skipped coordinator startup."""
    global _coordinator_missing_logged_at
    with _coordinator_lock:
        if _coordinator is not None:
            return _coordinator
    try:
        from system.config_loader import ConfigLoader
        from system.credentials_loader import try_load_credentials
        from system.ig_rest_session import get_shared_rest_client

        resolved_cfg = cfg
        if resolved_cfg is None:
            resolved_cfg = ConfigLoader().load()
        cred = try_load_credentials()
        if not cred.ok or cred.credentials is None:
            now = time.time()
            if now - _coordinator_missing_logged_at >= 60.0:
                _coordinator_missing_logged_at = now
                log_engine(
                    "DualCoreCoordinator: lazy attach skipped — credentials unavailable"
                )
            return None
        rest = get_shared_rest_client(cred.credentials)
        coord = start_dual_core_coordinator(rest, config=resolved_cfg)
        if coord is not None:
            try:
                from runtime.session_trade_unlimited import inject_session_unlimited_trades

                inject_session_unlimited_trades()
            except Exception:
                pass
            log_engine("DualCoreCoordinator: lazy attach succeeded on piercing dispatch")
        return coord
    except Exception as exc:
        now = time.time()
        if now - _coordinator_missing_logged_at >= 60.0:
            _coordinator_missing_logged_at = now
            log_engine(
                f"DualCoreCoordinator: lazy attach failed: {type(exc).__name__}: {exc}"
            )
        return None


def dispatch_piercing_zone_order(
    epic: str,
    direction: str,
    *,
    z_score: float = 0.0,
    cfg: Any | None = None,
) -> None:
    """Master valve dispatch from async piercing-zone sweep."""
    with _coordinator_lock:
        coord = _coordinator
    if coord is None:
        coord = _ensure_coordinator_for_dispatch(cfg)
    if coord is None:
        try:
            from runtime.dual_core_execution import set_last_gate_suppression_reason

            set_last_gate_suppression_reason("dual_core_coordinator_missing")
        except Exception:
            pass
        return
    _ = z_score
    if coord._order_cadence_sec > 0:
        now = time.time()
        last = coord._last_order_at.get(epic, 0.0)
        if now - last < coord._order_cadence_sec:
            try:
                from runtime.dual_core_execution import set_last_gate_suppression_reason

                set_last_gate_suppression_reason("order_cadence_throttle")
            except Exception:
                pass
            return
        coord._last_order_at[epic] = now
    log_engine(
        f"DualCoreCoordinator: piercing dispatch queued epic={epic} dir={direction} z={z_score:.4f}"
    )
    try:
        coord._executor.submit(coord._dispatch_micro_order, epic, direction)
    except RuntimeError:
        from concurrent.futures import ThreadPoolExecutor

        coord._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="micro-scalper")
        coord._executor.submit(coord._dispatch_micro_order, epic, direction)


def get_dual_core_coordinator() -> DualCoreCoordinator | None:
    with _coordinator_lock:
        return _coordinator


def start_dual_core_coordinator(
    rest_client: Any,
    *,
    config: Any | None = None,
) -> DualCoreCoordinator | None:
    """Attach parallel micro-scalper track (idempotent)."""
    global _coordinator
    if rest_client is None:
        return None
    with _coordinator_lock:
        if _coordinator is not None:
            return _coordinator
        _coordinator = DualCoreCoordinator(rest_client=rest_client, config=config)
        _coordinator.start()
        return _coordinator


def stop_dual_core_coordinator() -> None:
    global _coordinator
    with _coordinator_lock:
        if _coordinator is not None:
            _coordinator.stop()
            _coordinator = None

