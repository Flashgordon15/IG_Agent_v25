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


def exit_price_for_side(side: str, bid: float, offer: float) -> float:
    """Conservative exit touch for open position marks."""
    return float(bid if str(side or "").upper() == "BUY" else offer)


def eval_trailing_stop(ev: TrailEval) -> float | None:
    """Return proposed stop when trail should advance; None if unchanged."""
    side = str(ev.side or "").upper()
    stop = float(ev.stop)
    target = float(ev.target)
    px = float(ev.px)
    profit = float(ev.profit)
    trigger = float(ev.trigger)
    distance = float(ev.distance)
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
