"""Shared stop/risk resolution — single source of truth for portfolio + SQLite rows."""

from __future__ import annotations

from typing import Any

from system.pnl_math import ig_points_to_price_delta, price_delta_to_ig_points


def instrument_for_epic(epic: str, cfg: Any | None) -> dict[str, Any] | None:
    if cfg is None:
        return None
    try:
        instruments = cfg.get("instruments") or {}
        if isinstance(instruments, dict):
            for inst in instruments.values():
                if isinstance(inst, dict) and str(inst.get("epic") or "") == epic:
                    return inst
    except (TypeError, ValueError, AttributeError):
        pass
    return None


def configured_stop_points(epic: str, cfg: Any | None) -> float:
    inst = instrument_for_epic(epic, cfg)
    if inst:
        for key in ("stop_distance_points", "risk_points"):
            try:
                v = float(inst.get(key) or 0)
                if v > 0:
                    return v
            except (TypeError, ValueError):
                continue
    if cfg is not None:
        try:
            v = float(cfg.get("stop_distance_points") or cfg.get("risk_points") or 0)
            if v > 0:
                return v
        except (TypeError, ValueError, AttributeError):
            pass
    return 40.0


def point_value_for_epic(epic: str, cfg: Any | None) -> float:
    inst = instrument_for_epic(epic, cfg)
    if inst:
        try:
            return float(inst.get("ig_point_value_gbp") or 1.0)
        except (TypeError, ValueError):
            pass
    return 1.0


def stop_price_from_distance(
    *,
    entry: float,
    side: str,
    stop_distance_pts: float,
    epic: str = "",
) -> float:
    dist = ig_points_to_price_delta(
        str(epic or "").strip(), max(0.0, float(stop_distance_pts))
    )
    side_u = str(side or "BUY").upper()
    if side_u == "BUY":
        return float(entry) - dist
    return float(entry) + dist


def resolve_stop_price(
    *,
    entry: float,
    side: str,
    stop_level: float,
    epic: str,
    cfg: Any | None,
) -> float:
    """Absolute stop price for DB storage (never 0, never entry-as-placeholder)."""
    epic_str = str(epic or "").strip()
    entry_f = float(entry or 0)
    if entry_f <= 0:
        return 0.0
    level = float(stop_level or 0)
    max_reasonable = 500.0
    if ig_points_to_price_delta(epic_str, 1.0) < 0.01:
        max_reasonable = 1.0
    if level > 0 and abs(level - entry_f) <= max_reasonable:
        return level
    dist = configured_stop_points(epic_str, cfg)
    return stop_price_from_distance(
        entry=entry_f,
        side=side,
        stop_distance_pts=dist,
        epic=epic_str,
    )


def stop_distance_points(row: Any, *, cfg: Any | None = None) -> float:
    """Stop distance in IG points for a trade row."""
    try:
        entry = float(row["entry"] or 0)
        stop = float(row["stop"] or 0)
    except (TypeError, ValueError, KeyError):
        entry = 0.0
        stop = 0.0
    epic = str(row["epic"] or "").strip()
    if not epic:
        return 0.0
    price_diff = abs(entry - stop) if stop > 0 else 0.0
    max_reasonable = 500.0
    if ig_points_to_price_delta(epic, 1.0) < 0.01:
        max_reasonable = 1.0
    if 0 < price_diff <= max_reasonable:
        return price_delta_to_ig_points(epic, price_diff)
    return configured_stop_points(epic, cfg)


def risk_gbp_from_row(row: Any, *, cfg: Any | None = None) -> float:
    try:
        size = float(row["size"] or 0)
    except (TypeError, ValueError, KeyError):
        return 0.0
    if size <= 0:
        return 0.0
    epic = str(row["epic"] or "").strip()
    if not epic:
        return 0.0
    dist = stop_distance_points(row, cfg=cfg)
    if dist <= 0:
        return 0.0
    return dist * size * point_value_for_epic(epic, cfg)


def infer_epic_from_row(row: Any, cfg: Any | None) -> str:
    """Best-effort epic for legacy IG-import rows missing epic."""
    epic = str(row["epic"] or "").strip() if row is not None else ""
    if epic:
        return epic
    try:
        entry = float(row["entry"] or 0)
    except (TypeError, ValueError, KeyError):
        entry = 0.0
    if cfg is not None:
        for inst in (cfg.get("instruments") or {}).values():
            if not isinstance(inst, dict):
                continue
            ie = str(inst.get("epic") or "").strip()
            if not ie:
                continue
            # Index epics: entry is large price level
            if entry > 1000 and "IX.D." in ie:
                return ie
            if entry < 10 and ie.startswith("CS.D.") and "CFD" in ie:
                return ie
    if entry > 10000:
        return "IX.D.NIKKEI.IFM.IP"
    return ""
