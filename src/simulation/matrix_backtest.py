"""
Vectorized matrix backtest — fast permutation evaluation on replay tick slices.

Uses EMA/RSI/ATR signal proxy aligned with SignalEngine thresholds; simulates
stop-loss / take-profit exits from ParamVector reward_multiple and risk_points.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from simulation.historical_replayer import ReplayTick
from simulation.strategy_param_matrix import ParamVector

_FLOAT64 = np.float64
BAR_SEC = 300  # 5-minute bars


@dataclass(frozen=True)
class BacktestMetrics:
    closed_trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float
    max_drawdown: float
    sharpe_ratio: float
    total_pnl: float
    gross_profit: float
    gross_loss: float
    expectancy_per_trade: float = 0.0

    def score(self) -> float:
        """Ranking key — prioritise win rate, expectancy, Sharpe, profit factor."""
        if self.closed_trades < 5:
            return -1e9
        pf = min(self.profit_factor, 10.0)
        dd_pen = max(0.0, 1.0 - self.max_drawdown / 500.0)
        exp_bonus = max(0.0, self.expectancy_per_trade) * 0.5
        return (
            self.win_rate * 100.0
            + self.sharpe_ratio * 5.0
            + pf * 2.0
            + dd_pen * 10.0
            + exp_bonus
        )


def _ticks_to_mids(ticks: list[ReplayTick]) -> tuple[np.ndarray, np.ndarray]:
    if not ticks:
        return np.array([], dtype=_FLOAT64), np.array([], dtype=_FLOAT64)
    mids = np.array([(t.bid + t.offer) * 0.5 for t in ticks], dtype=_FLOAT64)
    ts = np.array([t.timestamp for t in ticks], dtype=_FLOAT64)
    return ts, mids


def _resample_bars(ts: np.ndarray, mids: np.ndarray, bar_sec: int = BAR_SEC) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    if len(mids) == 0:
        empty = np.array([], dtype=_FLOAT64)
        return empty, empty, empty, empty, empty
    t0 = ts[0]
    bucket = ((ts - t0) // bar_sec).astype(int)
    max_b = int(bucket.max())
    opens = np.full(max_b + 1, np.nan, dtype=_FLOAT64)
    highs = np.full(max_b + 1, np.nan, dtype=_FLOAT64)
    lows = np.full(max_b + 1, np.nan, dtype=_FLOAT64)
    closes = np.full(max_b + 1, np.nan, dtype=_FLOAT64)
    bar_ts = np.full(max_b + 1, np.nan, dtype=_FLOAT64)
    for i, b in enumerate(bucket):
        px = mids[i]
        if np.isnan(opens[b]):
            opens[b] = px
            highs[b] = px
            lows[b] = px
            bar_ts[b] = ts[i]
        else:
            highs[b] = max(highs[b], px)
            lows[b] = min(lows[b], px)
        closes[b] = px
    valid = ~np.isnan(closes)
    return bar_ts[valid], opens[valid], highs[valid], lows[valid], closes[valid]


def _ema(values: np.ndarray, span: int) -> np.ndarray:
    n = len(values)
    if n == 0:
        return values
    alpha = _FLOAT64(2.0) / _FLOAT64(span + 1)
    out = np.empty(n, dtype=_FLOAT64)
    out[0] = values[0]
    for i in range(1, n):
        out[i] = alpha * values[i] + (_FLOAT64(1.0) - alpha) * out[i - 1]
    return out


def _rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(close)
    out = np.full(n, 50.0, dtype=_FLOAT64)
    if n < period + 1:
        return out
    delta = np.diff(close, prepend=close[0])
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    avg_g = np.mean(gains[1 : period + 1])
    avg_l = np.mean(losses[1 : period + 1])
    for i in range(period, n):
        if i > period:
            avg_g = (avg_g * (period - 1) + gains[i]) / period
            avg_l = (avg_l * (period - 1) + losses[i]) / period
        rs = avg_g / max(avg_l, 1e-12)
        out[i] = 100.0 - (100.0 / (1.0 + rs))
    return np.clip(out, 15.0, 85.0)


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(close)
    out = np.full(n, 0.0, dtype=_FLOAT64)
    if n < 2:
        return out
    tr = np.maximum(high - low, np.maximum(np.abs(high - np.roll(close, 1)), np.abs(low - np.roll(close, 1))))
    tr[0] = high[0] - low[0]
    for i in range(n):
        start = max(0, i - period + 1)
        out[i] = float(np.mean(tr[start : i + 1]))
    return out


def _pip_scale(epic: str) -> float:
    if "EURUSD" in epic:
        return 10000.0
    if "CFPGOLD" in epic or "GOLD" in epic:
        return 1.0
    return 1.0


def _confidence(fast: float, slow: float, rsi: float, params: ParamVector) -> float:
    trend = 50.0
    if fast > slow:
        trend = 50.0 + min(30.0, abs(fast - slow) / max(slow, 1e-9) * 5000.0)
    elif fast < slow:
        trend = 50.0 - min(30.0, abs(fast - slow) / max(slow, 1e-9) * 5000.0)
    rsi_bias = 0.0
    if rsi >= params.rsi_buy_min:
        rsi_bias = min(20.0, (rsi - params.rsi_buy_min) * 0.5)
    elif rsi <= params.rsi_sell_max:
        rsi_bias = min(20.0, (params.rsi_sell_max - rsi) * 0.5)
    return float(np.clip(trend + rsi_bias, 0.0, 100.0))


def _simulate_epic(
    epic: str,
    ticks: list[ReplayTick],
    params: ParamVector,
    *,
    chaos: bool = False,
    rng: np.random.Generator | None = None,
) -> list[float]:
    ts, mids = _ticks_to_mids(ticks)
    _, opens, highs, lows, closes = _resample_bars(ts, mids)
    if len(closes) < 30:
        return []
    fast = _ema(closes, 9)
    slow = _ema(closes, 21)
    rsi = _rsi(closes, 14)
    atr = _atr(highs, lows, closes, 14)
    scale = _pip_scale(epic)
    local_rng = rng if rng is not None else np.random.default_rng()
    pnls: list[float] = []
    i = 25
    while i < len(closes) - 1:
        conf = _confidence(fast[i], slow[i], rsi[i], params)
        if conf < params.signal_threshold:
            i += 1
            continue
        side = 1 if fast[i] > slow[i] else -1
        if side > 0 and rsi[i] < params.rsi_buy_min:
            i += 1
            continue
        if side < 0 and rsi[i] > params.rsi_sell_max:
            i += 1
            continue
        entry = closes[i]
        stop_dist = max(
            params.risk_points / scale,
            atr[i] * params.atr_volatility_multiplier,
        )
        mom = abs(closes[i] - closes[max(0, i - 3)]) * scale
        if mom < params.momentum_gap_points * 0.1:
            i += 1
            continue
        tp_dist = stop_dist * params.reward_multiple
        if side > 0:
            stop_px = entry - stop_dist
            tp_px = entry + tp_dist
        else:
            stop_px = entry + stop_dist
            tp_px = entry - tp_dist
        pnl = 0.0
        closed = False
        for j in range(i + 1, len(closes)):
            if side > 0:
                if lows[j] <= stop_px:
                    pnl = (stop_px - entry) * scale
                    closed = True
                    i = j + 1
                    break
                if highs[j] >= tp_px:
                    pnl = (tp_px - entry) * scale
                    closed = True
                    i = j + 1
                    break
            else:
                if highs[j] >= stop_px:
                    pnl = (entry - stop_px) * scale
                    closed = True
                    i = j + 1
                    break
                if lows[j] <= tp_px:
                    pnl = (entry - tp_px) * scale
                    closed = True
                    i = j + 1
                    break
        if not closed:
            i += 1
            continue
        if chaos:
            try:
                from simulation.optimization_chaos import slippage_cost_gbp

                slip_pips = float(local_rng.uniform(0.5, 1.5))
                pnl -= slippage_cost_gbp(epic, spread_pips=slip_pips, legs=2, pip_scale=scale)
            except Exception:
                pnl -= scale * 0.0001 * 2.0
        pnls.append(pnl)
    return pnls


def run_matrix_backtest(
    ticks: list[ReplayTick],
    params: ParamVector,
    *,
    chaos: bool | None = None,
    seed: int | None = None,
) -> BacktestMetrics:
    if chaos is None:
        try:
            from simulation.optimization_chaos import chaos_enabled

            chaos = chaos_enabled()
        except Exception:
            chaos = False
    seed_key = seed if seed is not None else hash(
        (
            params.signal_threshold,
            params.rsi_buy_min,
            params.rsi_sell_max,
            params.reward_multiple,
            params.momentum_gap_points,
        )
    ) & 0xFFFFFFFF
    rng = np.random.default_rng(seed_key)
    by_epic: dict[str, list[ReplayTick]] = {}
    for t in ticks:
        by_epic.setdefault(t.epic, []).append(t)
    all_pnls: list[float] = []
    for epic, epic_ticks in by_epic.items():
        all_pnls.extend(_simulate_epic(epic, epic_ticks, params, chaos=chaos, rng=rng))
    if not all_pnls:
        return BacktestMetrics(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    wins = sum(1 for p in all_pnls if p > 0)
    losses = sum(1 for p in all_pnls if p < 0)
    closed = wins + losses
    gross_profit = sum(p for p in all_pnls if p > 0)
    gross_loss = abs(sum(p for p in all_pnls if p < 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    equity = np.cumsum(np.array(all_pnls, dtype=_FLOAT64))
    peak = np.maximum.accumulate(equity)
    dd = float(np.max(peak - equity)) if len(equity) else 0.0
    rets = np.array(all_pnls, dtype=_FLOAT64)
    sharpe = 0.0
    if len(rets) > 1 and float(np.std(rets)) > 1e-9:
        sharpe = float(np.mean(rets) / np.std(rets) * np.sqrt(252 * (86400 / BAR_SEC)))
    win_rate = wins / closed if closed else 0.0
    expectancy = float(sum(all_pnls) / closed) if closed else 0.0
    return BacktestMetrics(
        closed_trades=closed,
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        profit_factor=pf,
        max_drawdown=dd,
        sharpe_ratio=sharpe,
        total_pnl=float(sum(all_pnls)),
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        expectancy_per_trade=expectancy,
    )
