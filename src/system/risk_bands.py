"""v26 risk bands — probe/core/full sizing by confidence (config_v26 risk_bands)."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, Literal

from system.paths import project_root

RiskBand = Literal["probe", "core", "full", "below_floor"]


@lru_cache(maxsize=1)
def _bands_config() -> dict[str, Any]:
    path = project_root() / "config" / "config_v26.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        block = raw.get("risk_bands") or {}
        return block if isinstance(block, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def reset_risk_bands_cache_for_tests() -> None:
    _bands_config.cache_clear()


def bands_enabled() -> bool:
    return bool(_bands_config().get("enabled", False))


def entry_confidence_floor() -> float:
    block = _bands_config()
    try:
        return float(block.get("entry_confidence_floor") or 72.0)
    except (TypeError, ValueError):
        return 72.0


def full_size_min_confidence() -> float:
    block = _bands_config()
    try:
        return float(block.get("full_size_min_confidence") or 85.0)
    except (TypeError, ValueError):
        return 85.0


def probe_max_confidence() -> float:
    block = _bands_config()
    try:
        return float(block.get("probe_max_confidence") or 80.0)
    except (TypeError, ValueError):
        return 80.0


def risk_band_for_confidence(confidence: float) -> RiskBand:
    conf = float(confidence)
    floor = entry_confidence_floor()
    if conf < floor:
        return "below_floor"
    if conf < probe_max_confidence():
        return "probe"
    if conf < full_size_min_confidence():
        return "core"
    return "full"


def probe_risk_target_gbp(confidence: float) -> float:
    """Linear £50–£80 across probe band (72–80% by default)."""
    block = _bands_config()
    lo = float(block.get("probe_risk_gbp_min") or 50.0)
    hi = float(block.get("probe_risk_gbp_max") or 80.0)
    floor = entry_confidence_floor()
    probe_hi = probe_max_confidence()
    conf = float(confidence)
    if conf <= floor:
        return lo
    if conf >= probe_hi:
        return hi
    span = max(probe_hi - floor, 1.0)
    frac = (conf - floor) / span
    return lo + frac * (hi - lo)


def core_size_multiplier() -> float:
    block = _bands_config()
    try:
        return float(block.get("core_size_multiplier") or 0.65)
    except (TypeError, ValueError):
        return 0.65


def apply_risk_band_to_size(
    size: float,
    *,
    confidence: float,
    stop_pts: float,
    point_value_gbp: float,
    epic_risk_cap_gbp: float,
) -> tuple[float, RiskBand, str]:
    """Clip size to probe/core/full risk profile. Returns (size, band, note)."""
    if not bands_enabled():
        return float(size), "full", ""

    band = risk_band_for_confidence(confidence)
    if band == "below_floor":
        return 0.0, band, "below entry floor"

    stop = max(0.0, float(stop_pts))
    pv = max(0.0, float(point_value_gbp))
    if stop <= 0 or pv <= 0:
        return float(size), band, ""

    risk_per_unit = stop * pv
    sized = float(size)

    if band == "probe":
        target_risk = probe_risk_target_gbp(confidence)
        max_size = target_risk / risk_per_unit
        sized = min(sized, max_size)
        note = f"probe band £{target_risk:.0f} risk"
    elif band == "core":
        sized *= core_size_multiplier()
        cap = (
            float(epic_risk_cap_gbp) if epic_risk_cap_gbp > 0 else sized * risk_per_unit
        )
        max_size = cap / risk_per_unit if cap > 0 else sized
        sized = min(sized, max_size)
        note = f"core band ×{core_size_multiplier():.2f}"
    else:
        cap = float(epic_risk_cap_gbp) if epic_risk_cap_gbp > 0 else 0.0
        if cap > 0:
            max_size = cap / risk_per_unit
            sized = min(sized, max_size)
        note = "full band"

    return max(0.0, sized), band, note


def threshold_pass_map(
    confidence: float,
    direction: str,
    *,
    thresholds: tuple[int, ...] = (70, 75, 80, 85),
) -> dict[str, bool]:
    """Feature-store replay: would signal pass at each threshold?"""
    d = str(direction or "").upper()
    active = d in ("BUY", "SELL")
    conf = float(confidence)
    return {f">={t}": active and conf >= float(t) for t in thresholds}


def bands_snapshot() -> dict[str, Any]:
    block = _bands_config()
    return {
        "enabled": bands_enabled(),
        "entry_confidence_floor": entry_confidence_floor(),
        "probe_max_confidence": probe_max_confidence(),
        "full_size_min_confidence": full_size_min_confidence(),
        "probe_risk_gbp_min": block.get("probe_risk_gbp_min"),
        "probe_risk_gbp_max": block.get("probe_risk_gbp_max"),
        "core_size_multiplier": core_size_multiplier(),
    }
