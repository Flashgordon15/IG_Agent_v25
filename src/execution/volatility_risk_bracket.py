"""
Volatility-adjusted risk bracket — pure ATR trailing stop math (no I/O).

Dynamic trail distance scales inversely with realised vol expansion so stops
tighten during stress (flash moves). Delegates ratchet logic to
trailing_stop_engine.eval_trailing_stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator, Literal

from execution.trailing_stop_engine import TrailEval, eval_trailing_stop

Side = Literal["BUY", "SELL"]

# Spec-locked bracket constants (anti-curve-fit)
DEFAULT_INITIAL_STOP_ATR_MULT = 2.5
DEFAULT_BASE_TRAIL_ATR_MULT = 2.0
DEFAULT_TRAIL_TRIGGER_ATR_MULT = 0.35
DEFAULT_FLASH_MOVE_ATR_MULT = 2.5
DEFAULT_FLASH_TIGHTEN_ATR_MULT = 1.0
VOL_RATIO_FLOOR = 0.25
VOL_RATIO_CEILING = 4.0
ATR_SMOOTH_ALPHA = 0.18


@dataclass(frozen=True)
class BracketConfig:
    initial_stop_atr_mult: float = DEFAULT_INITIAL_STOP_ATR_MULT
    base_trail_atr_mult: float = DEFAULT_BASE_TRAIL_ATR_MULT
    trail_trigger_atr_mult: float = DEFAULT_TRAIL_TRIGGER_ATR_MULT
    flash_move_atr_mult: float = DEFAULT_FLASH_MOVE_ATR_MULT
    flash_tighten_atr_mult: float = DEFAULT_FLASH_TIGHTEN_ATR_MULT
    atr_smooth_alpha: float = ATR_SMOOTH_ALPHA


@dataclass(frozen=True)
class BracketQuote:
    bid: float
    offer: float
    live_atr: float | None = None


@dataclass
class BracketState:
    side: Side
    entry: float
    stop: float
    target: float
    entry_atr: float
    baseline_atr: float
    live_atr: float
    prev_px: float = 0.0

    @classmethod
    def open_long(
        cls,
        *,
        entry: float,
        entry_atr: float,
        baseline_atr: float | None = None,
        cfg: BracketConfig | None = None,
    ) -> BracketState:
        c = cfg or BracketConfig()
        atr_v = max(entry_atr, 1e-9)
        base = max(baseline_atr or atr_v, 1e-9)
        stop = entry - atr_v * c.initial_stop_atr_mult
        target = entry + atr_v * c.base_trail_atr_mult * 4.0
        return cls(
            side="BUY",
            entry=entry,
            stop=stop,
            target=target,
            entry_atr=atr_v,
            baseline_atr=base,
            live_atr=atr_v,
            prev_px=entry,
        )

    @classmethod
    def open_short(
        cls,
        *,
        entry: float,
        entry_atr: float,
        baseline_atr: float | None = None,
        cfg: BracketConfig | None = None,
    ) -> BracketState:
        c = cfg or BracketConfig()
        atr_v = max(entry_atr, 1e-9)
        base = max(baseline_atr or atr_v, 1e-9)
        stop = entry + atr_v * c.initial_stop_atr_mult
        target = entry - atr_v * c.base_trail_atr_mult * 4.0
        return cls(
            side="SELL",
            entry=entry,
            stop=stop,
            target=target,
            entry_atr=atr_v,
            baseline_atr=base,
            live_atr=atr_v,
            prev_px=entry,
        )


@dataclass(frozen=True)
class BracketUpdate:
    stop: float
    stop_hit: bool
    trail_distance: float
    vol_ratio: float
    trail_atr_mult: float
    mode: str
    exit_px: float


@dataclass(frozen=True)
class BracketSimulation:
    stopped: bool
    stop_tick: int
    final_stop: float
    exit_px: float
    ticks_processed: int
    modes: tuple[str, ...]


def exit_price(side: Side, bid: float, offer: float) -> float:
    return float(bid if side == "BUY" else offer)


def stop_hit(side: Side, stop: float, bid: float, offer: float) -> bool:
    px = exit_price(side, bid, offer)
    s = float(stop)
    if side == "BUY":
        return px <= s
    return px >= s


def volatility_ratio(live_atr: float, baseline_atr: float) -> float:
    base = max(float(baseline_atr), 1e-9)
    raw = max(float(live_atr), 1e-9) / base
    return max(VOL_RATIO_FLOOR, min(VOL_RATIO_CEILING, raw))


def dynamic_trail_atr_multiple(
    vol_ratio: float,
    *,
    base_mult: float = DEFAULT_BASE_TRAIL_ATR_MULT,
) -> float:
    """High vol → tighter trail (smaller ATR multiple)."""
    vr = max(VOL_RATIO_FLOOR, min(VOL_RATIO_CEILING, float(vol_ratio)))
    if vr >= 1.25:
        return max(0.65, float(base_mult) / vr)
    if vr <= 0.80:
        return min(float(base_mult) * 1.20, float(base_mult) / max(vr, 0.80))
    return float(base_mult)


def _profit_pts(side: Side, entry: float, px: float) -> float:
    if side == "BUY":
        return px - entry
    return entry - px


def _adverse_move(side: Side, prev_px: float, px: float) -> float:
    if side == "BUY":
        return max(0.0, prev_px - px)
    return max(0.0, px - prev_px)


def _smooth_live_atr(prev: float, tick_move: float, alpha: float) -> float:
    a = max(0.01, min(0.95, float(alpha)))
    return max(1e-9, prev * (1.0 - a) + abs(tick_move) * a)


def _flash_tighten_stop(
    state: BracketState,
    px: float,
    live_atr: float,
    cfg: BracketConfig,
) -> tuple[float, str]:
    atr_v = max(live_atr, 1e-9)
    tighten = atr_v * cfg.flash_tighten_atr_mult
    if state.side == "BUY":
        candidate = px - tighten
        if candidate > state.stop:
            return candidate, "flash_tighten_long"
        return state.stop, "flash_tighten_long_hold"
    candidate = px + tighten
    if candidate < state.stop:
        return candidate, "flash_tighten_short"
    return state.stop, "flash_tighten_short_hold"


def update_bracket(
    state: BracketState,
    quote: BracketQuote,
    *,
    cfg: BracketConfig | None = None,
) -> BracketUpdate:
    """Advance bracket one quote — ratchet stop, detect flash tighten + stop hit."""
    c = cfg or BracketConfig()
    side = state.side
    bid = float(quote.bid)
    offer = float(quote.offer)
    px = exit_price(side, bid, offer)

    tick_move = abs(px - state.prev_px) if state.prev_px > 0 else 0.0
    if quote.live_atr is not None and quote.live_atr > 0:
        live_atr = max(float(quote.live_atr), 1e-9)
    else:
        live_atr = _smooth_live_atr(state.live_atr, tick_move, c.atr_smooth_alpha)

    vol_ratio = volatility_ratio(live_atr, state.baseline_atr)
    trail_mult = dynamic_trail_atr_multiple(vol_ratio, base_mult=c.base_trail_atr_mult)
    trail_dist = live_atr * trail_mult
    trigger = state.entry_atr * c.trail_trigger_atr_mult
    profit = _profit_pts(side, state.entry, px)

    mode = "hold"
    stop = float(state.stop)

    adverse = _adverse_move(side, state.prev_px or state.entry, px)
    if state.prev_px > 0 and adverse >= live_atr * c.flash_move_atr_mult:
        stop, mode = _flash_tighten_stop(state, px, live_atr, c)

    proposed = eval_trailing_stop(
        TrailEval(
            side=side,
            entry=state.entry,
            stop=stop,
            target=state.target,
            px=px,
            profit=profit,
            trigger=trigger,
            distance=trail_dist,
        )
    )
    if proposed is not None:
        stop = float(proposed)
        mode = "atr_trail"

    hit = stop_hit(side, stop, bid, offer)

    state.stop = stop
    state.live_atr = live_atr
    state.prev_px = px

    return BracketUpdate(
        stop=stop,
        stop_hit=hit,
        trail_distance=trail_dist,
        vol_ratio=vol_ratio,
        trail_atr_mult=trail_mult,
        mode=mode,
        exit_px=px,
    )


def simulate_bracket_path(
    state: BracketState,
    quotes: Iterable[BracketQuote],
    *,
    cfg: BracketConfig | None = None,
) -> BracketSimulation:
    """Replay quotes until stop fires or stream ends."""
    modes: list[str] = []
    stop_tick = -1
    exit_px = state.prev_px or state.entry
    ticks = 0
    final_stop = state.stop
    stopped = False

    for tick in quotes:
        upd = update_bracket(state, tick, cfg=cfg)
        ticks += 1
        modes.append(upd.mode)
        final_stop = upd.stop
        exit_px = upd.exit_px
        if upd.stop_hit:
            stopped = True
            stop_tick = ticks - 1
            break

    return BracketSimulation(
        stopped=stopped,
        stop_tick=stop_tick,
        final_stop=final_stop,
        exit_px=exit_px,
        ticks_processed=ticks,
        modes=tuple(modes),
    )


def quotes_from_mid_stream(
    mids: Iterable[float],
    *,
    spread_half: float = 0.00005,
) -> Iterator[BracketQuote]:
    """Build bid/offer quotes from mid prices (FX-style)."""
    sh = max(float(spread_half), 1e-9)
    for mid in mids:
        m = float(mid)
        yield BracketQuote(bid=round(m - sh, 5), offer=round(m + sh, 5))
