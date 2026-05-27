"""One-line summary of why the bot is not trading (dashboard / status)."""

from __future__ import annotations

from typing import Any

from system.demo_execution_trace import get_demo_diagnostics_snapshot


def _trim(text: str, limit: int = 140) -> str:
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def why_no_trade_line(
    bot: Any,
    *,
    block_reason: str = "",
    validation_reasons: list[str] | None = None,
    signal_direction: str = "",
    operational_detail: str = "",
) -> str:
    """
    Return a short human-readable line for the Dashboard banner.

    Prefers the most recent execution block over generic WAIT.
    """
    diag = get_demo_diagnostics_snapshot()
    running = bool(getattr(getattr(bot, "state", None), "running", False))

    for candidate in (
        str(block_reason or "").strip(),
        "; ".join(validation_reasons or []),
        str(diag.last_rejection or "").strip(),
        str(getattr(getattr(bot, "_live_gate", None), "last_block_reason", "") or "").strip(),
    ):
        if candidate and candidate.lower() not in ("wait", "none"):
            prefix = "Blocked" if running else "Last block"
            return f"{prefix}: {_trim(candidate)}"

    sig = str(signal_direction or getattr(getattr(bot, "state", None), "last_signal", "") or "").upper()
    if running and sig == "WAIT":
        return "Evaluating — no actionable signal (WAIT)"

    if operational_detail:
        return _trim(operational_detail)

    if not running:
        return "Idle — click Start DEMO Mode when market is open"

    return "Monitoring — gates passed; waiting for signal"
