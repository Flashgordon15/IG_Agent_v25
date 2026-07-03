"""
Single choke-point before IG order POST — tradeability + hard min size + traffic slot.

Every path that reaches place_market_order should pass through guard_order_transmit
(or rely on place_market_order calling it internally).
"""

from __future__ import annotations

from typing import Any

from system.engine_log import log_engine


def guard_order_transmit(
    *,
    epic: str,
    direction: str,
    size: float,
    rest_client: Any,
    cfg: Any | None = None,
    check_traffic_slot: bool = True,
) -> tuple[bool, float, str]:
    """
    Returns (allowed, normalized_size, reason).

    Fail-closed on tradeability and sizing; does not consume traffic slot unless
    caller will immediately place (place_market_order consumes on POST).
    """
    _ = direction
    key = str(epic or "").strip()
    if not key:
        return False, 0.0, "missing_epic"
    if rest_client is None:
        return False, 0.0, "rest_client_unavailable"

    try:
        from execution.broker_tradeability import broker_new_deal_allowed

        ok, reason = broker_new_deal_allowed(rest_client, key, cfg=cfg)
        if not ok:
            return False, 0.0, reason or "market_not_tradeable"
    except Exception as exc:
        return False, 0.0, f"market_status_unavailable:{type(exc).__name__}"

    try:
        from execution.broker_epic_resolver import resolve_order_epic_safe
        from execution.ig_size_validator import resolve_executable_lot_size

        broker_epic = resolve_order_epic_safe(
            rest_client,
            key,
            cfg=cfg,
        )
        lot = resolve_executable_lot_size(
            key,
            float(size),
            "BUY",
            cfg,
            rest_client,
            broker_epic=broker_epic,
        )
        if not lot.ok or float(lot.size) <= 0:
            return False, 0.0, lot.rejection_reason or "invalid_size"
        norm_size = float(lot.size)
    except Exception as exc:
        return False, 0.0, f"size_guard_{type(exc).__name__}"

    if check_traffic_slot:
        try:
            from execution.ig_rest_traffic_governor import positions_otc_transmit_slot_available

            if not positions_otc_transmit_slot_available():
                return False, norm_size, "traffic_governor_wait"
        except Exception:
            pass

    return True, norm_size, ""


def log_transmit_block(*, epic: str, reason: str, source: str = "") -> None:
    if not reason:
        return
    log_engine(
        f"OrderTransmitGuard: blocked epic={epic} reason={reason} source={source or 'unknown'}"
    )
