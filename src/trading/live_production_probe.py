"""
One-shot E2E live brokerage replay probe — env `IG_LIVE_PROBE_ALPHA=1`.

Forces WIN_ZONE on the next Gold bare-metal tick, dispatches a micro-lot BUY
to the real IG client, publishes cockpit SHM, and pushes Telegram immediately.
"""

from __future__ import annotations

import os
import threading
from typing import Any

_LIVE_PROBE_LOCK = threading.Lock()
_LIVE_PROBE_CONSUMED = False

LIVE_PROBE_EPIC = "CS.D.CFPGOLD.CFP.IP"
LIVE_PROBE_SIGNATURE = "LIVE_PROBING_ALPHA"
LIVE_PROBE_PAYLOAD: dict[str, Any] = {
    "action": "BUY",
    "epic": LIVE_PROBE_EPIC,
    "size": 0.1,
    "order_type": "MARKET",
    "signature": LIVE_PROBE_SIGNATURE,
}


def live_probe_enabled() -> bool:
    return os.environ.get("IG_LIVE_PROBE_ALPHA", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def try_acquire_live_probe(epic: str) -> bool:
    """One-shot arm — only the first Gold bare-metal tick after boot."""
    global _LIVE_PROBE_CONSUMED
    if not live_probe_enabled():
        return False
    if str(epic or "").strip() != LIVE_PROBE_EPIC:
        return False
    with _LIVE_PROBE_LOCK:
        if _LIVE_PROBE_CONSUMED:
            return False
        _LIVE_PROBE_CONSUMED = True
        try:
            from system.engine_log import log_engine

            log_engine(
                f"LIVE_PROBE_ALPHA armed — next RAM tick forces WIN_ZONE "
                f"epic={LIVE_PROBE_EPIC} signature={LIVE_PROBE_SIGNATURE}"
            )
        except Exception:
            pass
        return True


def emit_live_probe_telemetry(
    *,
    epic: str,
    direction: str,
    entry: float,
    size: float,
    deal_id: str,
    coordinate: int,
    confidence: float,
    latency_us: float,
    success: bool,
) -> None:
    """SHM cockpit fill + fulfillment row + immediate Telegram."""
    status = "OPEN" if success else "REJECT"
    result = "PROBE" if success else "LOSS"

    try:
        from system.unified_fulfillment_cache import record_execution_performance_row

        record_execution_performance_row(
            epic=epic,
            direction=direction,
            result=result,
            confidence=confidence,
            cell_index=coordinate,
            latency_us=latency_us,
            deal_id=deal_id,
            size=size,
            entry=entry,
            exit=entry,
            pnl_gbp=0.0,
            status=status,
        )
    except Exception:
        pass

    try:
        from system.ipc.ring_buffer import publish_live_probe_cockpit

        publish_live_probe_cockpit(
            epic=epic,
            direction=direction,
            entry=entry,
            size=size,
            status=status,
            signature=LIVE_PROBE_SIGNATURE,
        )
    except Exception:
        pass

    try:
        from system.unified_fulfillment_cache import get_fulfillment_payload
        from system.ipc.ring_buffer import publish_cockpit_shm

        publish_cockpit_shm(get_fulfillment_payload())
    except Exception:
        pass

    try:
        from system.telegram_notifier import get_telegram_notifier

        notifier = get_telegram_notifier()
        if notifier is None:
            return
        ok = "PLACED" if success else "REJECTED"
        text = (
            f"🚨 LIVE PROBE [{LIVE_PROBE_SIGNATURE}]\n"
            f"{epic} {direction} @ {entry:.2f} size={size:g}\n"
            f"deal={deal_id or '—'} status={ok} coord={coordinate}"
        )
        notifier.send_now(text)
    except Exception:
        pass
