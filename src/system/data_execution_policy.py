"""Data vs execution separation — audit IG REST usage."""

from __future__ import annotations

from system.engine_log import log_engine

_EXECUTION_LABEL_MARKERS = (
    "PLACE",
    "ORDER",
    "CONFIRM",
    "POSITION",
    "WORKINGORDER",
    "DEAL",
    "STOP",
    "LIMIT",
    "MICRO_SCALPER",
    "CLOSE",
    "FLATTEN",
)


def is_execution_rest_label(label: str) -> bool:
    u = str(label or "").upper()
    return any(m in u for m in _EXECUTION_LABEL_MARKERS)


def audit_ig_rest_call(label: str, category: str) -> None:
    """
    Log when IG is used for market data while Yahoo poll is active (non-blocking).
    """
    if category != "market":
        return
    if is_execution_rest_label(label):
        return
    try:
        from system.rest_api_budget import stream_quote_poll_rest_active
        from feeder.yahoo_quote_poller import yahoo_poller_active

        if stream_quote_poll_rest_active() or yahoo_poller_active():
            log_engine(
                f"DataPolicy: IG market REST '{label}' — prefer Yahoo for quotes"
            )
    except Exception:
        pass
