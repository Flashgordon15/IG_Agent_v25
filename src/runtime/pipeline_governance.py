"""
Trade pipeline governance — rule-based anomaly detection on observability data.

Read-only diagnostics; does not modify trading logic or execution behaviour.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from runtime.strategy_profile import (
    StrategyProfile,
    effective_trailing_guards_active,
    is_path_a_profile,
    is_rotation_profile,
    is_scalp_profile,
    strategy_profile_from_row,
)

# Rule thresholds (seconds / milliseconds)
ORDER_PENDING_MAX_SEC = 120.0
RECONCILE_AFTER_CLOSE_MAX_SEC = 300.0
ML_STRONG_TO_ORDER_MAX_SEC = 180.0
FEED_LATENCY_DEGRADED_MS = 45_000.0
FEED_STALE_MAX_SEC = 120.0
ACTIVE_MARKET_SIGNAL_MAX_SEC = 3600.0

_SCORE_DEDUCT = {
    "pipeline": 15,
    "feed": 10,
    "rotation": 8,
    "session": 12,
}


def _parse_ts_age_sec(ts: str | None) -> float | None:
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


def _clamp_score(value: float) -> int:
    return max(0, min(100, int(round(value))))


def _pipeline_anomalies(epic_row: dict[str, Any]) -> list[str]:
    anomalies: list[str] = []
    profile = strategy_profile_from_row(epic_row)

    if is_rotation_profile(epic_row):
        return anomalies

    state = str(epic_row.get("pipeline_state") or "").upper()
    ml = epic_row.get("ml_appetite") or {}
    appetite = str(ml.get("appetite") or "").upper()

    if state == "ORDER_PENDING":
        age = _parse_ts_age_sec(epic_row.get("order_dispatched_timestamp"))
        if age is not None and age > ORDER_PENDING_MAX_SEC:
            if is_scalp_profile(epic_row) or str(epic_row.get("strategy_source") or "") == "MICRO":
                anomalies.append("ORDER_PENDING_TOO_LONG")
            elif is_path_a_profile(epic_row):
                anomalies.append("ORDER_PENDING_TOO_LONG")

    if epic_row.get("closed") and not epic_row.get("reconciled"):
        age = _parse_ts_age_sec(epic_row.get("closed_timestamp"))
        if age is not None and age > RECONCILE_AFTER_CLOSE_MAX_SEC:
            anomalies.append("NO_RECONCILE_AFTER_CLOSE")

    if not is_scalp_profile(epic_row):
        if appetite == "STRONG" and not epic_row.get("order_prepared") and not epic_row.get("order_dispatched"):
            ref_ts = epic_row.get("signal_timestamp")
            age = _parse_ts_age_sec(ref_ts)
            if age is None or age > ML_STRONG_TO_ORDER_MAX_SEC:
                anomalies.append("ML_APPETITE_STRONG_BUT_NO_ORDER")

    if epic_row.get("live_tracking") and not effective_trailing_guards_active(epic_row):
        if profile is not StrategyProfile.ROTATION:
            anomalies.append("LIVE_WITHOUT_TRAILING_GUARDS")

    if state in ("IN_LOSS",) and epic_row.get("live_tracking"):
        pnl = epic_row.get("unrealised_pnl")
        if pnl is not None and float(pnl) < 0:
            anomalies.append("LIVE_POSITION_IN_LOSS")

    return anomalies


def _feed_anomalies_for_epic(
    epic_row: dict[str, Any],
    api_feed_health: dict[str, Any],
) -> list[str]:
    """Attach global feed issues to epics with active pipeline activity."""
    if is_rotation_profile(epic_row) and not epic_row.get("order_dispatched"):
        return []

    state = str(epic_row.get("pipeline_state") or "").upper()
    active = state in (
        "ORDER_PENDING",
        "LIVE",
        "IN_PROFIT",
        "IN_LOSS",
        "SIGNAL_ONLY",
    ) or bool(epic_row.get("live_tracking"))
    if not active:
        return []

    anomalies: list[str] = []
    feeds = api_feed_health.get("feeds") or {}
    ranking = api_feed_health.get("ranking") or {}
    primary = str(ranking.get("primary_feed") or "feed1")
    primary_meta = feeds.get(primary) or {}

    if primary_meta.get("status") == "DEGRADED":
        anomalies.append("PRIMARY_FEED_DEGRADED")

    latency = primary_meta.get("latency_ms")
    if latency is not None and float(latency) > FEED_LATENCY_DEGRADED_MS:
        anomalies.append("PRIMARY_FEED_STALE")

    last_update = primary_meta.get("last_update_timestamp")
    age = _parse_ts_age_sec(last_update)
    if age is not None and age > FEED_STALE_MAX_SEC:
        anomalies.append("PRIMARY_FEED_STALE")

    if feeds and all((meta or {}).get("status") == "DEGRADED" for meta in feeds.values()):
        anomalies.append("ALL_FEEDS_DEGRADED")

    return sorted(set(anomalies))


def _rotation_anomalies_for_epic(
    epic: str,
    epic_row: dict[str, Any],
    rotation_status: dict[str, Any],
) -> list[str]:
    anomalies: list[str] = []
    if is_scalp_profile(epic_row):
        return anomalies

    active_markets = set(rotation_status.get("active_markets") or [])
    rot_state = str(rotation_status.get("rotation_state") or "IDLE").upper()

    if is_rotation_profile(epic_row):
        if epic in active_markets and not epic_row.get("signal_ingested"):
            anomalies.append("ACTIVE_MARKET_WITHOUT_RECENT_SIGNAL")
        elif epic in active_markets:
            age = _parse_ts_age_sec(epic_row.get("signal_timestamp"))
            if age is not None and age > ACTIVE_MARKET_SIGNAL_MAX_SEC:
                anomalies.append("ACTIVE_MARKET_WITHOUT_RECENT_SIGNAL")
        if rot_state == "ROTATING" and epic in active_markets:
            anomalies.append("MARKET_ROTATION_IN_PROGRESS")
        return anomalies

    if epic in active_markets and not epic_row.get("signal_ingested"):
        anomalies.append("ACTIVE_MARKET_WITHOUT_RECENT_SIGNAL")
    elif epic in active_markets:
        age = _parse_ts_age_sec(epic_row.get("signal_timestamp"))
        if age is not None and age > ACTIVE_MARKET_SIGNAL_MAX_SEC:
            anomalies.append("ACTIVE_MARKET_WITHOUT_RECENT_SIGNAL")

    if rot_state == "ROTATING" and epic in active_markets:
        anomalies.append("MARKET_ROTATION_IN_PROGRESS")

    return anomalies


def _epic_health_score(
    pipeline_anomalies: list[str],
    feed_anomalies: list[str],
    rotation_anomalies: list[str],
) -> int:
    score = 100.0
    score -= len(pipeline_anomalies) * _SCORE_DEDUCT["pipeline"]
    score -= len(feed_anomalies) * _SCORE_DEDUCT["feed"]
    score -= len(rotation_anomalies) * _SCORE_DEDUCT["rotation"]
    return _clamp_score(score)


def _session_anomalies(
    per_epic: list[dict[str, Any]],
    api_feed_health: dict[str, Any],
) -> list[str]:
    anomalies: list[str] = []
    if not per_epic:
        anomalies.append("NO_ACTIVE_MARKETS")

    stalled = sum(1 for row in per_epic if "ORDER_PENDING_TOO_LONG" in row.get("pipeline_anomalies", []))
    if stalled >= 2:
        anomalies.append("MULTIPLE_EPICS_STALLED_IN_ORDER_PENDING")
    elif stalled == 1:
        anomalies.append("EPIC_STALLED_IN_ORDER_PENDING")

    feeds = api_feed_health.get("feeds") or {}
    if feeds and all((meta or {}).get("status") == "DEGRADED" for meta in feeds.values()):
        anomalies.append("ALL_FEEDS_DEGRADED")

    reconcile_lags = sum(
        1 for row in per_epic if "NO_RECONCILE_AFTER_CLOSE" in row.get("pipeline_anomalies", [])
    )
    if reconcile_lags > 0:
        anomalies.append("RECONCILIATION_LAG_DETECTED")

    low_scores = sum(1 for row in per_epic if int(row.get("pipeline_health_score") or 100) < 50)
    if low_scores >= 2:
        anomalies.append("MULTIPLE_EPICS_DEGRADED")

    return sorted(set(anomalies))


def _session_health_score(per_epic: list[dict[str, Any]], session_anomalies: list[str]) -> int:
    if not per_epic:
        base = 70.0
    else:
        base = sum(int(row.get("pipeline_health_score") or 100) for row in per_epic) / len(per_epic)
    base -= len(session_anomalies) * _SCORE_DEDUCT["session"]
    return _clamp_score(base)


def _alert_severity(code: str) -> str:
    critical = {
        "ALL_FEEDS_DEGRADED",
        "ORDER_STALL",
        "MULTIPLE_EPICS_STALLED",
        "NO_ACTIVE_MARKETS",
    }
    warn = {
        "RECONCILE_LAG",
        "FEED_DEGRADED",
        "FEED_STALE",
        "TRAILING_MISSING",
        "ML_NO_ORDER",
        "ROTATION_SIGNAL_GAP",
        "SESSION_DEGRADED",
    }
    if code in critical:
        return "CRITICAL"
    if code in warn:
        return "WARN"
    return "INFO"


def _build_gui_alerts(
    per_epic: list[dict[str, Any]],
    session_anomalies: list[str],
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []

    code_map = {
        "ORDER_PENDING_TOO_LONG": ("ORDER_STALL", "Order pending longer than allowed threshold"),
        "NO_RECONCILE_AFTER_CLOSE": ("RECONCILE_LAG", "Closed trade not reconciled within threshold"),
        "ML_APPETITE_STRONG_BUT_NO_ORDER": ("ML_NO_ORDER", "Strong ML appetite but no order prepared"),
        "LIVE_WITHOUT_TRAILING_GUARDS": ("TRAILING_MISSING", "Live position without trailing guards"),
        "PRIMARY_FEED_DEGRADED": ("FEED_DEGRADED", "Primary quote feed is degraded"),
        "PRIMARY_FEED_STALE": ("FEED_STALE", "Primary feed latency or update age exceeds threshold"),
        "ALL_FEEDS_DEGRADED": ("ALL_FEEDS_DEGRADED", "All quote feeds are degraded"),
        "ACTIVE_MARKET_WITHOUT_RECENT_SIGNAL": ("ROTATION_SIGNAL_GAP", "Active market lacks recent signal"),
        "MARKET_ROTATION_IN_PROGRESS": ("ROTATION_ACTIVE", "Market rotation in progress"),
        "LIVE_POSITION_IN_LOSS": ("POSITION_IN_LOSS", "Live position showing unrealised loss"),
    }

    for row in per_epic:
        epic = row.get("epic")
        for bucket in ("pipeline_anomalies", "feed_anomalies", "rotation_anomalies"):
            for anomaly in row.get(bucket) or []:
                code, message = code_map.get(anomaly, (anomaly, anomaly.replace("_", " ").lower()))
                alerts.append(
                    {
                        "severity": _alert_severity(code),
                        "scope": "EPIC",
                        "epic": epic,
                        "code": code,
                        "message": message,
                    }
                )

    session_map = {
        "NO_ACTIVE_MARKETS": ("NO_ACTIVE_MARKETS", "No active markets in pipeline health window"),
        "ALL_FEEDS_DEGRADED": ("ALL_FEEDS_DEGRADED", "All quote feeds degraded for session"),
        "MULTIPLE_EPICS_STALLED_IN_ORDER_PENDING": (
            "MULTIPLE_EPICS_STALLED",
            "Multiple markets stalled in order pending",
        ),
        "EPIC_STALLED_IN_ORDER_PENDING": ("ORDER_STALL", "A market is stalled in order pending"),
        "RECONCILIATION_LAG_DETECTED": ("RECONCILE_LAG", "Reconciliation lag detected across session"),
        "MULTIPLE_EPICS_DEGRADED": ("SESSION_DEGRADED", "Multiple markets show degraded pipeline health"),
    }
    for anomaly in session_anomalies:
        code, message = session_map.get(anomaly, (anomaly, anomaly.replace("_", " ").lower()))
        alerts.append(
            {
                "severity": _alert_severity(code),
                "scope": "SESSION",
                "epic": None,
                "code": code,
                "message": message,
            }
        )

    # De-duplicate identical epic+code alerts
    seen: set[tuple[str | None, str]] = set()
    unique: list[dict[str, Any]] = []
    for alert in alerts:
        key = (alert.get("epic"), str(alert.get("code")))
        if key in seen:
            continue
        seen.add(key)
        unique.append(alert)
    return unique


def build_pipeline_governance(
    *,
    trade_pipeline_health: list[dict[str, Any]] | None = None,
    api_feed_health: dict[str, Any] | None = None,
    market_rotation_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Compute governance scores and anomalies from existing health snapshots.

    When inputs are omitted, reads live telemetry via pipeline_health builders.
    """
    if trade_pipeline_health is None or api_feed_health is None or market_rotation_status is None:
        from runtime.pipeline_health import (
            build_api_feed_health,
            build_market_rotation_status,
            build_trade_pipeline_health,
        )

        if trade_pipeline_health is None:
            trade_pipeline_health = build_trade_pipeline_health()
        if api_feed_health is None:
            api_feed_health = build_api_feed_health()
        if market_rotation_status is None:
            market_rotation_status = build_market_rotation_status()

    per_epic: list[dict[str, Any]] = []
    for epic_row in trade_pipeline_health:
        epic = str(epic_row.get("epic") or "")
        if not epic:
            continue
        pipeline_anomalies = _pipeline_anomalies(epic_row)
        feed_anomalies = _feed_anomalies_for_epic(epic_row, api_feed_health)
        rotation_anomalies = _rotation_anomalies_for_epic(epic, epic_row, market_rotation_status)
        score = _epic_health_score(pipeline_anomalies, feed_anomalies, rotation_anomalies)
        per_epic.append(
            {
                "epic": epic,
                "pipeline_health_score": score,
                "pipeline_anomalies": pipeline_anomalies,
                "feed_anomalies": feed_anomalies,
                "rotation_anomalies": rotation_anomalies,
                "active_strategy_profile": epic_row.get("active_strategy_profile", "UNKNOWN"),
                "strategy_source": epic_row.get("strategy_source", "NONE"),
            }
        )

    session_anomalies = _session_anomalies(per_epic, api_feed_health)
    session_score = _session_health_score(per_epic, session_anomalies)
    gui_alerts = _build_gui_alerts(per_epic, session_anomalies)

    return {
        "pipeline_governance": {"per_epic": per_epic},
        "session_governance": {
            "overall_session_health_score": session_score,
            "session_anomalies": session_anomalies,
        },
        "gui_alerts": gui_alerts,
    }


def evaluate_epic_governance_for_test(
    epic_row: dict[str, Any],
    *,
    api_feed_health: dict[str, Any] | None = None,
    market_rotation_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Test helper — governance for a single epic row."""
    api_feed_health = api_feed_health or {"feeds": {}, "ranking": {}}
    market_rotation_status = market_rotation_status or {
        "active_markets": [],
        "candidate_markets": [],
        "rotation_state": "IDLE",
    }
    return build_pipeline_governance(
        trade_pipeline_health=[epic_row],
        api_feed_health=api_feed_health,
        market_rotation_status=market_rotation_status,
    )["pipeline_governance"]["per_epic"][0]
