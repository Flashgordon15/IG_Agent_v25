"""Staged readiness model — G1–G4 levels and per-subsystem visibility (telemetry only).

Gate semantics (reporting; safety enforcement unchanged elsewhere):

| Gate | Role                         | Blocks trading? | Cockpit visibility      |
|------|------------------------------|-----------------|-------------------------|
| G1   | Preflight / API bind         | Yes if FAILED   | Level 1 — platform up   |
| G2   | Broker auth / hydration      | Yes (no session)| Level 2 — session/feeds |
| G3   | Quote streaming              | Yes (no quotes) | Level 3 — live ticks    |
| G4   | OHLC + loop construction     | Yes (no loops)  | Level 4 — routing warm  |
| G5   | Trading active (outside 0–4) | Unpause valve   | Full dispatch           |

Risk and hard-enforcement guards remain authoritative for order safety.
This module only stages *reporting* so the cockpit can show partial readiness.
"""

from __future__ import annotations

from typing import Any

from system.system_state import GateStatus

_NIGHT_MATRIX_EPICS = 4

_READINESS_LABELS: dict[int, str] = {
    0: "Initializing",
    1: "Platform online",
    2: "Feeds live",
    3: "Routing warming",
    4: "Fully operational",
}

_GATE_TO_LEVEL: dict[str, int] = {
    "G1": 1,
    "G2": 2,
    "G3": 3,
    "G4": 4,
}


def _norm_gate_status(raw: Any) -> str:
    if hasattr(raw, "status"):
        return str(raw.status).lower()
    if isinstance(raw, dict):
        return str(raw.get("status") or GateStatus.PENDING).lower()
    return str(raw or GateStatus.PENDING).lower()


def gate_readiness_level(gates: dict[str, Any] | None) -> int:
    """Map completed G1–G4 gates to readiness level 0–4."""
    gates = gates or {}
    if any(_norm_gate_status(gates.get(gid)) == str(GateStatus.FAILED) for gid in ("G1", "G2", "G3", "G4")):
        return 0
    level = 0
    for gid in ("G1", "G2", "G3", "G4"):
        if _norm_gate_status(gates.get(gid)) == str(GateStatus.COMPLETE):
            level = _GATE_TO_LEVEL[gid]
    return level


def _feed_rows(api_feed_health: Any) -> list[dict[str, Any]]:
    if isinstance(api_feed_health, list):
        return [r for r in api_feed_health if isinstance(r, dict)]
    if isinstance(api_feed_health, dict):
        nested = api_feed_health.get("feeds")
        if isinstance(nested, dict):
            return [{"id": k, **(v if isinstance(v, dict) else {})} for k, v in nested.items()]
        if isinstance(nested, list):
            return [r for r in nested if isinstance(r, dict)]
    return []


def _feed_fresh_count(rows: list[dict[str, Any]]) -> tuple[int, int]:
    if not rows:
        return 0, 0
    fresh = 0
    for row in rows:
        if row.get("fresh") is True:
            fresh += 1
            continue
        status = str(row.get("status") or "").upper()
        if status == "OK":
            fresh += 1
    return fresh, len(rows)


def _subsystem_feeds(api_feed_health: Any) -> dict[str, Any]:
    rows = _feed_rows(api_feed_health)
    fresh, total = _feed_fresh_count(rows)
    if total == 0:
        return {
            "level": "cold",
            "ready": False,
            "fresh_count": 0,
            "total_count": 0,
            "detail": "Awaiting feed telemetry",
        }
    if fresh >= _NIGHT_MATRIX_EPICS or fresh == total:
        level = "ready"
    elif fresh > 0:
        level = "partial"
    else:
        level = "warming"
    return {
        "level": level,
        "ready": level in ("partial", "ready"),
        "fresh_count": fresh,
        "total_count": total,
        "detail": f"{fresh}/{total} feeds fresh",
    }


def _subsystem_routing(routes: list[Any] | None) -> dict[str, Any]:
    rows = [r for r in (routes or []) if isinstance(r, dict)]
    count = len(rows)
    armed = sum(
        1
        for r in rows
        if str(r.get("execution_path") or "NONE").upper() != "NONE"
    )
    if count == 0:
        return {
            "level": "cold",
            "ready": False,
            "route_count": 0,
            "armed_count": 0,
            "detail": "Route cache warming",
        }
    if count >= _NIGHT_MATRIX_EPICS and armed > 0:
        level = "ready"
    elif count > 0:
        level = "partial" if armed > 0 else "warming"
    else:
        level = "warming"
    return {
        "level": level,
        "ready": level in ("partial", "ready"),
        "route_count": count,
        "armed_count": armed,
        "detail": f"{armed}/{max(count, _NIGHT_MATRIX_EPICS)} routes armed",
    }


def _subsystem_risk(envelope: list[Any] | None) -> dict[str, Any]:
    rows = [r for r in (envelope or []) if isinstance(r, dict)]
    if not rows:
        return {
            "level": "cold",
            "ready": False,
            "detail": "Risk envelope populating",
        }
    profile = str(rows[0].get("risk_profile") or rows[0].get("profile") or "loaded")
    return {
        "level": "ready",
        "ready": True,
        "detail": profile,
        "row_count": len(rows),
    }


def _subsystem_governance(
    pipeline_governance: dict[str, Any] | None,
    hard_enforcement: list[Any] | None,
    session_governance: dict[str, Any] | None,
) -> dict[str, Any]:
    hard = [r for r in (hard_enforcement or []) if isinstance(r, dict)]
    active_hard = [r for r in hard if r.get("active")]
    gov = pipeline_governance if isinstance(pipeline_governance, dict) else {}
    session = session_governance if isinstance(session_governance, dict) else {}

    if active_hard:
        return {
            "level": "restricted",
            "ready": True,
            "active_blocks": len(active_hard),
            "detail": f"{len(active_hard)} hard enforcement block(s)",
        }
    if gov or hard is not None or session:
        posture = str(gov.get("risk_posture") or "nominal")
        return {
            "level": "ready",
            "ready": True,
            "active_blocks": 0,
            "detail": posture,
        }
    return {
        "level": "warming",
        "ready": False,
        "active_blocks": 0,
        "detail": "Governance advisory warming",
    }


def _subsystem_derived_level(subsystems: dict[str, dict[str, Any]]) -> int:
    """Best-effort level from live telemetry when gates lag behind."""
    feeds = subsystems.get("feeds") or {}
    routing = subsystems.get("routing") or {}
    risk = subsystems.get("risk") or {}
    gov = subsystems.get("governance") or {}

    if gov.get("level") == "restricted":
        return max(1, 2)
    if routing.get("ready"):
        return 3
    if feeds.get("ready"):
        return 2
    if risk.get("ready"):
        return 2
    if feeds.get("level") == "warming":
        return 1
    return 0


def build_readiness_bundle(
    *,
    gate_progression: dict[str, Any] | None = None,
    api_feed_health: Any = None,
    unified_execution_route: list[Any] | None = None,
    regime_risk_envelope: list[Any] | None = None,
    pipeline_governance: dict[str, Any] | None = None,
    session_governance: dict[str, Any] | None = None,
    hard_enforcement_decisions: list[Any] | None = None,
    session_status: str | None = None,
    snapshot_warming: bool = False,
) -> dict[str, Any]:
    """Compose readiness_level, labels, and per-subsystem readiness for GUI/API."""
    prog = gate_progression or {}
    gates = prog.get("gates") or {}
    gate_level = gate_readiness_level(gates)
    failed = any(
        _norm_gate_status(gates.get(gid)) == str(GateStatus.FAILED)
        for gid in ("G1", "G2", "G3", "G4")
    )

    subsystems = {
        "feeds": _subsystem_feeds(api_feed_health),
        "routing": _subsystem_routing(unified_execution_route),
        "risk": _subsystem_risk(regime_risk_envelope),
        "governance": _subsystem_governance(
            pipeline_governance,
            hard_enforcement_decisions,
            session_governance,
        ),
    }

    telemetry_level = _subsystem_derived_level(subsystems)
    readiness_level = max(gate_level, telemetry_level)
    if failed:
        readiness_level = 0

    if snapshot_warming and readiness_level < 1 and gate_level >= 1:
        readiness_level = gate_level

    label = _READINESS_LABELS.get(readiness_level, "Initializing")
    if failed:
        label = "Boot failed"
    elif readiness_level < 4 and telemetry_level >= 2 and gate_level < telemetry_level:
        label = "Platform warming"

    session_ok = str(session_status or "").upper() == "HEALTHY"
    cockpit_usable = (
        not failed
        and readiness_level >= 1
        and (
            subsystems["feeds"]["ready"]
            or subsystems["routing"]["level"] != "cold"
            or subsystems["risk"]["ready"]
            or session_ok
        )
    )

    trading_ready = bool(prog.get("operational_ready")) and readiness_level >= 4
    if subsystems["governance"].get("level") == "restricted":
        trading_ready = False

    return {
        "readiness_level": readiness_level,
        "readiness_label": label,
        "readiness_phase": prog.get("phase"),
        "readiness_percent": prog.get("percent"),
        "subsystem_readiness": subsystems,
        "cockpit_usable": cockpit_usable,
        "trading_ready": trading_ready,
        "partial_ready": readiness_level >= 1 and readiness_level < 4,
        "ready": readiness_level >= 4 and not failed,
    }


def build_readiness_from_system_state() -> dict[str, Any]:
    """Fast readiness from SystemState only — safe on hot paths."""
    from api.readiness_snapshot import resolve_gate_progression

    prog = resolve_gate_progression()
    return build_readiness_bundle(gate_progression=prog, snapshot_warming=True)
