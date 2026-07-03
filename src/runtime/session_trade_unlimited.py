"""Runtime hook — disable trade-count caps; preserve demo order cadence pacing."""

from __future__ import annotations


def inject_session_unlimited_trades() -> None:
    from trading.entry_protection import inject_unlimited_trades_for_session

    inject_unlimited_trades_for_session(clear_counts=True)

    try:
        from runtime.trade_manager import get_dual_core_coordinator

        coord = get_dual_core_coordinator()
        if coord is None:
            return
        try:
            from system.demo_execution_plane import demo_order_cadence_sec, demo_throughput_active

            if demo_throughput_active(coord._cfg):
                cadence = demo_order_cadence_sec(coord._cfg)
                coord.apply_order_cadence_sec(cadence)
                return
        except Exception:
            pass
        coord.apply_unlimited_order_cadence()
    except Exception:
        pass
