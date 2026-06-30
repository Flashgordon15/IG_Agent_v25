"""
Trade pipeline health model — observability only (no trading decisions).

Aggregates per-epic pipeline stage state from existing telemetry sources:
lifecycle bus, triage SQLite, market hub quotes, gate activity.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

_PIPELINE_LOOKBACK_HOURS = 24


class PipelineState(str, Enum):
    IDLE = "IDLE"
    SIGNAL_ONLY = "SIGNAL_ONLY"
    ORDER_PENDING = "ORDER_PENDING"
    LIVE = "LIVE"
    IN_PROFIT = "IN_PROFIT"
    IN_LOSS = "IN_LOSS"
    CLOSED = "CLOSED"
    RECONCILED = "RECONCILED"


@dataclass
class MlAppetite:
    appetite: str = "NONE"
    probability: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "appetite": self.appetite,
            "probability": round(float(self.probability), 4),
            "reason": self.reason,
        }


@dataclass
class OrderSize:
    stake: float = 0.0
    stop: float | None = None
    limit: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stake": round(float(self.stake), 4),
            "stop": self.stop,
            "limit": self.limit,
        }


@dataclass
class TrailingGuards:
    active: bool = False
    last_update_timestamp: str | None = None
    last_stop: float | None = None
    last_limit: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "last_update_timestamp": self.last_update_timestamp,
            "last_stop": self.last_stop,
            "last_limit": self.last_limit,
        }


@dataclass
class EpicPipelineHealth:
    epic: str
    market_name: str = ""
    signal_ingested: bool = False
    signal_timestamp: str | None = None
    ml_appetite: MlAppetite = field(default_factory=MlAppetite)
    order_prepared: bool = False
    order_size: OrderSize = field(default_factory=OrderSize)
    order_prepared_timestamp: str | None = None
    order_dispatched: bool = False
    broker_epic: str | None = None
    order_dispatched_timestamp: str | None = None
    order_confirmed: bool = False
    ig_order_id: str | None = None
    fill_price: float | None = None
    order_confirmed_timestamp: str | None = None
    live_tracking: bool = False
    last_price: float | None = None
    unrealised_pnl: float | None = None
    live_tracking_timestamp: str | None = None
    trailing_guards: TrailingGuards = field(default_factory=TrailingGuards)
    closed: bool = False
    close_reason: str | None = None
    closed_timestamp: str | None = None
    reconciled: bool = False
    ledger_entry_id: str | None = None
    reconciled_timestamp: str | None = None
    active_strategy_profile: str = "UNKNOWN"
    strategy_source: str = "NONE"

    def pipeline_state(self) -> PipelineState:
        return derive_pipeline_state(self)

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "epic": self.epic,
            "market_name": self.market_name,
            "pipeline_state": self.pipeline_state().value,
            "signal_ingested": self.signal_ingested,
            "signal_timestamp": self.signal_timestamp,
            "ml_appetite": self.ml_appetite.to_dict(),
            "order_prepared": self.order_prepared,
            "order_size": self.order_size.to_dict(),
            "order_prepared_timestamp": self.order_prepared_timestamp,
            "order_dispatched": self.order_dispatched,
            "broker_epic": self.broker_epic,
            "order_dispatched_timestamp": self.order_dispatched_timestamp,
            "order_confirmed": self.order_confirmed,
            "ig_order_id": self.ig_order_id,
            "fill_price": self.fill_price,
            "order_confirmed_timestamp": self.order_confirmed_timestamp,
            "live_tracking": self.live_tracking,
            "last_price": self.last_price,
            "unrealised_pnl": self.unrealised_pnl,
            "live_tracking_timestamp": self.live_tracking_timestamp,
            "trailing_guards": self.trailing_guards.to_dict(),
            "closed": self.closed,
            "close_reason": self.close_reason,
            "closed_timestamp": self.closed_timestamp,
            "reconciled": self.reconciled,
            "ledger_entry_id": self.ledger_entry_id,
            "reconciled_timestamp": self.reconciled_timestamp,
            "active_strategy_profile": self.active_strategy_profile,
            "strategy_source": self.strategy_source,
        }


def derive_pipeline_state(record: EpicPipelineHealth) -> PipelineState:
    if record.reconciled:
        return PipelineState.RECONCILED
    if record.closed:
        return PipelineState.CLOSED
    if record.live_tracking:
        pnl = record.unrealised_pnl
        if pnl is not None:
            if pnl > 0:
                return PipelineState.IN_PROFIT
            if pnl < 0:
                return PipelineState.IN_LOSS
        return PipelineState.LIVE
    if record.order_dispatched and not record.order_confirmed:
        return PipelineState.ORDER_PENDING
    if record.signal_ingested and not record.order_dispatched:
        return PipelineState.SIGNAL_ONLY
    return PipelineState.IDLE


def _triage_db_path() -> Path:
    raw = os.environ.get("IG_TRIAGE_DB", "").strip()
    if raw:
        return Path(raw).resolve()
    return Path(__file__).resolve().parents[1] / "analytics" / "triage_v31.db"


def _market_name(epic: str) -> str:
    try:
        from runtime.dual_core_execution import epic_display_name

        return epic_display_name(epic)
    except Exception:
        pass
    try:
        from trading.open_position_view import epic_market_label

        return epic_market_label(epic)
    except Exception:
        return epic


def _parse_iso_age_sec(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        normalized = str(ts).replace("Z", "+00:00")
        if "T" not in normalized and " " in normalized:
            normalized = normalized.replace(" ", "T")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except (TypeError, ValueError):
        return None


def _within_lookback(ts: str | None, *, lookback_sec: float) -> bool:
    age = _parse_iso_age_sec(ts)
    if age is None:
        return True
    return age <= lookback_sec


def _ml_appetite_from_probability(prob: float | None, verdict: str | None) -> MlAppetite:
    if prob is None:
        return MlAppetite(appetite="NONE", probability=0.0, reason="no_ml_score")
    p = float(prob)
    verdict_s = str(verdict or "").upper()
    if verdict_s in ("REJECT", "BLOCK", "SKIP") or p <= 0.0:
        return MlAppetite(appetite="NONE", probability=p, reason=verdict_s or "below_threshold")
    if p >= 0.62:
        return MlAppetite(appetite="STRONG", probability=p, reason=verdict_s or "strong_conviction")
    if p >= 0.45:
        return MlAppetite(appetite="WEAK", probability=p, reason=verdict_s or "marginal_conviction")
    return MlAppetite(appetite="NONE", probability=p, reason=verdict_s or "weak_score")


def _merge_lifecycle_stages(record: EpicPipelineHealth, lifecycle: dict[str, Any]) -> None:
    stages = lifecycle.get("stages") or {}
    signal = stages.get("signal") or {}
    if signal.get("status") == "ok":
        record.signal_ingested = True
        record.signal_timestamp = signal.get("timestamp") or lifecycle.get("started_at")

    risk = stages.get("risk") or {}
    exec_req = stages.get("execution_request") or {}
    if risk.get("status") == "ok" or exec_req.get("status") == "ok":
        record.order_prepared = True
        record.order_prepared_timestamp = exec_req.get("timestamp") or risk.get("timestamp")
        extra = exec_req.get("extra") or risk.get("extra") or {}
        record.order_size = OrderSize(
            stake=float(extra.get("size") or extra.get("stake") or record.order_size.stake or 0),
            stop=_float_or_none(extra.get("stop") or extra.get("stop_level")),
            limit=_float_or_none(extra.get("limit") or extra.get("limit_level")),
        )

    ig_resp = stages.get("ig_response") or {}
    if ig_resp.get("status") in ("ok", "fail", "pending"):
        record.order_dispatched = ig_resp.get("status") in ("ok", "pending", "fail")
        record.order_dispatched_timestamp = ig_resp.get("timestamp")
        extra = ig_resp.get("extra") or {}
        record.broker_epic = str(extra.get("broker_epic") or extra.get("epic") or record.epic)

    opened = stages.get("position_opened") or {}
    if opened.get("status") == "ok":
        record.order_confirmed = True
        record.order_confirmed_timestamp = opened.get("timestamp")
        extra = opened.get("extra") or {}
        record.ig_order_id = str(lifecycle.get("deal_id") or extra.get("deal_id") or "") or None
        record.fill_price = _float_or_none(extra.get("fill_price") or extra.get("level"))

    tracking = stages.get("position_tracking") or {}
    if tracking.get("status") == "ok":
        record.live_tracking = True
        record.live_tracking_timestamp = tracking.get("timestamp")
        extra = tracking.get("extra") or {}
        record.last_price = _float_or_none(extra.get("last_price") or extra.get("level"))
        record.unrealised_pnl = _float_or_none(extra.get("unrealised_pnl") or extra.get("upl"))

    closed = stages.get("position_closed") or {}
    if closed.get("status") == "ok":
        record.closed = True
        record.closed_timestamp = closed.get("timestamp") or lifecycle.get("closed_at")
        extra = closed.get("extra") or {}
        record.close_reason = _normalise_close_reason(
            str(extra.get("result") or extra.get("close_reason") or lifecycle.get("close_source") or "")
        )
        if lifecycle.get("final_state") == "SUCCESS" and record.ig_order_id:
            record.reconciled = True
            record.ledger_entry_id = record.ig_order_id
            record.reconciled_timestamp = record.closed_timestamp


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalise_close_reason(raw: str) -> str | None:
    key = str(raw or "").upper()
    if not key:
        return None
    for token in ("STOP", "LIMIT", "ML_DECISION", "MANUAL"):
        if token in key:
            return token
    if "TRAIL" in key:
        return "STOP"
    return key[:32]


def _apply_active_lifecycle_row(record: EpicPipelineHealth, row: dict[str, Any]) -> None:
    state = str(row.get("lifecycle_state") or "").upper()
    if state in ("CLOSED", "CLOSED_ON_BROKER_ANOMALY"):
        record.closed = True
        record.closed_timestamp = row.get("last_broker_sync_at")
        record.close_reason = "MANUAL" if "ANOMALY" in state else "STOP"
        return
    record.order_confirmed = True
    record.live_tracking = True
    record.ig_order_id = str(row.get("deal_id") or "") or record.ig_order_id
    record.fill_price = _float_or_none(row.get("broker_level"))
    record.last_price = record.fill_price
    record.unrealised_pnl = _float_or_none(row.get("broker_upl"))
    record.live_tracking_timestamp = row.get("last_broker_sync_at")
    stop = _float_or_none(row.get("broker_stop"))
    limit = _float_or_none(row.get("broker_limit"))
    if stop is not None or limit is not None:
        record.trailing_guards = TrailingGuards(
            active=True,
            last_update_timestamp=row.get("last_broker_sync_at"),
            last_stop=stop,
            last_limit=limit,
        )
    if not record.order_size.stake:
        record.order_size = OrderSize(stake=float(row.get("size") or 0))


def _apply_production_order_row(record: EpicPipelineHealth, row: dict[str, Any]) -> None:
    status = str(row.get("status") or "").upper()
    record.order_dispatched = status in (
        "ACCEPTED",
        "CONFIRMED",
        "PENDING",
        "DISPATCHED",
        "OPEN",
    )
    record.order_dispatched_timestamp = row.get("created_at")
    record.broker_epic = str(row.get("epic") or record.epic)
    if not record.order_size.stake:
        record.order_size = OrderSize(stake=float(row.get("size") or 0))
    deal_id = str(row.get("deal_id") or row.get("deal_reference") or "")
    if deal_id:
        record.ig_order_id = deal_id
    if status == "CONFIRMED":
        record.order_confirmed = True
        record.order_confirmed_timestamp = row.get("created_at")
        record.live_tracking = True
    if status in ("CLOSED", "FAILED", "REJECTED", "TIMEOUT", "CLOSED_ON_BROKER_ANOMALY"):
        record.closed = True
        record.closed_timestamp = row.get("created_at")
        record.close_reason = "MANUAL" if status == "FAILED" else "STOP"
    payload = row.get("broker_payload")
    if isinstance(payload, dict):
        record.fill_price = _float_or_none(payload.get("level") or payload.get("fill_price"))


def _apply_closed_position_row(record: EpicPipelineHealth, row: dict[str, Any]) -> None:
    record.closed = True
    record.closed_timestamp = row.get("exit_timestamp")
    record.close_reason = _normalise_close_reason(str(row.get("result") or ""))
    record.reconciled = True
    record.ledger_entry_id = str(row.get("ticket") or "") or record.ledger_entry_id
    record.reconciled_timestamp = row.get("exit_timestamp")


def _apply_ml_row(record: EpicPipelineHealth, row: dict[str, Any]) -> None:
    record.ml_appetite = _ml_appetite_from_probability(
        _float_or_none(row.get("win_probability")),
        str(row.get("model_verdict") or ""),
    )


def _apply_hub_quote(record: EpicPipelineHealth, epic: str) -> None:
    try:
        from system.market_data_hub import get_market_data_hub

        snap = get_market_data_hub().get_snapshot(epic)
        if snap is None or snap.bid <= 0:
            return
        mid = (float(snap.bid) + float(snap.offer)) / 2.0
        record.last_price = round(mid, 5)
        if record.live_tracking and record.live_tracking_timestamp is None:
            record.live_tracking_timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        pass


def _fetch_lifecycle_by_epic() -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    try:
        from system.trade_lifecycle_bus import get_lifecycle_bus

        snap = get_lifecycle_bus().snapshot()
        rows: list[dict[str, Any]] = []
        if snap.get("current"):
            rows.append(snap["current"])
        rows.extend(snap.get("history") or [])
        for row in rows:
            epic = str(row.get("epic") or "").strip()
            if epic:
                out.setdefault(epic, []).append(row)
    except Exception:
        pass
    return out


def _fetch_db_rows(*, lookback_sec: float) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
    dict[str, dict[str, Any]],
]:
    active: dict[str, list[dict[str, Any]]] = {}
    orders: dict[str, list[dict[str, Any]]] = {}
    closed: dict[str, list[dict[str, Any]]] = {}
    ml_latest: dict[str, dict[str, Any]] = {}
    path = _triage_db_path()
    if not path.is_file():
        return active, orders, closed, ml_latest
    try:
        from analytics.triage_db import connect_triage_sqlite_readonly

        conn = connect_triage_sqlite_readonly(path)
    except Exception:
        return active, orders, closed, ml_latest
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=lookback_sec)).isoformat(timespec="seconds")
    try:
        cur = conn.execute(
            """
            SELECT deal_id, epic, direction, size, lifecycle_state, broker_level,
                   broker_stop, broker_limit, broker_upl, last_broker_sync_at
            FROM active_lifecycle_trades
            WHERE lifecycle_state NOT IN ('CLOSED', 'CLOSED_ON_BROKER_ANOMALY')
            """
        )
        for row in cur.fetchall():
            epic = str(row[1] or "")
            if not epic:
                continue
            active.setdefault(epic, []).append(
                {
                    "deal_id": row[0],
                    "epic": epic,
                    "direction": row[2],
                    "size": row[3],
                    "lifecycle_state": row[4],
                    "broker_level": row[5],
                    "broker_stop": row[6],
                    "broker_limit": row[7],
                    "broker_upl": row[8],
                    "last_broker_sync_at": row[9],
                }
            )
    except Exception:
        pass
    try:
        cur = conn.execute(
            """
            SELECT deal_reference, deal_id, epic, direction, size, status, created_at, broker_payload
            FROM production_orders
            WHERE datetime(created_at) >= datetime(?)
            ORDER BY datetime(created_at) DESC
            """,
            (cutoff,),
        )
        for ref, deal_id, epic, direction, size, status, created_at, payload_raw in cur.fetchall():
            epic_s = str(epic or "")
            if not epic_s:
                continue
            payload_obj: Any = None
            if payload_raw:
                try:
                    payload_obj = json.loads(str(payload_raw))
                except Exception:
                    payload_obj = None
            orders.setdefault(epic_s, []).append(
                {
                    "deal_reference": ref,
                    "deal_id": deal_id,
                    "epic": epic_s,
                    "direction": direction,
                    "size": size,
                    "status": status,
                    "created_at": created_at,
                    "broker_payload": payload_obj,
                }
            )
    except Exception:
        pass
    try:
        cur = conn.execute(
            """
            SELECT ticket, epic, result, exit_timestamp
            FROM closed_positions
            WHERE datetime(exit_timestamp) >= datetime(?)
            ORDER BY datetime(exit_timestamp) DESC
            """,
            (cutoff,),
        )
        for ticket, epic, result, exit_ts in cur.fetchall():
            epic_s = str(epic or "")
            if not epic_s:
                continue
            closed.setdefault(epic_s, []).append(
                {"ticket": ticket, "epic": epic_s, "result": result, "exit_timestamp": exit_ts}
            )
    except Exception:
        pass
    try:
        cur = conn.execute(
            """
            SELECT epic, win_probability, model_verdict, timestamp
            FROM ml_feature_executions
            ORDER BY timestamp DESC
            LIMIT 200
            """
        )
        for epic, prob, verdict, ts in cur.fetchall():
            epic_s = str(epic or "")
            if epic_s and epic_s not in ml_latest:
                ml_latest[epic_s] = {
                    "win_probability": prob,
                    "model_verdict": verdict,
                    "timestamp": ts,
                }
    except Exception:
        pass
    finally:
        conn.close()
    return active, orders, closed, ml_latest


def _collect_epics(*, lookback_sec: float) -> set[str]:
    epics: set[str] = set()
    now = time.time()
    try:
        from system.gate_activity import last_gate_check_by_epic

        for epic, ts in last_gate_check_by_epic().items():
            if now - float(ts) <= lookback_sec:
                epics.add(epic)
    except Exception:
        pass
    try:
        from system.market_data_hub import NIGHT_MATRIX_EPICS

        epics.update(NIGHT_MATRIX_EPICS)
    except Exception:
        pass
    active, orders, closed, ml_latest = _fetch_db_rows(lookback_sec=lookback_sec)
    epics.update(active.keys())
    epics.update(orders.keys())
    epics.update(closed.keys())
    epics.update(ml_latest.keys())
    epics.update(_fetch_lifecycle_by_epic().keys())
    return {e for e in epics if e}


def build_trade_pipeline_health(*, lookback_hours: float = _PIPELINE_LOOKBACK_HOURS) -> list[dict[str, Any]]:
    lookback_sec = float(lookback_hours) * 3600.0
    epics = _collect_epics(lookback_sec=lookback_sec)
    lifecycle_by_epic = _fetch_lifecycle_by_epic()
    active, orders, closed, ml_latest = _fetch_db_rows(lookback_sec=lookback_sec)

    results: list[dict[str, Any]] = []
    for epic in sorted(epics):
        record = EpicPipelineHealth(epic=epic, market_name=_market_name(epic))
        for lifecycle in lifecycle_by_epic.get(epic, []):
            _merge_lifecycle_stages(record, lifecycle)
        for row in active.get(epic, []):
            _apply_active_lifecycle_row(record, row)
        for row in orders.get(epic, []):
            _apply_production_order_row(record, row)
        for row in closed.get(epic, []):
            _apply_closed_position_row(record, row)
        if epic in ml_latest:
            _apply_ml_row(record, ml_latest[epic])
        _apply_hub_quote(record, epic)

        from runtime.strategy_profile import build_derivation_hints, derive_strategy_ownership

        hints = build_derivation_hints(
            epic=epic,
            record=record,
            lifecycle_rows=lifecycle_by_epic.get(epic, []),
            order_rows=orders.get(epic, []),
        )
        profile, source = derive_strategy_ownership(record, hints)
        record.active_strategy_profile = profile.value
        record.strategy_source = source.value

        recent = (
            record.signal_timestamp
            or record.order_dispatched_timestamp
            or record.live_tracking_timestamp
            or record.closed_timestamp
        )
        if not (
            record.live_tracking
            or record.order_dispatched
            or record.signal_ingested
            or record.closed
            or _within_lookback(recent, lookback_sec=lookback_sec)
        ):
            continue
        results.append(record.to_summary_dict())
    return results


def build_api_feed_health() -> dict[str, Any]:
    """Feed health with latency, timestamps, and freshness ranking."""
    ok = "OK"
    degraded = "DEGRADED"
    now = time.time()
    feeds: dict[str, dict[str, Any]] = {
        "feed1": {"status": degraded, "latency_ms": None, "last_update_timestamp": None, "label": "hub_quotes"},
        "feed2": {"status": degraded, "latency_ms": None, "last_update_timestamp": None, "label": "rest_stream"},
        "feed3": {"status": degraded, "latency_ms": None, "last_update_timestamp": None, "label": "mock_or_secondary"},
    }

    try:
        from api.agent_health import get_cached_health_status

        health = get_cached_health_status()
        if health.get("quotes_fresh") or int(health.get("quotes_fresh_count") or 0) > 0:
            feeds["feed1"]["status"] = ok
            feeds["feed1"]["latency_ms"] = 0
    except Exception:
        pass

    try:
        from system.market_data_hub import NIGHT_MATRIX_EPICS, get_market_data_hub

        hub = get_market_data_hub()
        best_age: float | None = None
        best_ts: float | None = None
        for epic in NIGHT_MATRIX_EPICS:
            snap = hub.get_snapshot(epic)
            if snap is None:
                continue
            age = snap.age_seconds()
            ts = float(snap.updated_at or now)
            if best_age is None or age < best_age:
                best_age = age
                best_ts = ts
        if best_age is not None:
            feeds["feed1"]["latency_ms"] = round(best_age * 1000.0, 1)
            feeds["feed1"]["last_update_timestamp"] = datetime.fromtimestamp(
                best_ts or now, tz=timezone.utc
            ).isoformat(timespec="seconds")
            if best_age <= 45.0:
                feeds["feed1"]["status"] = ok
    except Exception:
        pass

    try:
        from system.rest_api_budget import hub_quote_stream_fresh

        if hub_quote_stream_fresh():
            feeds["feed2"]["status"] = ok
            feeds["feed2"]["latency_ms"] = feeds["feed2"]["latency_ms"] or 50.0
            feeds["feed2"]["last_update_timestamp"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
    except Exception:
        pass

    if os.environ.get("IG_MOCK_FEED", "").strip().lower() in ("1", "true", "yes"):
        feeds["feed3"]["status"] = ok
        feeds["feed3"]["latency_ms"] = 1.0
        feeds["feed3"]["last_update_timestamp"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    elif feeds["feed1"]["status"] == ok:
        feeds["feed3"]["status"] = ok
        feeds["feed3"]["latency_ms"] = feeds["feed1"].get("latency_ms")
        feeds["feed3"]["last_update_timestamp"] = feeds["feed1"].get("last_update_timestamp")

    ranked = sorted(
        feeds.items(),
        key=lambda item: (
            0 if item[1]["status"] == ok else 1,
            item[1]["latency_ms"] if item[1]["latency_ms"] is not None else 1e9,
        ),
    )
    ranking = {
        "primary_feed": ranked[0][0] if ranked else "feed1",
        "secondary_feed": ranked[1][0] if len(ranked) > 1 else None,
        "tertiary_feed": ranked[2][0] if len(ranked) > 2 else None,
    }
    return {"feeds": feeds, "ranking": ranking}


def build_market_rotation_status() -> dict[str, Any]:
    """Rotation health from orchestrator + dual-core stack (telemetry only)."""
    active: list[str] = []
    candidates: list[str] = []
    rotation_state = "IDLE"
    last_ts: str | None = None
    try:
        from runtime.dual_core_execution import ROTATION_UNIVERSE, get_active_stack_epics

        active = list(get_active_stack_epics())
        active_set = set(active)
        candidates = [e for e in ROTATION_UNIVERSE if e not in active_set]
    except Exception:
        pass
    try:
        from runtime.market_orchestrator import MarketOrchestrator

        orch_active = MarketOrchestrator.get_global_active_epics()
        if orch_active:
            active = list(dict.fromkeys([*active, *orch_active]))
            rotation_state = "ACTIVE"
    except Exception:
        pass
    try:
        from runtime.market_orchestrator import MarketOrchestrator

        ts = getattr(MarketOrchestrator, "_last_rotation_mono", None)
        if ts is not None:
            import time
            from datetime import datetime, timezone

            last_ts = datetime.fromtimestamp(
                time.time() - max(0.0, time.monotonic() - float(ts)),
                tz=timezone.utc,
            ).isoformat(timespec="seconds")
    except Exception:
        pass
    status = "ok" if active else "warming"
    detail = (
        f"{len(active)} active / {len(candidates)} candidates"
        if active
        else "awaiting orchestrator rank"
    )
    return {
        "active_markets": active,
        "candidate_markets": candidates,
        "rotation_state": rotation_state,
        "last_rotation_timestamp": last_ts,
        "status": status,
        "detail": detail,
    }


# ── Test helpers (observation merge without touching trading code) ───────────

_TEST_OBSERVATIONS: dict[str, dict[str, Any]] = {}


def reset_pipeline_health_for_tests() -> None:
    _TEST_OBSERVATIONS.clear()


def observe_pipeline_for_test(epic: str, **fields: Any) -> EpicPipelineHealth:
    """Merge staged observations for unit tests simulating hook progression."""
    base = dict(_TEST_OBSERVATIONS.get(epic) or {})
    base.update(fields)
    _TEST_OBSERVATIONS[epic] = base
    record = EpicPipelineHealth(epic=epic, market_name=_market_name(epic))
    if base.get("signal_ingested"):
        record.signal_ingested = True
        record.signal_timestamp = base.get("signal_timestamp")
    if "ml_appetite" in base:
        ma = base["ml_appetite"]
        if isinstance(ma, MlAppetite):
            record.ml_appetite = ma
        elif isinstance(ma, dict):
            record.ml_appetite = MlAppetite(**ma)
    for key in (
        "order_prepared",
        "order_prepared_timestamp",
        "order_dispatched",
        "order_dispatched_timestamp",
        "order_confirmed",
        "order_confirmed_timestamp",
        "live_tracking",
        "live_tracking_timestamp",
        "closed",
        "closed_timestamp",
        "reconciled",
        "reconciled_timestamp",
    ):
        if key in base:
            setattr(record, key, base[key])
    if "order_size" in base:
        osz = base["order_size"]
        record.order_size = osz if isinstance(osz, OrderSize) else OrderSize(**osz)
    if "trailing_guards" in base:
        tg = base["trailing_guards"]
        record.trailing_guards = tg if isinstance(tg, TrailingGuards) else TrailingGuards(**tg)
    for key in (
        "broker_epic",
        "ig_order_id",
        "fill_price",
        "last_price",
        "unrealised_pnl",
        "close_reason",
        "ledger_entry_id",
    ):
        if key in base:
            setattr(record, key, base[key])
    return record
