"""
Execution-boundary risk wrappers — spread gate, atomic SL/TP, stop fail-safe, micro-BE.

Does not alter entry signals, indicators, or strategy logic. Applied only at
order placement and open-position management boundaries.
"""

from __future__ import annotations

from typing import Any

from data.models import Quote
from execution.types import TradeSignal
from system.engine_log import log_engine

_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "spread_ma_periods": 20,
    "spread_ma_multiplier": 1.5,
    "spread_min_samples": 5,
    "protection_verify_ms": 200,
    "commission_points_per_side": 0.5,
    "breakeven_buffer_points": 2.0,
    "halt_all_entries_on_stop_fail": False,
    "use_limit_at_touch": False,
}


def protect_settings(cfg: Any | None = None) -> dict[str, Any]:
    if cfg is None:
        from system.config_loader import get_config

        cfg = get_config()
    raw_ep = cfg.get("execution_protect", {}) if hasattr(cfg, "get") else {}
    raw_sc = cfg.get("scalping_framework", {}) if hasattr(cfg, "get") else {}
    if not isinstance(raw_ep, dict):
        raw_ep = {}
    if not isinstance(raw_sc, dict):
        raw_sc = {}
    merged = {**_DEFAULTS, **raw_sc, **raw_ep}
    if "enabled" not in raw_ep and bool(raw_sc.get("enabled")):
        merged["enabled"] = True
    if "use_limit_at_touch" not in raw_ep and bool(raw_sc.get("enabled")):
        merged["use_limit_at_touch"] = True
    if raw_sc.get("halt_entries_on_protection_failure") is True:
        merged["halt_all_entries_on_stop_fail"] = True
    return merged


def is_protect_enabled(cfg: Any | None = None) -> bool:
    return bool(protect_settings(cfg).get("enabled", False))


def current_spread(bid: float, offer: float) -> float:
    try:
        return max(0.0, float(offer) - float(bid))
    except (TypeError, ValueError):
        return 0.0


def check_local_spread(
    epic: str,
    bid: float,
    offer: float,
    cfg: Any | None = None,
) -> tuple[bool, str]:
    """
    Abort when (Ask - Bid) >= 1.5 × 20-period spread MA.
    Low-latency: in-memory deque only, no REST.
    """
    if not is_protect_enabled(cfg):
        return True, ""
    spread = current_spread(bid, offer)
    from execution.scalping.dynamic_spread_filter import get_spread_filter

    ok, msg = get_spread_filter().allows(epic, spread)
    if not ok:
        log_engine(f"EXEC_PROTECT spread abort {epic}: {msg}")
    return ok, msg


def check_signal_spread(signal: TradeSignal, cfg: Any | None = None) -> tuple[bool, str]:
    q = signal.quote
    return check_local_spread(signal.epic, float(q.bid), float(q.offer), cfg=cfg)


def submit_atomic_entry(
    client: Any,
    signal: TradeSignal,
    *,
    size: float,
    stop_distance: float,
    limit_distance: float,
    currency_code: str,
    cfg: Any | None = None,
) -> dict[str, Any]:
    """
    Entry with Stop Loss and Take Profit in the initial broker payload.
    Uses LIMIT-at-touch when configured; otherwise MARKET with distances attached.
    """
    settings = protect_settings(cfg)
    use_limit = bool(settings.get("use_limit_at_touch", False))
    bid = float(signal.quote.bid)
    offer = float(signal.quote.offer)

    if use_limit:
        from execution.scalping.atomic_protect import submit_atomic_limit_entry

        log_engine(
            f"EXEC_PROTECT atomic LIMIT entry {signal.direction} {signal.epic} "
            f"bid={bid:.2f} ask={offer:.2f} stop={stop_distance} limit={limit_distance}"
        )
        return submit_atomic_limit_entry(
            client,
            epic=signal.epic,
            direction=signal.direction,
            size=size,
            bid=bid,
            offer=offer,
            stop_distance=stop_distance,
            limit_distance=limit_distance,
            currency_code=currency_code,
        )

    log_engine(
        f"EXEC_PROTECT atomic MARKET entry {signal.direction} {signal.epic} "
        f"stop={stop_distance} limit={limit_distance} size={size}"
    )
    return client.place_market_order(
        epic=signal.epic,
        direction=signal.direction,
        size=size,
        stop_distance=stop_distance,
        limit_distance=limit_distance,
        currency_code=currency_code,
    )


def verify_stop_fail_safe(
    client: Any,
    *,
    deal_id: str,
    epic: str,
    direction: str,
    size: float,
    stop_distance: float,
    cfg: Any | None = None,
) -> bool:
    """
    If entry filled but Stop Loss is missing/rejected, emergency market-close
    that position only. Does not halt all entries unless configured.
    """
    if not deal_id:
        return False
    settings = protect_settings(cfg)
    from execution.scalping.atomic_protect import verify_stop_or_emergency

    return verify_stop_or_emergency(
        client,
        deal_id=deal_id,
        epic=epic,
        direction=direction,
        size=size,
        stop_distance=stop_distance,
        verify_ms=int(settings.get("protection_verify_ms", 200)),
        halt_all_entries=bool(settings.get("halt_all_entries_on_stop_fail", False)),
    )


def micro_breakeven_trigger(quote: Quote, cfg: Any | None = None) -> float:
    from execution.scalping.breakeven_trail import breakeven_trigger_points

    return breakeven_trigger_points(quote, protect_settings(cfg))


def micro_breakeven_stop_offset(quote: Quote, cfg: Any | None = None) -> float:
    from execution.scalping.breakeven_trail import breakeven_stop_offset

    return breakeven_stop_offset(quote, protect_settings(cfg))
