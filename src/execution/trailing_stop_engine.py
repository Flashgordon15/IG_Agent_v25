"""
Ultra-light trailing / breakeven evaluation — pure math, no I/O or logging.

Used on the hot quote path (hub stream + trading loop) for sub-millisecond checks.
"""

from __future__ import annotations

from collections import namedtuple

TrailEval = namedtuple(
    "TrailEval",
    "side entry stop target px profit trigger distance",
)
BreakevenEval = namedtuple(
    "BreakevenEval",
    "side entry stop target px profit trigger offset",
)
QuoteTick = namedtuple("QuoteTick", "bid offer epic market ts")
StaleDecayConfig = namedtuple(
    "StaleDecayConfig",
    (
        "activation_minutes",
        "factor_per_minute",
        "trade_age_minutes",
        "at_mfe",
        "limit_extension_winning",
    ),
    defaults=(15.0, 0.02, 0.0, False, False),
)


def exit_price_for_side(side: str, bid: float, offer: float) -> float:
    """Conservative exit touch for open position marks."""
    return float(bid if str(side or "").upper() == "BUY" else offer)


def _stale_decay_pct(cfg: StaleDecayConfig) -> float:
    age = float(cfg.trade_age_minutes)
    activation = float(cfg.activation_minutes)
    if age <= activation:
        return 0.0
    decay_minutes = age - activation
    return decay_minutes * float(cfg.factor_per_minute)


def _bypass_stale_decay(cfg: StaleDecayConfig) -> bool:
    return bool(cfg.at_mfe or cfg.limit_extension_winning)


def stale_decay_compression_pct(cfg: StaleDecayConfig | None) -> float:
    """Fraction of trail distance removed by stale decay (0.0–1.0)."""
    if cfg is None or _bypass_stale_decay(cfg):
        return 0.0
    return min(1.0, max(0.0, _stale_decay_pct(cfg)))


def _effective_trail_distance(
    distance: float,
    *,
    stale_decay: StaleDecayConfig | None,
) -> float:
    base = float(distance)
    if stale_decay is None or _bypass_stale_decay(stale_decay):
        return base
    decay_pct = _stale_decay_pct(stale_decay)
    if decay_pct <= 0.0:
        return base
    capped = min(max(decay_pct, 0.0), 1.0)
    return base * (1.0 - capped)


def eval_trailing_stop(
    ev: TrailEval,
    *,
    stale_decay: StaleDecayConfig | None = None,
) -> float | None:
    """Return proposed stop when trail should advance; None if unchanged."""
    side = str(ev.side or "").upper()
    stop = float(ev.stop)
    target = float(ev.target)
    px = float(ev.px)
    profit = float(ev.profit)
    trigger = float(ev.trigger)
    distance = _effective_trail_distance(float(ev.distance), stale_decay=stale_decay)
    if distance <= 0:
        return None

    if side == "BUY":
        trail_stop = px - distance
        if trail_stop <= stop or trail_stop >= target:
            return None
        if profit >= trigger:
            return trail_stop
    elif side == "SELL":
        trail_stop = px + distance
        if trail_stop >= stop or trail_stop <= target:
            return None
        if profit >= trigger:
            return trail_stop
    return None


def eval_breakeven_stop(ev: BreakevenEval) -> float | None:
    """Return breakeven lock stop when trigger reached."""
    side = str(ev.side or "").upper()
    entry = float(ev.entry)
    stop = float(ev.stop)
    profit = float(ev.profit)
    trigger = float(ev.trigger)
    offset = float(ev.offset)
    if profit < trigger:
        return None
    if side == "BUY":
        be_stop = entry + offset
        return be_stop if stop < be_stop else None
    if side == "SELL":
        be_stop = entry - offset
        return be_stop if stop > be_stop else None
    return None
