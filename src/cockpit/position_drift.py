"""Broker desync drift detection — local cache vs IG OTC execution values."""

from __future__ import annotations

from typing import Any

SIZE_TOLERANCE = 0.001
PRICE_TOLERANCE_FX = 0.00005
PRICE_TOLERANCE_DEFAULT = 0.05


def _price_tol(epic: str) -> float:
    key = str(epic or "").upper()
    if "EUR" in key or "GBP" in key or "USD" in key:
        return PRICE_TOLERANCE_FX
    return PRICE_TOLERANCE_DEFAULT


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _field_drift(
    field: str,
    local: float | None,
    broker: float | None,
    *,
    tol: float,
) -> dict[str, Any] | None:
    if local is None or broker is None:
        return None
    if abs(local - broker) <= tol:
        return None
    return {
        "field": field,
        "local": local,
        "broker": broker,
        "delta": round(local - broker, 6),
    }


def detect_deal_drift(
    deal_id: str,
    local_row: dict[str, Any],
    broker_row: dict[str, Any],
) -> dict[str, Any] | None:
    epic = str(local_row.get("epic") or broker_row.get("epic") or "")
    ptol = _price_tol(epic)
    mismatches: list[dict[str, Any]] = []

    for field, lkey, bkey in (
        ("size", "size", "size"),
        ("entry", "entry", "entry"),
        ("level", "level", "level"),
    ):
        local_v = _num(local_row.get(lkey) or local_row.get("level"))
        broker_v = _num(broker_row.get(bkey) or broker_row.get("entry"))
        tol = SIZE_TOLERANCE if field == "size" else ptol
        drift = _field_drift(field, local_v, broker_v, tol=tol)
        if drift:
            mismatches.append(drift)

    if not mismatches:
        return None
    return {
        "deal_id": deal_id,
        "dealId": deal_id,
        "epic": epic,
        "drift_detected": True,
        "mismatches": mismatches,
    }


def build_position_drift_report(
    *,
    broker_map: dict[str, dict[str, Any]],
    local_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Memory-only compare — safe on cockpit collector thread."""
    drifts: dict[str, Any] = {}
    for deal_id, broker_row in broker_map.items():
        local_row = local_map.get(deal_id)
        if not local_row:
            continue
        hit = detect_deal_drift(deal_id, local_row, broker_row)
        if hit:
            drifts[deal_id] = hit
    return {
        "any_drift": bool(drifts),
        "count": len(drifts),
        "by_deal": drifts,
    }


def local_positions_from_store() -> dict[str, dict[str, Any]]:
    """Quick SQLite read of open trades for drift compare."""
    out: dict[str, dict[str, Any]] = {}
    try:
        from data.learning_store import LearningStore

        store = LearningStore()
        if not hasattr(store, "conn"):
            return out
        cur = store.conn.execute(
            """
            SELECT ig_deal_id, epic, side, size, entry, level
            FROM trades
            WHERE closed_at IS NULL AND ig_deal_id IS NOT NULL AND TRIM(ig_deal_id) != ''
            """
        )
        for row in cur.fetchall():
            keys = row.keys() if hasattr(row, "keys") else []
            deal_id = str(
                row["ig_deal_id"] if "ig_deal_id" in keys else row[0] if row else ""
            ).strip()
            if not deal_id:
                continue
            out[deal_id] = {
                "dealId": deal_id,
                "epic": row["epic"] if "epic" in keys else "",
                "side": row["side"] if "side" in keys else "",
                "size": row["size"] if "size" in keys else None,
                "entry": row["entry"] if "entry" in keys else row.get("level"),
                "level": row["level"] if "level" in keys else row.get("entry"),
            }
    except Exception:
        pass
    return out
