"""IG execution-time market snapshot when reference quotes come from Yahoo."""

from __future__ import annotations

from typing import Any

from feeder.mock_feed_engine import mock_feed_active
from feeder.pricing_transport import ig_snapshot_at_execution
from system.engine_log import log_engine
from system.market_data_hub import get_market_data_hub


def refresh_ig_execution_snapshot(epic: str, cfg: Any | None = None) -> tuple[bool, str]:
    """
    Optional pre-order IG market snapshot for spread validation.

    Publishes source=ig_execution into the hub without replacing the Yahoo
    reference quote used for signal evaluation on the same tick.
    """
    if not ig_snapshot_at_execution(cfg):
        return True, ""
    if mock_feed_active():
        return True, ""

    try:
        from system.credentials_holder import get_credentials_holder
        from system.ig_rest_session import ensure_shared_authenticated

        holder = get_credentials_holder()
        if holder.credentials is None:
            return True, ""
        rest = ensure_shared_authenticated(holder.credentials)
        from system.rest_api_budget import execution_snapshot_rest_window

        with execution_snapshot_rest_window():
            snap = rest.fetch_market_snapshot(
                str(epic), live=True, budget_priority=True
            )
        bid = float((snap or {}).get("bid") or 0)
        offer = float((snap or {}).get("offer") or 0)
        if bid <= 0 or offer <= 0:
            return False, "ig_execution_snapshot: no bid/offer"
        get_market_data_hub().publish(epic, bid, offer, source="ig_execution")
        return True, ""
    except Exception as exc:
        detail = f"ig_execution_snapshot: {type(exc).__name__}: {exc}"
        log_engine(detail)
        return False, detail
