"""
Closed-loop broker ledger reconciliation for trailing / profit-taking paths.

Every trailing PUT is preceded by a synchronous GET /positions/otc ledger pull.
Local cache or SQLite rows alone are never sufficient to authorize stop modification.
"""

from __future__ import annotations

import os
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
