"""
Non-blocking Telegram payloads for [RISK ENGINE] / [RISK SHIELD] events.

Fire-and-forget via TelegramNotifier._send_async — never blocks hub fast path.
"""

from __future__ import annotations

from typing import Any

from system.engine_log import log_engine

_DEDUPE_SEC = 300.0


def _dispatch_telegram(text: str, *, dedupe_key: str | None = None) -> None:
    try:
        from system.telegram_notifier import get_telegram_notifier

        notifier = get_telegram_notifier()
        if notifier is None or not notifier.enabled:
            return
        line = text if text.startswith("⚠️") else f"⚠️ {text}"
        if dedupe_key:
            notifier.send_alert(line, dedupe_key=dedupe_key)
        else:
            notifier.send(line)
    except Exception as e:
        log_engine(f"risk_telemetry telegram failed: {type(e).__name__}: {e}")


def format_spread_atr_entry_block(
    epic: str,
    *,
    spread_pts: float,
    atr_pts: float,
    max_ratio: float,
    ratio: float,
) -> str:
    pct = int(round(float(max_ratio) * 100))
    atr_cap_pts = float(atr_pts) * float(max_ratio)
    return (
        f"[RISK SHIELD] {epic} Entry Blocked. "
        f"Live Spread ({spread_pts:.1f} pts) exceeds {pct}% ATR limit "
        f"({atr_cap_pts:.1f} pts, ratio {ratio:.2f})."
    )


def notify_spread_atr_entry_blocked(
    epic: str,
    quote: Any,
    snapshot: dict[str, Any] | None,
    *,
    max_ratio: float,
    ratio: float,
) -> None:
    from execution.spread_atr_circuit import _quote_spread, atr_from_signal_snapshot
    from system.pnl_math import price_delta_to_ig_points

    spread_price = _quote_spread(quote)
    atr_price = atr_from_signal_snapshot(snapshot)
    spread_pts = price_delta_to_ig_points(str(epic or ""), spread_price)
    atr_pts = price_delta_to_ig_points(str(epic or ""), atr_price)
    text = format_spread_atr_entry_block(
        str(epic or ""),
        spread_pts=spread_pts,
        atr_pts=atr_pts,
        max_ratio=max_ratio,
        ratio=ratio,
    )
    key = f"risk_shield:spread:{str(epic or '').strip()}"
    _dispatch_telegram(text, dedupe_key=key)


def format_stale_decay_trail_tighten(
    epic: str,
    *,
    market: str,
    side: str,
    stop: float,
    compression_pct: float,
    age_minutes: float,
) -> str:
    label = str(market or epic).strip()
    return (
        f"[RISK SHIELD] {label} Stale Decay Trail. "
        f"{side} stop tightened to {stop:.5f} "
        f"({int(round(compression_pct * 100))}% distance compression, "
        f"age {age_minutes:.0f}m)."
    )


def notify_stale_decay_trail_tighten(
    *,
    epic: str,
    market: str,
    side: str,
    trade_id: int,
    stop: float,
    compression_pct: float,
    age_minutes: float,
    min_compression_pct: float = 0.25,
) -> None:
    if float(compression_pct) < float(min_compression_pct):
        return
    text = format_stale_decay_trail_tighten(
        epic,
        market=market,
        side=side,
        stop=stop,
        compression_pct=compression_pct,
        age_minutes=age_minutes,
    )
    key = f"risk_shield:stale:{int(trade_id)}"
    _dispatch_telegram(text, dedupe_key=key)


def reset_risk_telemetry_for_tests() -> None:
    try:
        from system.telegram_notifier import get_telegram_notifier

        n = get_telegram_notifier()
        if n is not None:
            n._alert_last_sent.clear()
    except Exception:
        pass
