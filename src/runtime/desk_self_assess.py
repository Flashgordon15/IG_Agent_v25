"""Trading Desk self-assessment — why idle, bridge stale hub quotes, harden recovery.

Permanent operator-facing loop that questions performance when the desk is
idle or fail-closed, and heals hub starvation by re-publishing Yahoo mids.
"""

from __future__ import annotations

import time
from typing import Any

from system.engine_log import log_engine

_LAST_BRIDGE_MONO = 0.0
_BRIDGE_COOLDOWN_SEC = 3.0
_LAST_ASSESS: dict[str, Any] = {}


def _active_epics() -> list[str]:
    try:
        from runtime.dual_core_execution import get_active_stack_epics

        epics = [str(e) for e in (get_active_stack_epics() or []) if e]
        if epics:
            return epics
    except Exception:
        pass
    return [
        "IX.D.DOW.IFM.IP",
        "IX.D.NIKKEI.IFM.IP",
        "CS.D.CFPGOLD.CFP.IP",
        "CS.D.EURUSD.CFD.IP",
    ]


def bridge_stale_hub_from_yahoo(*, force: bool = False) -> dict[str, Any]:
    """If hub quotes are older than the entry budget, fetch Yahoo and publish.

    This closes the gap where MultiFeed ring ticks climb but MarketDataHub
    epochs freeze (Gate 3 WAITING / entries fail-closed).
    """
    global _LAST_BRIDGE_MONO
    now_m = time.monotonic()
    if not force and now_m - _LAST_BRIDGE_MONO < _BRIDGE_COOLDOWN_SEC:
        return {"ok": False, "skipped": True, "reason": "cooldown"}

    from system.market_data_hub import get_market_data_hub
    from system.market_integrity import effective_entry_quote_budget_sec

    budget = float(effective_entry_quote_budget_sec())
    hub = get_market_data_hub()
    epics = _active_epics()
    stale: list[str] = []
    for epic in epics:
        try:
            snap = hub.get_snapshot(epic)
            age = float(snap.age_seconds()) if snap is not None else 1e9
        except Exception:
            age = 1e9
        if age > budget:
            stale.append(epic)
    if not stale:
        return {"ok": True, "bridged": [], "stale": [], "budget_sec": budget}

    bridged: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        from feeder.yahoo_quote_poller import fetch_yahoo_quote
    except Exception as exc:
        return {"ok": False, "error": f"yahoo_import:{type(exc).__name__}"}

    try:
        from system.packet_validator import (
            clear_feed_circuit_breaker_for_heal,
            reanchor_epic_mid,
        )

        clear_feed_circuit_breaker_for_heal(reason="yahoo_hub_bridge")
    except Exception as exc:
        errors.append(f"validator_prep:{type(exc).__name__}")

        def reanchor_epic_mid(epic: str) -> None:  # type: ignore[misc]
            return None

    for epic in stale:
        try:
            sample = fetch_yahoo_quote(epic)
            if sample is None:
                errors.append(f"{epic}:no_sample")
                continue
            bid = float(getattr(sample, "bid", 0) or 0)
            offer = float(getattr(sample, "offer", 0) or 0)
            if bid <= 0 or offer <= 0:
                mid = float(getattr(sample, "mid", 0) or 0)
                if mid <= 0:
                    errors.append(f"{epic}:bad_quote")
                    continue
                half = max(mid * 0.00005, 0.01)
                bid, offer = mid - half, mid + half
            published = None
            # Retry: re-anchor jump guard, then optionally drop a stale cache row.
            for attempt in range(4):
                reanchor_epic_mid(epic)
                if attempt >= 2:
                    try:
                        hub.invalidate(epic)
                    except Exception:
                        pass
                published = hub.publish(epic, bid, offer, source="yahoo")
                if published is not None and float(published.age_seconds()) <= budget:
                    break
            if published is None or float(published.age_seconds()) > budget:
                err = "publish_rejected"
                try:
                    from system.packet_validator import feed_circuit_breaker_active

                    if feed_circuit_breaker_active():
                        err = "circuit_breaker"
                except Exception:
                    pass
                if published is not None:
                    err = f"stale_after_publish:{published.age_seconds():.1f}s"
                errors.append(f"{epic}:{err}")
                continue
            bridged.append(
                {
                    "epic": epic,
                    "mid": round((bid + offer) / 2.0, 4),
                    "source": "yahoo",
                    "age_sec": round(float(published.age_seconds()), 3),
                }
            )
        except Exception as exc:
            errors.append(f"{epic}:{type(exc).__name__}")

    _LAST_BRIDGE_MONO = now_m
    if bridged:
        log_engine(
            f"desk_self_assess: bridged {len(bridged)} stale hub quotes via Yahoo "
            f"(budget={budget:.1f}s)"
        )
    return {
        "ok": bool(bridged),
        "bridged": bridged,
        "stale": stale,
        "errors": errors,
        "budget_sec": budget,
    }


def build_why_idle_payload() -> dict[str, Any]:
    """Aggregate entry blockers + performance questions for the desk AI strip."""
    global _LAST_ASSESS
    from system.market_integrity import effective_entry_quote_budget_sec

    budget = float(effective_entry_quote_budget_sec())
    blockers: list[dict[str, Any]] = []
    questions: list[str] = []

    # Hub / Gate 3
    hub_ages: dict[str, float] = {}
    try:
        from system.market_data_hub import get_market_data_hub

        hub = get_market_data_hub()
        for epic in _active_epics():
            snap = hub.get_snapshot(epic)
            if snap is None:
                hub_ages[epic] = -1.0
            else:
                hub_ages[epic] = round(float(snap.age_seconds()), 2)
        worst = max((a for a in hub_ages.values() if a >= 0), default=-1.0)
        if worst < 0 or worst > budget:
            blockers.append(
                {
                    "id": "hub_quote_stale",
                    "severity": 0,
                    "detail": f"hub max age {worst}s > budget {budget}s",
                    "ages": hub_ages,
                }
            )
            questions.append(
                "Why are hub quotes older than the entry budget while the desk UI shows LIVE?"
            )
    except Exception as exc:
        blockers.append(
            {"id": "hub_quote_error", "severity": 0, "detail": type(exc).__name__}
        )

    # Fulfillment / sniper
    try:
        from system.unified_fulfillment_cache import get_fulfillment_payload

        ful = get_fulfillment_payload() or {}
        qf = ful.get("quote_freshness") or {}
        if not ful.get("all_ready") or not qf.get("fresh"):
            blockers.append(
                {
                    "id": "fulfillment_fail_closed",
                    "severity": 0,
                    "detail": (
                        f"all_ready={ful.get('all_ready')} "
                        f"quotes_fresh={qf.get('fresh')} "
                        f"age={qf.get('age_sec')} budget={qf.get('budget_sec')}"
                    ),
                }
            )
            questions.append("Is sniper correctly fail-closed, or is the feed heal path stuck?")
        if ful.get("trading_paused"):
            blockers.append(
                {
                    "id": "trading_paused",
                    "severity": 1,
                    "detail": str(ful.get("critical_alert") or "trading_paused"),
                }
            )
    except Exception:
        pass

    # Gate stack
    gs: dict[str, Any] = {}
    try:
        from runtime.dual_core_execution import resolve_core_b_gate_stack

        gs = resolve_core_b_gate_stack() or {}
    except Exception:
        gs = {}
    if isinstance(gs, dict) and gs.get("all_clear") is False:
        waiting = [
            g
            for g in (gs.get("gates") or [])
            if isinstance(g, dict) and str(g.get("status")).upper() in ("WAITING", "FAILED", "BLOCKED")
        ]
        if waiting:
            blockers.append(
                {
                    "id": "gate_stack",
                    "severity": 0,
                    "detail": "; ".join(
                        f"G{g.get('gate')} {g.get('name')}: {g.get('detail')}" for g in waiting[:4]
                    ),
                }
            )

    # Quality / loss streak
    try:
        from system.strategy_quality_gate import (
            consecutive_managed_loss_streak,
            evaluate_session_win_rate_gate,
            strategy_quality_enabled,
        )

        if strategy_quality_enabled():
            streak = consecutive_managed_loss_streak()
            passed, detail, value = evaluate_session_win_rate_gate()
            if not passed:
                blockers.append(
                    {
                        "id": "strategy_quality",
                        "severity": 1,
                        "detail": detail,
                        "value": {
                            "loss_streak": streak,
                            "win_rate": (value or {}).get("win_rate"),
                        },
                    }
                )
                questions.append(
                    f"Loss streak={streak} — are fail-safe null-UPL closes inflating the pause?"
                )
    except Exception:
        pass

    # Book / P&L
    try:
        from diagnostics.performance_journal import milestone_progress_payload

        mile = milestone_progress_payload() or {}
        realized = float(mile.get("daily_realized_pnl_gbp") or 0)
        if abs(realized) < 1e-9:
            questions.append(
                "Daily realized is £0 — is that zero settled cash, or a wiped provisional ledger?"
            )
    except Exception:
        pass

    blockers.sort(key=lambda b: int(b.get("severity", 9)))
    # Entry path: hub freshness + Gate 3. Stage-light / ring DESYNC alone
    # must not mark the desk entry-blocked when Yahoo hub ages are inside budget.
    hard = [
        b
        for b in blockers
        if b.get("id") in ("hub_quote_stale", "hub_quote_error", "gate_stack", "strategy_quality")
    ]
    idle = bool(hard)
    primary = (hard[0] if hard else None) or (blockers[0] if blockers else None)
    entries_allowed = not idle
    # Prefer live fulfillment quote_freshness when available
    try:
        from system.unified_fulfillment_cache import get_fulfillment_payload

        ful = get_fulfillment_payload() or {}
        qf = ful.get("quote_freshness") or {}
        if qf.get("fresh") and not any(
            b.get("id") == "strategy_quality" for b in hard
        ):
            entries_allowed = True
            idle = False
    except Exception:
        pass
    out = {
        "ok": True,
        "idle": idle,
        "entries_allowed": entries_allowed,
        "primary_blocker": primary,
        "blockers": blockers,
        "self_questions": questions,
        "hub_ages": hub_ages,
        "quote_budget_sec": budget,
        "assessed_at": time.time(),
        "recommendation": (
            "bridge_stale_hub_from_yahoo"
            if primary and primary.get("id") == "hub_quote_stale"
            else ("clear_quality_pause" if primary and primary.get("id") == "strategy_quality" else "monitor")
        ),
    }
    _LAST_ASSESS = dict(out)
    return out


def run_self_assess_tick(*, heal: bool = True) -> dict[str, Any]:
    """Assess → optional Yahoo hub bridge → re-assess."""
    before = build_why_idle_payload()
    heal_result: dict[str, Any] | None = None
    if heal and before.get("recommendation") == "bridge_stale_hub_from_yahoo":
        heal_result = bridge_stale_hub_from_yahoo()
        after = build_why_idle_payload()
    else:
        after = before
    return {"before": before, "heal": heal_result, "after": after}


def last_assessment() -> dict[str, Any]:
    return dict(_LAST_ASSESS) if _LAST_ASSESS else build_why_idle_payload()
