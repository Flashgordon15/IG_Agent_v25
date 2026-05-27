"""
Signal engine — EMA, RSI, momentum, confidence (config-driven).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from data.models import Quote
from signals.indicators import atr, bucket, ema, floor_time, rsi, session_name, vol_regime
from system.config import Config
from system.config_loader import get_config


@dataclass
class SignalResult:
    signal: str
    raw_confidence: float
    adjusted_confidence: float
    learning_delta: float
    setup_key: str
    notes: str
    snapshot: dict[str, Any]


class SignalEngine:
    def __init__(self, config: Config, memory: Any | None = None) -> None:
        self._cfg = config
        self.memory = memory
        self.quotes_by_market: dict[str, list[Quote]] = {}
        self.last_snapshot: dict[str, dict[str, Any]] = {}
        # Track last closed bar per market to avoid duplicate signals on same bar.
        self._last_signal_bar: dict[str, Any] = {}

    @property
    def config(self) -> Config:
        return get_config()

    def add_quote(self, market: str, quote: Quote) -> None:
        self.quotes_by_market.setdefault(market, []).append(quote)
        self.quotes_by_market[market] = self.quotes_by_market[market][-self._cfg.max_live_quotes:]

    def quote_df(self, market: str) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {"time": q.time, "bid": q.bid, "offer": q.offer, "mid": q.mid, "spread": q.spread}
                for q in self.quotes_by_market.get(market, [])
            ]
        )

    def candles(self, df: pd.DataFrame, minutes: int) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame(
                columns=["time", "open", "high", "low", "close", "price", "bid", "offer", "spread"]
            )
        t = df.copy()
        t["bucket"] = t["time"].apply(lambda d: floor_time(d, minutes))
        return (
            t.groupby("bucket")
            .agg(
                time=("bucket", "first"), open=("mid", "first"), high=("mid", "max"),
                low=("mid", "min"), close=("mid", "last"), price=("mid", "last"),
                bid=("bid", "last"), offer=("offer", "last"), spread=("spread", "last"),
            )
            .reset_index(drop=True)
        )

    def add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        cfg = self._cfg
        out = df.copy()
        out["fast_ema"] = ema(out["price"], cfg.fast_ema)
        out["slow_ema"] = ema(out["price"], cfg.slow_ema)
        out["rsi"] = rsi(out["price"], cfg.rsi_period)
        if all(c in out.columns for c in ("high", "low", "close")):
            out["atr"] = atr(out, cfg.atr_period)
        else:
            out["atr"] = 0.0
        return out

    def setup_key(self, signal: str, row_5m: pd.Series, row_15m: pd.Series,
                  atr_series: pd.Series | None = None) -> str:
        if row_5m["fast_ema"] > row_5m["slow_ema"] and row_15m["fast_ema"] >= row_15m["slow_ema"]:
            trend = "bull"
        elif row_5m["fast_ema"] < row_5m["slow_ema"] and row_15m["fast_ema"] <= row_15m["slow_ema"]:
            trend = "bear"
        else:
            trend = "mixed"
        rsi_val = float(row_5m.get("rsi", 50))
        rsi_label = "high" if rsi_val >= 60 else "low" if rsi_val <= 40 else "mid"
        regime = vol_regime(atr_series) if atr_series is not None else "unknown"
        return "|".join(
            [
                signal, trend, session_name(),
                "atr" + bucket(float(row_5m.get("atr", 0)), 30, 200),
                "rsi" + rsi_label,
                "vol" + regime,
            ]
        )

    def learning_adjustment(self, setup_key: str) -> tuple[float, str]:
        cfg = self._cfg
        if not self.memory or not cfg.learning_enabled:
            return 0.0, "learning disabled"

        st = self.memory.setup_stats(setup_key)
        if not st or int(st.get("trades") or 0) < cfg.learning_min_trades_per_setup:
            return 0.0, "learning neutral: not enough setup history"

        wr = float(st.get("winrate") or 0)
        avg = float(st.get("avg_pnl") or 0)

        if avg > 0 and wr >= cfg.adaptive_good_winrate_threshold:
            delta = min(cfg.learning_max_bonus, (wr - 0.55) * 20 + min(avg / 20, 3))
            return delta, f"learning bonus: winrate {wr:.0%}, avg {avg:.1f} pts"

        if avg < 0 or wr < cfg.adaptive_bad_winrate_threshold:
            delta = -min(cfg.learning_max_penalty, (0.50 - wr) * 30 + min(abs(avg) / 10, 6))
            return delta, f"learning penalty: winrate {wr:.0%}, avg {avg:.1f} pts"

        return 0.0, f"learning neutral: winrate {wr:.0%}, avg {avg:.1f} pts"

    def evaluate(self, market: str) -> SignalResult:
        cfg = self._cfg
        df = self.quote_df(market)
        c5 = self.candles(df, 5)
        c15 = self.candles(df, 15)

        # Need at least 4 5m bars so we have 3 confirmed closed bars (iloc[-4..-2])
        # plus one currently-open bar (iloc[-1]) that is excluded from signal logic.
        if len(c5) < 4 or len(c15) < 3:
            self.last_snapshot[market] = {}
            return SignalResult("WAIT", 0.0, 0.0, 0.0, "WAIT|collecting", "Collecting live data", {})

        c5i = self.add_indicators(c5)
        c15i = self.add_indicators(c15)

        # Use confirmed closed bars only — iloc[-2] is the last fully closed 5m bar.
        # iloc[-1] is the currently open bar and is intentionally excluded so that
        # RSI/EMA values cannot shift before the bar closes (incomplete-candle bias).
        last = c5i.iloc[-2]
        prev = c5i.iloc[-3]
        prev2 = c5i.iloc[-4]
        trend15 = c15i.iloc[-2]

        # Suppress duplicate signals that already fired on this closed bar.
        # Include the close price in the key so that re-evaluating after quotes are
        # updated (even with the same timestamps) produces a fresh evaluation.
        close_px_key = round(float(last.get("close", last.get("price", 0))), 0)
        closed_bar_key = (market, str(last.get("time", last.name)), close_px_key)
        if self._last_signal_bar.get(market) == closed_bar_key:
            snap = self.last_snapshot.get(market, {})
            return SignalResult(
                "WAIT", 0.0, 0.0, 0.0, snap.get("setup_key", "WAIT|dup"),
                "Awaiting next closed bar (duplicate suppressed)", snap,
            )

        atr_ok = cfg.min_atr_points <= 0 or float(last.get("atr", 0)) >= cfg.min_atr_points

        # Volatility regime — classify current ATR as low/normal/high for learning context.
        # The regime label is included in setup_key so the adaptive engine naturally
        # learns which regimes produce better outcomes, rather than hard-blocking.
        # max_atr_points > 0 is a hard cap that blocks in extreme volatility only.
        atr_series = c5i["atr"] if "atr" in c5i.columns else None
        current_regime = vol_regime(atr_series) if atr_series is not None else "unknown"
        max_atr_points = float(getattr(cfg, "max_atr_points", 0))
        vol_blocked = False
        vol_block_reason = ""
        if max_atr_points > 0 and float(last.get("atr", 0)) > max_atr_points:
            vol_blocked = True
            vol_block_reason = f"vol regime=high (ATR {float(last.get('atr', 0)):.1f} > max {max_atr_points:.1f})"
        if cfg.vol_regime_filter_enabled and current_regime == "low":
            vol_blocked = True
            vol_block_reason = "vol regime=low (chop filter)"
        two_bull = bool(last["close"] >= last["open"] and prev["close"] >= prev["open"])
        two_bear = bool(last["close"] <= last["open"] and prev["close"] <= prev["open"])

        trend_gap = abs(float(last["fast_ema"]) - float(last["slow_ema"]))
        momentum_bonus = min(10, trend_gap / max(cfg.momentum_gap_points, 1) * 10)
        bull_momentum = momentum_bonus if float(last["fast_ema"]) > float(last["slow_ema"]) else 0.0
        bear_momentum = momentum_bonus if float(last["fast_ema"]) < float(last["slow_ema"]) else 0.0
        spread_score = (
            max(0, min(20, 20 * (1 - float(last["spread"]) / max(cfg.max_spread_points, 0.01))))
            if cfg.max_spread_points > 0 else 0
        )

        rsi_buy_cap = cfg.rsi_buy_max if cfg.rsi_buy_max > cfg.rsi_buy_min else 99.0
        rsi_sell_cap = cfg.rsi_sell_min if cfg.rsi_sell_min < cfg.rsi_sell_max else 0.0
        buy = (
            (30 if trend15["fast_ema"] >= trend15["slow_ema"] and trend15["rsi"] >= 50 else 0)
            + (20 if last["fast_ema"] > last["slow_ema"] else 0)
            + (
                min(20, max(0, min(float(last["rsi"]), rsi_buy_cap) - cfg.rsi_buy_min))
                if last["rsi"] >= cfg.rsi_buy_min else 0
            )
            + (10 if last["price"] >= prev["price"] >= prev2["price"] else 0)
            + spread_score + (10 if two_bull else 0) + bull_momentum
        )
        sell = (
            (30 if trend15["fast_ema"] <= trend15["slow_ema"] and trend15["rsi"] <= 50 else 0)
            + (20 if last["fast_ema"] < last["slow_ema"] else 0)
            + (
                min(20, max(0, cfg.rsi_sell_max - max(float(last["rsi"]), rsi_sell_cap)))
                if last["rsi"] <= cfg.rsi_sell_max else 0
            )
            + (10 if last["price"] <= prev["price"] <= prev2["price"] else 0)
            + spread_score + (10 if two_bear else 0) + bear_momentum
        )

        if vol_blocked:
            buy *= 0.5
            sell *= 0.5
        if not atr_ok:
            buy *= 0.65
            sell *= 0.65
        if float(last["spread"]) > cfg.max_spread_points:
            buy *= 0.50
            sell *= 0.50

        raw_conf = max(buy, sell)
        raw_sig = "BUY" if buy > sell else "SELL" if sell > buy else "WAIT"
        threshold = cfg.signal_threshold
        buy_ok = buy >= threshold
        sell_ok = sell >= threshold

        signal = "WAIT"
        candidate = raw_sig
        if buy_ok and sell_ok:
            candidate = "BUY" if buy >= sell else "SELL"
        elif buy_ok:
            candidate = "BUY"
        elif sell_ok:
            candidate = "SELL"

        if candidate in ("BUY", "SELL"):
            rsi_val = float(last["rsi"])
            rsi_block = ""
            if candidate == "BUY" and cfg.rsi_buy_max > 0 and rsi_val > cfg.rsi_buy_max:
                rsi_block = (
                    f"RSI overbought filter: {rsi_val:.1f} > max {cfg.rsi_buy_max:.0f}"
                )
            elif candidate == "SELL" and cfg.rsi_sell_min > 0 and rsi_val < cfg.rsi_sell_min:
                rsi_block = (
                    f"RSI oversold filter: {rsi_val:.1f} < min {cfg.rsi_sell_min:.0f}"
                )
            if rsi_block:
                setup = self.setup_key(raw_sig, last, trend15, atr_series)
                delta, learn_note = self.learning_adjustment(setup)
                adjusted = max(0, min(99, raw_conf + delta))
                notes = (
                    f"raw={raw_sig}, buy_score={buy:.1f}, sell_score={sell:.1f}, "
                    f"threshold={threshold:.0f}, blocked: {rsi_block}, {learn_note}"
                )
                snapshot = {
                    "last": last,
                    "trend15": trend15,
                    "setup_key": setup,
                    "raw_signal": raw_sig,
                    "raw_confidence": raw_conf,
                    "adjusted_confidence": adjusted,
                    "learning_delta": delta,
                    "buy_score": buy,
                    "sell_score": sell,
                    "rsi_block": rsi_block,
                }
                self.last_snapshot[market] = snapshot
                return SignalResult(
                    "WAIT", float(raw_conf), float(adjusted), float(delta), setup, notes, snapshot
                )

            setup = self.setup_key(candidate, last, trend15, atr_series)
            side_score = buy if candidate == "BUY" else sell
            delta, learn_note = self.learning_adjustment(setup)
            adjusted = max(0, min(99, side_score + delta))
            if adjusted >= threshold:
                signal = candidate
                raw_conf = side_score
        else:
            setup = self.setup_key(raw_sig, last, trend15, atr_series)
            delta, learn_note = self.learning_adjustment(setup)
            adjusted = max(0, min(99, raw_conf + delta))

        vol_note = f", vol_regime={current_regime}" + (f" BLOCKED: {vol_block_reason}" if vol_blocked else "")
        notes = (
            f"raw={raw_sig}, buy_score={buy:.1f}, sell_score={sell:.1f}, "
            f"threshold={threshold:.0f}, adjusted={adjusted:.1f}, "
            f"spread={float(last['spread']):.1f}, atr={float(last.get('atr', 0)):.1f}"
            f"{vol_note}, {learn_note}"
        )
        snapshot = {
            "last": last, "trend15": trend15, "setup_key": setup,
            "raw_signal": raw_sig, "raw_confidence": raw_conf,
            "adjusted_confidence": adjusted, "learning_delta": delta,
            "buy_score": buy, "sell_score": sell,
            "vol_regime": current_regime,
        }
        self.last_snapshot[market] = snapshot

        if cfg.vol_regime_filter_enabled and current_regime == "low" and signal in ("BUY", "SELL"):
            signal = "WAIT"
            notes = f"{notes} | {vol_block_reason}"

        # Record bar key when an actionable signal fires so the next tick for the
        # SAME closed bar at the SAME price level is suppressed (avoids sending
        # duplicate orders between consecutive ticks within the same bar).
        if signal in ("BUY", "SELL"):
            self._last_signal_bar[market] = closed_bar_key
        elif signal == "WAIT":
            # Clear the bar lock when the signal drops to WAIT so a genuine new
            # direction on the same bar time (rare) can still fire.
            self._last_signal_bar.pop(market, None)

        return SignalResult(signal, float(raw_conf), float(adjusted), float(delta), setup, notes, snapshot)
