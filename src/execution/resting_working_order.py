"""Resting IG Working Order at historical inside touch (spread elasticity route)."""

from __future__ import annotations

from typing import Any

from system.engine_log import log_engine


def dispatch_resting_working_order(
    rest: Any,
    *,
    epic: str,
    direction: str,
    size: float,
    level: float,
    stop_distance: float,
    limit_distance: float | None = None,
    currency_code: str = "GBP",
) -> dict[str, Any]:
    """
    Place GOOD_TILL_CANCELLED LIMIT working order at ``level``.

    Used when spread elasticity forbids aggressive MARKET — capitalises on
    compression mean-reversion without paying toxic MM premium.

    Hard-capped accounts (Z6BAH4) are refused here: async WO fills bypass the
    in-process mutex/ledger and recreate cascade storms after agent stop.
    """
    account_id = str(getattr(rest, "account_id", "") or "").strip().upper()
    try:
        from execution.order_in_flight_mutex import (
            hard_cap_blocks_entry,
            resolve_account_hard_open_cap,
            try_acquire_order_mutex,
            release_order_mutex,
            mutex_veto_payload,
            note_account_open,
        )
    except Exception:
        hard_cap_blocks_entry = None  # type: ignore

    if hard_cap_blocks_entry is not None and resolve_account_hard_open_cap(account_id) is not None:
        # Un-bypassable: never park resting WOs on hard-capped CFD accounts.
        reason = (
            f"account_hard_cap:{account_id} resting_working_order_blocked "
            f"(async fills bypass mutex/ledger)"
        )
        log_engine(reason)
        return {
            "status": "REJECTED",
            "rejection_reason": reason,
            "account_hard_cap": True,
            "working_order_blocked": True,
        }

    lvl = float(level)
    if lvl <= 0:
        raise ValueError(f"invalid_working_order_level={level}")

    log_engine(
        f"RestingWO: {direction} epic={epic} level={lvl:.2f} size={size} "
        f"(spread elasticity — no MARKET)"
    )

    if hard_cap_blocks_entry is not None:
        cap_blocked, cap_reason = hard_cap_blocks_entry(account_id)
        if cap_blocked:
            return {
                "status": "REJECTED",
                "rejection_reason": cap_reason or "account_hard_cap",
                "account_hard_cap": True,
            }
        if not try_acquire_order_mutex(
            account_id, epic=str(epic), source="resting_working_order"
        ):
            return mutex_veto_payload(
                account_id=account_id, source="resting_working_order"
            )
        try:
            if hasattr(rest, "place_working_order_otc"):
                result = rest.place_working_order_otc(
                    epic=epic,
                    direction=str(direction or "BUY").upper(),
                    size=float(size),
                    level=lvl,
                    stop_distance=float(stop_distance),
                    limit_distance=limit_distance,
                    currency_code=currency_code,
                )
            elif hasattr(rest, "place_limit_entry_atomic"):
                result = rest.place_limit_entry_atomic(
                    epic=epic,
                    direction=str(direction or "BUY").upper(),
                    size=float(size),
                    level=lvl,
                    stop_distance=float(stop_distance),
                    limit_distance=limit_distance,
                    currency_code=currency_code,
                    time_in_force="GOOD_TILL_CANCELLED",
                )
            else:
                raise RuntimeError("rest client cannot place working order")
            # Treat accepted WO as an open reservation until flat/cancel.
            if isinstance(result, dict) and (
                result.get("dealReference") or result.get("dealId") or result.get("status") == "ACCEPTED"
            ):
                # reservation already taken in try_acquire for hard-cap; for
                # uncapped accounts note explicitly.
                if resolve_account_hard_open_cap(account_id) is None:
                    try:
                        note_account_open(account_id, delta=1)
                    except Exception:
                        pass
                release_order_mutex(account_id, reason="wo_accepted", filled=True)
            else:
                release_order_mutex(account_id, reason="wo_rejected", filled=False)
            return result
        except Exception:
            release_order_mutex(account_id, reason="wo_error", filled=False)
            raise

    if hasattr(rest, "place_working_order_otc"):
        return rest.place_working_order_otc(
            epic=epic,
            direction=str(direction or "BUY").upper(),
            size=float(size),
            level=lvl,
            stop_distance=float(stop_distance),
            limit_distance=limit_distance,
            currency_code=currency_code,
        )

    # Fallback: LIMIT entry without exchange FOK (resting intent)
    if hasattr(rest, "place_limit_entry_atomic"):
        return rest.place_limit_entry_atomic(
            epic=epic,
            direction=str(direction or "BUY").upper(),
            size=float(size),
            level=lvl,
            stop_distance=float(stop_distance),
            limit_distance=limit_distance,
            currency_code=currency_code,
            time_in_force="GOOD_TILL_CANCELLED",
        )

    raise RuntimeError("rest client cannot place working order")
