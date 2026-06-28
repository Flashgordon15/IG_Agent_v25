"""Runtime hook — disable trade-count caps and order cadence for the live session."""

from __future__ import annotations


def inject_session_unlimited_trades() -> None:
    from trading.entry_protection import inject_unlimited_trades_for_session

    inject_unlimited_trades_for_session(clear_counts=True)

    try:
        from runtime.trade_manager import get_dual_core_coordinator

        coord = get_dual_core_coordinator()
        if coord is not None:
            coord.apply_unlimited_order_cadence()
    except Exception:
        pass
