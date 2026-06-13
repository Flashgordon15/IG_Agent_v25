"""
Spread-to-ATR entry circuit — shared strategy gate and execution-layer shield.

Entry-only: does not apply to exits, stop dispatch, or position management.
"""

from __future__ import annotations

from typing import Any

from data.models import Quote
from system.config import Config

SPREAD_TO_ATR_CIRCUIT_BREAKER_MAX = 0.30
BLOCKED_SPREAD_TO_ATR_CIRCUIT_BREAKER = "BLOCKED_SPREAD_TO_ATR_CIRCUIT_BREAKER"


def atr_from_signal_snapshot(snapshot: dict[str, Any] | None) -> float:
    """14-period 5m ATR in price points from signal snapshot."""
    if not snapshot:
        return 0.0
    last = snapshot.get("last")
    try:
        if last is not None and hasattr(last, "get"):
            return float(last.get("atr", 0) or 0)
    except (TypeError, ValueError):
        pass
    try:
        return float(snapshot.get("atr", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def spread_to_atr_circuit_max(cfg: Config | dict[str, Any], epic: str) -> float:
    """Per-instrument override, config global, soak relaxation, then module default."""
    if hasattr(cfg, "get"):
        raw_max = cfg.get("spread_to_atr_circuit_breaker_max")
        as_dict = cfg.as_dict() if hasattr(cfg, "as_dict") else {}
    else:
        raw_max = (cfg or {}).get("spread_to_atr_circuit_breaker_max")
        as_dict = dict(cfg or {})
    default = float(raw_max or SPREAD_TO_ATR_CIRCUIT_BREAKER_MAX)
    try:
        from trading.instrument_registry import InstrumentRegistry

        inst = InstrumentRegistry(as_dict).get_by_epic(str(epic or ""))
        if inst and inst.get("spread_to_atr_max") is not None:
            default = float(inst["spread_to_atr_max"])
    except (TypeError, ValueError, ImportError):
        pass
    try:
        from system.gate_relaxation import soak_spread_to_atr_max

        return soak_spread_to_atr_max(default)
    except Exception:
        return default


def _quote_spread(quote: Quote | dict[str, Any] | None) -> float:
    if quote is None:
        return 0.0
    if isinstance(quote, dict):
        try:
            return float(quote.get("spread", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(quote.spread)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def spread_to_atr_ratio(
    quote: Quote | dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
) -> tuple[float, float]:
    """Return (spread/atr ratio, atr). Ratio 0.0 when inputs insufficient (no block)."""
    spread = _quote_spread(quote)
    atr = atr_from_signal_snapshot(snapshot)
    if spread <= 0.0 or atr <= 0.0:
        return 0.0, atr
    return spread / atr, atr


def entry_spread_atr_blocked(
    quote: Quote | dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
    cfg: Config | dict[str, Any],
    epic: str,
) -> tuple[bool, float, float]:
    """
    True when live spread/ATR exceeds configured circuit max (entry gate).
    Returns (blocked, ratio, max_ratio).
    """
    ratio, _atr = spread_to_atr_ratio(quote, snapshot)
    spread = _quote_spread(quote)
    max_ratio = spread_to_atr_circuit_max(cfg, epic)
    if spread > 0.0 and _atr > 0.0 and ratio > max_ratio:
        return True, ratio, max_ratio
    return False, ratio, max_ratio


def execution_dispatch_mode_label(uses_simulator: bool) -> str:
    return "SIMULATOR" if uses_simulator else "LIVE"


def execution_spread_atr_block_message(
    mode_name: str,
    ratio: float,
    *,
    max_ratio: float = SPREAD_TO_ATR_CIRCUIT_BREAKER_MAX,
) -> str:
    pct = int(round(max_ratio * 100))
    return (
        f"[RISK ENGINE] ({mode_name}) Execution blocked: Intra-tick spread expansion "
        f"({ratio:.2f}) breached {pct}% ATR limit before dispatch."
    )
