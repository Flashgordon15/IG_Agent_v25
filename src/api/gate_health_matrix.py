"""Gate-aware /api/health contract — no HTTP 200 until Gate 3 completes."""

from __future__ import annotations

from typing import Any

from system.system_state import GateStatus, get_system_state


def _gate_status(gates: dict[str, Any], gate_id: str) -> str:
    raw = gates.get(gate_id) or {}
    if hasattr(raw, "status"):
        return str(raw.status).lower()
    return str(raw.get("status") or GateStatus.PENDING).lower()


def resolve_gate_health_matrix() -> tuple[int, dict[str, Any]]:
    """
    Map boot gates to the v31 health matrix.

    Returns ``(http_status, payload)`` where payload always includes
    ``status``, ``ready``, and ``gate`` (1 or 2 during boot; omitted when operational).
    """
    snap = get_system_state().snapshot()
    phase = str(snap.get("phase") or "BOOTING").upper()
    gates = snap.get("gates") or {}

    g1 = _gate_status(gates, "G1")
    g2 = _gate_status(gates, "G2")
    g3 = _gate_status(gates, "G3")

    if phase == "FAILED" or any(
        _gate_status(gates, gid) == str(GateStatus.FAILED) for gid in ("G1", "G2", "G3")
    ):
        active_gate = (
            3
            if _gate_status(gates, "G3") == str(GateStatus.FAILED)
            else 2
            if _gate_status(gates, "G2") == str(GateStatus.FAILED)
            else 1
        )
        return (
            503,
            {
                "status": "FAILED",
                "gate": active_gate,
                "ready": False,
                "detail": str(snap.get("error") or snap.get("phase_label") or "boot failed"),
            },
        )

    if g3 == str(GateStatus.COMPLETE):
        return 200, {"status": "OPERATIONAL", "ready": True}

    if g1 == str(GateStatus.COMPLETE):
        return 503, {"status": "HYDRATING", "gate": 2, "ready": False}

    return 503, {"status": "INITIALIZING", "gate": 1, "ready": False}


def build_gate_health_response(*, include_extended: bool = False) -> tuple[int, dict[str, Any]]:
    """Compose gate matrix payload; merge operational telemetry when requested."""
    code, matrix = resolve_gate_health_matrix()
    if code != 200 or not include_extended:
        return code, dict(matrix)

    try:
        from api.agent_health import get_cached_health_status

        extended = get_cached_health_status()
        if isinstance(extended, dict):
            return code, {**extended, **matrix}
    except Exception:
        pass
    return code, dict(matrix)
