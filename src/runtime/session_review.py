"""
Session review engine — advisory session analysis, loosening advisor, self-reflection.

Read-only observability synthesis. Does NOT modify trading, execution, or config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime.strategy_selector import _feed_degraded, _volatility_z

_PROFILES = ("SCALP", "MOMENTUM", "SWING", "ROTATION", "STAND_DOWN")
_PATHS = ("PATH_A", "MICRO", "PATH_B_HANDOFF")

_PROFILE_EXECUTION_PATHS: dict[str, list[str]] = {
    "SCALP": ["MICRO", "PATH_B_HANDOFF"],
    "MOMENTUM": ["PATH_A"],
    "SWING": ["PATH_A"],
    "ROTATION": ["PATH_B_HANDOFF"],
    "STAND_DOWN": [],
}


def _paths_for_profile(profile: str) -> list[str]:
    return list(_PROFILE_EXECUTION_PATHS.get(str(profile or "").upper(), []))


@dataclass
class SessionReview:
    session_summary: dict[str, Any]
    session_quality_score: int
    session_risk_score: int
    session_stability_score: int
    session_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_summary": dict(self.session_summary),
            "session_quality_score": int(self.session_quality_score),
            "session_risk_score": int(self.session_risk_score),
            "session_stability_score": int(self.session_stability_score),
            "session_flags": sorted(set(self.session_flags)),
        }


@dataclass
class LooseningAdvice:
    recommended_changes: list[str]
    confidence: int
    reason: str
    loosening_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "recommended_changes": list(self.recommended_changes),
            "confidence": int(self.confidence),
            "reason": self.reason,
            "loosening_flags": sorted(set(self.loosening_flags)),
        }


@dataclass
class SelfReflection:
    critique_summary: str
    weaknesses: list[str]
    contradictions: list[str]
    missed_opportunities: list[str]
    strategy_misalignments: list[str]
    improvement_suggestions: list[str]
    reflection_flags: list[str] = field(default_factory=list)
    reflection_confidence: int = 50

    def to_dict(self) -> dict[str, Any]:
        return {
            "critique_summary": self.critique_summary,
            "weaknesses": list(self.weaknesses),
            "contradictions": list(self.contradictions),
            "missed_opportunities": list(self.missed_opportunities),
            "strategy_misalignments": list(self.strategy_misalignments),
            "improvement_suggestions": list(self.improvement_suggestions),
            "reflection_flags": sorted(set(self.reflection_flags)),
            "reflection_confidence": int(self.reflection_confidence),
        }


def _triage_db_path() -> Path:
    import os

    override = os.environ.get("IG_TRIAGE_DB", "").strip()
    if override:
        return Path(override)
    from system.paths import triage_db_path

    return triage_db_path()


def _session_started_iso(session_uptime_sec: float | None) -> str | None:
    if session_uptime_sec is None:
        return None
    try:
        started = datetime.now(timezone.utc).timestamp() - float(session_uptime_sec)
        return datetime.fromtimestamp(started, tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return None


def _fetch_session_trades(*, session_started: str | None) -> dict[str, Any]:
    """Read-only triage trade counts by path and profile."""
    trades_by_path: dict[str, int] = {p: 0 for p in _PATHS}
    trades_by_profile: dict[str, int] = {p: 0 for p in _PROFILES}
    closed_pnl: list[float] = []
    unrealised_pnl: list[float] = []
    total = 0

    path = _triage_db_path()
    if not path.is_file():
        return {
            "total_trades": 0,
            "trades_by_path": trades_by_path,
            "trades_by_strategy_profile": trades_by_profile,
            "closed_pnl_gbp": closed_pnl,
            "unrealised_pnl_gbp": unrealised_pnl,
        }

    try:
        from analytics.triage_db import connect_triage_sqlite_readonly

        conn = connect_triage_sqlite_readonly(path)
    except Exception:
        return {
            "total_trades": 0,
            "trades_by_path": trades_by_path,
            "trades_by_strategy_profile": trades_by_profile,
            "closed_pnl_gbp": closed_pnl,
            "unrealised_pnl_gbp": unrealised_pnl,
        }

    cutoff = session_started or "1970-01-01T00:00:00+00:00"
    try:
        cur = conn.execute(
            """
            SELECT deal_reference, epic, status, created_at
            FROM production_orders
            WHERE datetime(created_at) >= datetime(?)
            """,
            (cutoff.replace("T", " ").replace("+00:00", ""),),
        )
        for ref, _epic, status, _created in cur.fetchall():
            if str(status or "").upper() in ("REJECTED", "FAILED"):
                continue
            total += 1
            ref_s = str(ref or "")
            if ref_s.startswith("MICRO"):
                trades_by_path["MICRO"] += 1
                trades_by_profile["SCALP"] += 1
            else:
                trades_by_path["PATH_B_HANDOFF"] += 1
                trades_by_profile["ROTATION"] += 1
    except Exception:
        pass

    try:
        cur = conn.execute(
            """
            SELECT deal_id, epic, lifecycle_state, broker_upl, last_broker_sync_at
            FROM active_lifecycle_trades
            WHERE lifecycle_state NOT IN ('CLOSED', 'CLOSED_ON_BROKER_ANOMALY')
            """
        )
        for _deal, _epic, state, upl, _sync in cur.fetchall():
            if str(state or "").upper() in ("CLOSED",):
                continue
            total += 1
            trades_by_path["PATH_A"] += 1
            trades_by_profile["MOMENTUM"] += 1
            if upl is not None:
                try:
                    unrealised_pnl.append(float(upl))
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass

    try:
        cur = conn.execute(
            """
            SELECT net_pnl, gross_pnl, exit_timestamp
            FROM closed_positions
            WHERE datetime(exit_timestamp) >= datetime(?)
            """,
            (cutoff.replace("T", " ").replace("+00:00", ""),),
        )
        for net, gross, _exit in cur.fetchall():
            try:
                pnl = float(net if net is not None else gross or 0)
            except (TypeError, ValueError):
                pnl = 0.0
            closed_pnl.append(pnl)
    except Exception:
        pass
    finally:
        conn.close()

    return {
        "total_trades": total,
        "trades_by_path": trades_by_path,
        "trades_by_strategy_profile": trades_by_profile,
        "closed_pnl_gbp": closed_pnl,
        "unrealised_pnl_gbp": unrealised_pnl,
    }


def _count_block_posture(
    controller_decisions: list[dict[str, Any]] | None,
    enforcement_decisions: list[dict[str, Any]] | None,
) -> tuple[int, int]:
    """Snapshot-derived block posture counts (advisory proxy)."""
    controller_blocks = 0
    soft_blocks = 0
    for row in controller_decisions or []:
        controller_blocks += len(row.get("blocked_paths") or [])
    for row in enforcement_decisions or []:
        soft_blocks += len(row.get("soft_block_paths") or [])
    return controller_blocks, soft_blocks


def _time_in_profile(
    pipeline_rows: list[dict[str, Any]] | None,
    session_uptime_sec: float | None,
) -> dict[str, float]:
    uptime = float(session_uptime_sec or 0.0)
    counts: dict[str, int] = {p: 0 for p in _PROFILES}
    for row in pipeline_rows or []:
        profile = str(row.get("active_strategy_profile") or "UNKNOWN").upper()
        if profile in counts:
            counts[profile] += 1
        elif profile == "UNKNOWN":
            counts["STAND_DOWN"] += 0
    total = sum(counts.values()) or 1
    return {p: round(uptime * (counts[p] / total), 1) for p in _PROFILES}


def _feed_health_summary(api_feed_health: dict[str, Any] | None) -> dict[str, Any]:
    feeds = (api_feed_health or {}).get("feeds") or {}
    ok = sum(1 for meta in feeds.values() if (meta or {}).get("status") == "OK")
    degraded = sum(1 for meta in feeds.values() if (meta or {}).get("status") == "DEGRADED")
    return {
        "feeds_ok": ok,
        "feeds_degraded": degraded,
        "primary_feed": (api_feed_health or {}).get("ranking", {}).get("primary"),
        "overall": "DEGRADED" if _feed_degraded(api_feed_health or {}) else "OK",
    }


def _governance_summary(
    pipeline_governance: dict[str, Any] | None,
    session_governance: dict[str, Any] | None,
) -> dict[str, Any]:
    session_governance = session_governance or {}
    per_epic = (pipeline_governance or {}).get("per_epic") or []
    anomaly_count = sum(len(r.get("pipeline_anomalies") or []) for r in per_epic)
    return {
        "session_health_score": session_governance.get("overall_session_health_score"),
        "session_anomalies": list(session_governance.get("session_anomalies") or []),
        "epic_anomaly_count": anomaly_count,
        "epics_monitored": len(per_epic),
    }


def _volatility_summary(pipeline_rows: list[dict[str, Any]] | None) -> dict[str, Any]:
    z_scores: list[float] = []
    for row in pipeline_rows or []:
        epic = str(row.get("epic") or "")
        if not epic:
            continue
        z = _volatility_z(epic)
        if z is not None:
            z_scores.append(abs(z))
    if not z_scores:
        return {"epics_with_z": 0, "max_z": None, "mean_z": None, "spike": False}
    mean_z = sum(z_scores) / len(z_scores)
    max_z = max(z_scores)
    return {
        "epics_with_z": len(z_scores),
        "max_z": round(max_z, 3),
        "mean_z": round(mean_z, 3),
        "spike": max_z >= 2.0,
    }


def _drawdown_summary() -> dict[str, Any]:
    try:
        from system.drawdown_monitor import snapshot_for_telemetry

        snap = snapshot_for_telemetry()
        return {
            "max_drawdown_gbp": snap.get("max_drawdown_gbp"),
            "max_drawdown_pct": snap.get("max_drawdown_pct"),
            "current_drawdown_gbp": snap.get("current_drawdown_gbp"),
            "current_drawdown_pct": snap.get("current_drawdown_pct"),
            "operational_status": snap.get("operational_status"),
            "observations": snap.get("observations"),
        }
    except Exception:
        return {}


def _daily_loss_limit_summary() -> dict[str, Any]:
    try:
        from system.daily_loss_policy import daily_loss_gate_status

        ok, detail, meta = daily_loss_gate_status(None, None)
        return {
            "gate_ok": ok,
            "detail": detail,
            "meta": meta if isinstance(meta, dict) else {},
        }
    except Exception:
        return {"gate_ok": True, "detail": "unavailable", "meta": {}}


def _points_summary(closed_pnl: list[float], unrealised: list[float]) -> dict[str, Any]:
    total_closed = sum(closed_pnl) if closed_pnl else 0.0
    total_open = sum(unrealised) if unrealised else 0.0
    wins = sum(1 for p in closed_pnl if p > 0)
    losses = sum(1 for p in closed_pnl if p < 0)
    return {
        "closed_pnl_gbp": round(total_closed, 2),
        "unrealised_pnl_gbp": round(total_open, 2),
        "combined_pnl_gbp": round(total_closed + total_open, 2),
        "closed_wins": wins,
        "closed_losses": losses,
        "closed_trade_count": len(closed_pnl),
    }


def _derive_session_flags(
    *,
    trade_data: dict[str, Any],
    controller_blocks: int,
    soft_blocks: int,
    feed_summary: dict[str, Any],
    gov_summary: dict[str, Any],
    vol_summary: dict[str, Any],
    drawdown: dict[str, Any],
    session_uptime_sec: float | None,
    time_in_profile: dict[str, float] | None = None,
) -> list[str]:
    flags: list[str] = []
    total_trades = int(trade_data.get("total_trades") or 0)
    uptime_h = float(session_uptime_sec or 0) / 3600.0

    if soft_blocks + controller_blocks >= 6:
        flags.append("OVER_BLOCKED")
    if uptime_h >= 1.0 and total_trades <= 1:
        flags.append("UNDER_TRADING")
    if feed_summary.get("overall") == "DEGRADED":
        flags.append("FEED_DEGRADED")
    if vol_summary.get("spike"):
        flags.append("VOLATILITY_SPIKE")
    try:
        dd_pct = float(drawdown.get("max_drawdown_pct") or 0)
        if dd_pct >= 5.0:
            flags.append("DRAWDOWN_HIGH")
    except (TypeError, ValueError):
        pass
    try:
        score = int(gov_summary.get("session_health_score") or 100)
        if score < 50:
            flags.append("GOVERNANCE_WEAK")
    except (TypeError, ValueError):
        pass
    time_in = time_in_profile or {}
    total_time = sum(float(v or 0) for v in time_in.values()) or 1.0
    if float(time_in.get("STAND_DOWN") or 0) / total_time >= 0.35:
        flags.append("STAND_DOWN_DOMINANT")
    if "UNDER_TRADING" in flags and soft_blocks + controller_blocks >= 4:
        flags.append("OVER_BLOCKING_AGGRESSIVE")
    return flags


def _score_session(
    *,
    trade_data: dict[str, Any],
    gov_summary: dict[str, Any],
    feed_summary: dict[str, Any],
    drawdown: dict[str, Any],
    flags: list[str],
) -> tuple[int, int, int]:
    quality = 70
    risk = 30
    stability = 70

    closed = trade_data.get("closed_pnl_gbp") or []
    if closed:
        wins = sum(1 for p in closed if p > 0)
        win_rate = wins / len(closed)
        quality += int((win_rate - 0.5) * 30)

    try:
        gov_score = int(gov_summary.get("session_health_score") or 80)
        quality += (gov_score - 70) // 5
        stability += (gov_score - 70) // 4
    except (TypeError, ValueError):
        pass

    if feed_summary.get("overall") == "DEGRADED":
        quality -= 15
        stability -= 20
        risk += 15

    try:
        dd_pct = float(drawdown.get("max_drawdown_pct") or 0)
        risk += min(40, int(dd_pct * 4))
        stability -= min(30, int(dd_pct * 3))
    except (TypeError, ValueError):
        pass

    if "OVER_BLOCKED" in flags:
        quality -= 10
        stability -= 15
    if "UNDER_TRADING" in flags:
        quality -= 5
    if "VOLATILITY_SPIKE" in flags:
        risk += 15
        stability -= 10
    if "DRAWDOWN_HIGH" in flags:
        risk += 20

    return (
        max(0, min(100, quality)),
        max(0, min(100, risk)),
        max(0, min(100, stability)),
    )


def build_session_review(
    *,
    trade_pipeline_health: list[dict[str, Any]] | None = None,
    pipeline_governance: dict[str, Any] | None = None,
    session_governance: dict[str, Any] | None = None,
    api_feed_health: dict[str, Any] | None = None,
    market_rotation_status: dict[str, Any] | None = None,
    strategy_selector_advice: list[dict[str, Any]] | None = None,
    strategy_transition_advice: list[dict[str, Any]] | None = None,
    strategy_controller_decisions: list[dict[str, Any]] | None = None,
    strategy_enforcement_decisions: list[dict[str, Any]] | None = None,
    session_uptime_sec: float | None = None,
) -> dict[str, Any]:
    """Build structured session review — advisory only."""
    session_started = _session_started_iso(session_uptime_sec)
    trade_data = _fetch_session_trades(session_started=session_started)
    controller_blocks, soft_blocks = _count_block_posture(
        strategy_controller_decisions,
        strategy_enforcement_decisions,
    )
    feed_summary = _feed_health_summary(api_feed_health)
    gov_summary = _governance_summary(pipeline_governance, session_governance)
    vol_summary = _volatility_summary(trade_pipeline_health)
    drawdown = _drawdown_summary()
    daily_loss = _daily_loss_limit_summary()
    points = _points_summary(
        trade_data.get("closed_pnl_gbp") or [],
        trade_data.get("unrealised_pnl_gbp") or [],
    )
    time_in_profile = _time_in_profile(trade_pipeline_health, session_uptime_sec)

    flags = _derive_session_flags(
        trade_data=trade_data,
        controller_blocks=controller_blocks,
        soft_blocks=soft_blocks,
        feed_summary=feed_summary,
        gov_summary=gov_summary,
        vol_summary=vol_summary,
        drawdown=drawdown,
        session_uptime_sec=session_uptime_sec,
        time_in_profile=time_in_profile,
    )
    quality, risk, stability = _score_session(
        trade_data=trade_data,
        gov_summary=gov_summary,
        feed_summary=feed_summary,
        drawdown=drawdown,
        flags=flags,
    )

    review = SessionReview(
        session_summary={
            "total_trades": trade_data["total_trades"],
            "trades_by_path": trade_data["trades_by_path"],
            "trades_by_strategy_profile": trade_data["trades_by_strategy_profile"],
            "soft_blocks_count": soft_blocks,
            "controller_blocks_count": controller_blocks,
            "time_in_profile": time_in_profile,
            "feed_health_summary": feed_summary,
            "governance_summary": gov_summary,
            "volatility_summary": vol_summary,
            "drawdown_summary": drawdown,
            "daily_loss_limit_summary": daily_loss,
            "points_summary": points,
            "rotation_state": (market_rotation_status or {}).get("rotation_state"),
            "selector_epics": len(strategy_selector_advice or []),
            "transition_epics": len(strategy_transition_advice or []),
        },
        session_quality_score=quality,
        session_risk_score=risk,
        session_stability_score=stability,
        session_flags=flags,
    )
    return review.to_dict()


def build_loosening_advice(session_review: dict[str, Any]) -> dict[str, Any]:
    """Advisory-only recommendations for loosening trading restrictions."""
    quality = int(session_review.get("session_quality_score") or 0)
    risk = int(session_review.get("session_risk_score") or 100)
    flags = set(session_review.get("session_flags") or [])
    summary = session_review.get("session_summary") or {}
    drawdown = summary.get("drawdown_summary") or {}
    gov = summary.get("governance_summary") or {}

    changes: list[str] = []
    loosening_flags: list[str] = []
    reasons: list[str] = []
    confidence = 40

    stand_down_active = "STAND_DOWN_DOMINANT" in flags or "GOVERNANCE_WEAK" in flags

    if stand_down_active:
        changes.append("Do not loosen restrictions — STAND_DOWN or weak governance posture active")
        loosening_flags.append("STAND_DOWN_SUPPRESS")
        reasons.append("STAND_DOWN/governance suppresses loosening recommendations")
        confidence = 85
    elif quality >= 70 and risk <= 40:
        changes.extend(
            [
                "Consider raising trade frequency caps slightly",
                "Consider relaxing micro cadence throttle by 10–15%",
                "Consider loosening Path A gating strictness (confidence floor −2%)",
            ]
        )
        loosening_flags.append("HIGH_QUALITY_LOW_RISK")
        reasons.append(f"quality={quality} risk={risk}")
        confidence = max(confidence, 75)

    if not stand_down_active:
        try:
            dd_pct = float(drawdown.get("max_drawdown_pct") or 0)
            gov_clean = not gov.get("session_anomalies") and int(gov.get("epic_anomaly_count") or 0) == 0
            if dd_pct < 2.0 and gov_clean:
                changes.append("Consider raising daily loss limit buffer by £1–2 (advisory)")
                loosening_flags.append("SHALLOW_DRAWDOWN")
                reasons.append(f"drawdown {dd_pct:.1f}% with clean governance")
                confidence = max(confidence, 65)
        except (TypeError, ValueError):
            pass

    if "UNDER_TRADING" in flags and not stand_down_active:
        changes.append("Consider lowering SCALP soft-block threshold from 70 → 60")
        changes.append("Consider reducing STAND_DOWN sensitivity when feed health OK")
        loosening_flags.append("UNDER_TRADING")
        reasons.append("session under-trading relative to uptime")
        confidence = max(confidence, 60)

    if "OVER_BLOCKED" in flags:
        changes.append("Consider raising controller-block confidence thresholds by 5–10 points")
        changes.append("Consider narrowing soft-block path coverage for ROTATION epics")
        loosening_flags.append("OVER_BLOCKED")
        reasons.append("elevated block posture across epics")
        confidence = max(confidence, 55)

    if not changes:
        changes.append("No loosening recommended — maintain current restriction posture")
        reasons.append("session metrics do not support restriction easing")

    advice = LooseningAdvice(
        recommended_changes=changes,
        confidence=min(100, confidence),
        reason="; ".join(reasons),
        loosening_flags=loosening_flags,
    )
    return advice.to_dict()


def build_self_reflection(
    session_review: dict[str, Any],
    *,
    strategy_selector_advice: list[dict[str, Any]] | None = None,
    strategy_transition_advice: list[dict[str, Any]] | None = None,
    strategy_controller_decisions: list[dict[str, Any]] | None = None,
    strategy_enforcement_decisions: list[dict[str, Any]] | None = None,
    trade_pipeline_health: list[dict[str, Any]] | None = None,
    loosening_advice: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Critical structured self-assessment — advisory only."""
    flags: list[str] = []
    weaknesses: list[str] = []
    contradictions: list[str] = []
    missed: list[str] = []
    misalignments: list[str] = []
    suggestions: list[str] = []

    summary = session_review.get("session_summary") or {}
    session_flags = set(session_review.get("session_flags") or [])
    quality = int(session_review.get("session_quality_score") or 0)
    stability = int(session_review.get("session_stability_score") or 0)
    loosening = loosening_advice or {}

    if "OVER_BLOCKED" in session_flags or "OVER_BLOCKING_AGGRESSIVE" in session_flags:
        weaknesses.append("Strategy controller and soft enforcement may be over-blocking viable entries")
        suggestions.append("Review block threshold alignment between selector, controller, and enforcement layers")
        suggestions.append("Consider raising OWNERSHIP_CONFIDENCE_THRESHOLD from 70 → 75 for soft blocks")
        flags.append("OVER_BLOCKING")

    if "UNDER_TRADING" in session_flags:
        weaknesses.append("Session trade count is low relative to uptime and active epics")
        missed.append("Potential entries may have been skipped due to stacked guards or STAND_DOWN posture")
        flags.append("UNDER_TRADING")

    if "FEED_DEGRADED" in session_flags:
        weaknesses.append("Feed degradation reduced confidence in entry timing across epics")
        suggestions.append("Prioritise feed recovery before increasing trade frequency")

    if "STAND_DOWN_DOMINANT" in session_flags:
        weaknesses.append("STAND_DOWN posture dominated session — may be over-applied")
        suggestions.append("Tighten STAND_DOWN triggers to require multiple critical signals, not feed alone")
        flags.append("STAND_DOWN_OVERUSED")

    selector_by_epic = {r["epic"]: r for r in (strategy_selector_advice or []) if r.get("epic")}
    controller_by_epic = {r["epic"]: r for r in (strategy_controller_decisions or []) if r.get("epic")}
    enforcement_by_epic = {r["epic"]: r for r in (strategy_enforcement_decisions or []) if r.get("epic")}
    transition_by_epic = {r["epic"]: r for r in (strategy_transition_advice or []) if r.get("epic")}

    for row in trade_pipeline_health or []:
        epic = str(row.get("epic") or "")
        active = str(row.get("active_strategy_profile") or "").upper()
        source = str(row.get("strategy_source") or "").upper()
        if active == "MOMENTUM" and source == "MICRO":
            contradictions.append(f"{epic}: MOMENTUM profile tagged but MICRO source active")
            misalignments.append(f"{epic}: profile/source mismatch")
        if active == "SCALP" and source == "PATH_A":
            contradictions.append(f"{epic}: SCALP profile tagged but Path A source active")

    for epic, sel in selector_by_epic.items():
        rec = str(sel.get("recommended_strategy_profile") or "").upper()
        ctrl = controller_by_epic.get(epic) or {}
        enf = enforcement_by_epic.get(epic) or {}
        own = str(ctrl.get("ownership") or "").upper()
        if rec and own and rec != own and rec != "STAND_DOWN":
            misalignments.append(f"{epic}: selector recommends {rec} but controller ownership is {own}")
            flags.append("SELECTOR_CONTROLLER_DRIFT")

        rec_paths = _paths_for_profile(rec)
        blocked_ctrl = set(ctrl.get("blocked_paths") or [])
        blocked_soft = set(enf.get("soft_block_paths") or [])
        for path in rec_paths:
            if path in blocked_ctrl and path in blocked_soft:
                contradictions.append(
                    f"{epic}: selector recommends {rec} but controller and enforcement both block {path}"
                )
                flags.append("SELECTOR_ENFORCEMENT_CONFLICT")
            elif rec == "SCALP" and path == "MICRO" and path in blocked_ctrl.union(blocked_soft):
                contradictions.append(
                    f"{epic}: selector recommends SCALP but execution path MICRO is blocked"
                )
                flags.append("SELECTOR_ENFORCEMENT_CONFLICT")

        sel_stand = rec == "STAND_DOWN"
        ctrl_stand = own == "STAND_DOWN"
        if sel_stand and not ctrl_stand:
            misalignments.append(f"{epic}: selector STAND_DOWN but controller ownership is {own or 'UNKNOWN'}")
            flags.append("STAND_DOWN_UNDERUSED")
        if ctrl_stand and not sel_stand and rec:
            misalignments.append(f"{epic}: controller STAND_DOWN but selector recommends {rec}")
            flags.append("STAND_DOWN_OVERUSED")

    for epic, trans in transition_by_epic.items():
        try:
            t_conf = int(trans.get("transition_confidence") or 0)
        except (TypeError, ValueError):
            t_conf = 0
        if t_conf < 80:
            continue
        target = str(trans.get("target_profile") or "").upper()
        current = str(trans.get("current_profile") or "").upper()
        if not target or target == current:
            continue
        enf = enforcement_by_epic.get(epic) or {}
        blocked_soft = set(enf.get("soft_block_paths") or [])
        target_paths = _paths_for_profile(target)
        for path in target_paths:
            if path in blocked_soft:
                misalignments.append(
                    f"{epic}: transition to {target} ({t_conf}%) but enforcement soft-blocks {path}"
                )
                flags.append("TRANSITION_ENFORCEMENT_MISALIGN")
        if target == "MOMENTUM" and "PATH_A" in blocked_soft:
            misalignments.append(
                f"{epic}: SCALP→MOMENTUM transition ({t_conf}%) but Path A soft-blocked"
            )
            flags.append("TRANSITION_ENFORCEMENT_MISALIGN")

    high_transitions = [
        r
        for r in (strategy_transition_advice or [])
        if int(r.get("transition_confidence") or 0) >= 80
        and str(r.get("current_profile") or "") != str(r.get("target_profile") or "")
    ]
    if high_transitions:
        missed.append(
            f"{len(high_transitions)} high-confidence profile transitions pending — "
            "execution still on legacy profile paths"
        )
        flags.append("TRANSITION_LAG")

    stand_down_time = float((summary.get("time_in_profile") or {}).get("STAND_DOWN") or 0)
    uptime_proxy = sum(float(v or 0) for v in (summary.get("time_in_profile") or {}).values()) or 1.0
    if stand_down_time / uptime_proxy > 0.4:
        if "STAND_DOWN_OVERUSED" not in flags:
            weaknesses.append("Extended STAND_DOWN posture — opportunity cost elevated")
            suggestions.append("Review STAND_DOWN triggers for false positives when governance is clean")

    points = summary.get("points_summary") or {}
    if float(points.get("closed_pnl_gbp") or 0) > 0 and "UNDER_TRADING" in session_flags:
        missed.append(
            "Profitable closed P&L exists but session flagged UNDER_TRADING — over-blocking likely cost entries"
        )
        flags.append("MISSED_PNL_OPPORTUNITY")

    loosening_flags = set(loosening.get("loosening_flags") or [])
    if "STAND_DOWN_DOMINANT" in session_flags and "HIGH_QUALITY_LOW_RISK" in loosening_flags:
        contradictions.append("STAND_DOWN dominant but loosening_advice suggests increasing trade freedom")
        flags.append("LOOSENING_STAND_DOWN_INCONSISTENCY")

    pattern_count = len({f for f in flags if f.endswith("_CONFLICT") or f.endswith("_MISALIGN")})
    reflection_confidence = min(100, max(40, (quality + stability) // 2 + pattern_count * 5))
    critique = (
        f"Session quality {quality}/100, stability {stability}/100. "
        f"Identified {len(weaknesses)} weakness(es), {len(contradictions)} contradiction(s), "
        f"{len(misalignments)} strategy misalignment(s)."
    )

    reflection = SelfReflection(
        critique_summary=critique,
        weaknesses=weaknesses,
        contradictions=contradictions,
        missed_opportunities=missed,
        strategy_misalignments=misalignments,
        improvement_suggestions=suggestions or ["Maintain current strategy layering — no critical gaps detected"],
        reflection_flags=sorted(set(flags)),
        reflection_confidence=reflection_confidence,
    )
    return reflection.to_dict()


def build_session_review_bundle(
    *,
    trade_pipeline_health: list[dict[str, Any]] | None = None,
    pipeline_governance: dict[str, Any] | None = None,
    session_governance: dict[str, Any] | None = None,
    api_feed_health: dict[str, Any] | None = None,
    market_rotation_status: dict[str, Any] | None = None,
    strategy_selector_advice: list[dict[str, Any]] | None = None,
    strategy_transition_advice: list[dict[str, Any]] | None = None,
    strategy_controller_decisions: list[dict[str, Any]] | None = None,
    strategy_enforcement_decisions: list[dict[str, Any]] | None = None,
    session_uptime_sec: float | None = None,
) -> dict[str, Any]:
    """Full advisory bundle: review + loosening + self-reflection."""
    review = build_session_review(
        trade_pipeline_health=trade_pipeline_health,
        pipeline_governance=pipeline_governance,
        session_governance=session_governance,
        api_feed_health=api_feed_health,
        market_rotation_status=market_rotation_status,
        strategy_selector_advice=strategy_selector_advice,
        strategy_transition_advice=strategy_transition_advice,
        strategy_controller_decisions=strategy_controller_decisions,
        strategy_enforcement_decisions=strategy_enforcement_decisions,
        session_uptime_sec=session_uptime_sec,
    )
    loosening = build_loosening_advice(review)
    return {
        "session_review": review,
        "loosening_advice": loosening,
        "self_reflection": build_self_reflection(
            review,
            strategy_selector_advice=strategy_selector_advice,
            strategy_transition_advice=strategy_transition_advice,
            strategy_controller_decisions=strategy_controller_decisions,
            strategy_enforcement_decisions=strategy_enforcement_decisions,
            trade_pipeline_health=trade_pipeline_health,
            loosening_advice=loosening,
        ),
    }
