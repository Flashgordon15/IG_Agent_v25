"""
Signal engine — EMA, RSI, momentum, confidence (config-driven).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from data.models import Quote
from signals.indicators import (
    atr,
    bucket,
    ema,
    rsi,
    session_name,
    vol_regime,
)
from system.config import Config
from system.config_loader import get_config
from system.paths import data_dir


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
    def __init__(
        self,
        config: Config,
        memory: Any | None = None,
        environment_scorer: Any | None = None,
    ) -> None:
        self._cfg = config
        self.memory = memory
        self._environment_scorer = environment_scorer
        self.quotes_by_market: dict[str, list[Quote]] = {}
        # REST OHLC seed — not trimmed by max_live_quotes (stream ticks evict live buffer only).
        self._ohlc_seed: dict[str, list[Quote]] = {}
        self.last_snapshot: dict[str, dict[str, Any]] = {}
        # Track last closed bar per market to avoid duplicate signals on same bar.
        self._last_signal_bar: dict[str, Any] = {}

    @property
    def config(self) -> Config:
        return get_config()

    def _resolve_market_key(self, market: str) -> str:
        key = str(market or "").strip()
        if not key:
            return key
        if key in self._ohlc_seed or key in self.quotes_by_market:
            return key
        low = key.lower()
        for _store in (self._ohlc_seed, self.quotes_by_market):
            for existing in _store:
                if str(existing).lower() == low:
                    return str(existing)
        return key

    def seed_ohlc_history(
        self,
        market: str,
        quotes: list[Quote],
        *,
        aliases: list[str] | None = None,
    ) -> int:
        """Replace IG OHLC bootstrap quotes for *market* (shared by quote_df / scorers)."""
        ordered = sorted(quotes, key=lambda q: q.time)
        if not ordered:
            return 0
        keys: list[str] = []
        for raw in (market, *(aliases or [])):
            key = str(raw or "").strip()
            if key and key not in keys:
                keys.append(key)
        if not keys:
            return 0
        for key in keys:
            self._ohlc_seed[key] = ordered
        return len(ordered)

    def ohlc_seed_count(self, market: str) -> int:
        key = self._resolve_market_key(market)
        return len(self._ohlc_seed.get(key, []))

    def _quotes_for_market(self, market: str) -> list[Quote]:
        key = self._resolve_market_key(market)
        seed = self._ohlc_seed.get(key, [])
        live = self.quotes_by_market.get(key, [])
        if not seed:
            return list(live)
        if not live:
            return list(seed)
        return sorted(seed + live, key=lambda q: q.time)

    def add_quote(self, market: str, quote: Quote) -> None:
        self.quotes_by_market.setdefault(market, []).append(quote)
        self.quotes_by_market[market] = self.quotes_by_market[market][
            -self._cfg.max_live_quotes :
        ]

    def quote_df(self, market: str) -> pd.DataFrame:
        key = self._resolve_market_key(market)
        return pd.DataFrame(
            [
                {
                    "time": q.time,
                    "bid": q.bid,
                    "offer": q.offer,
                    "mid": q.mid,
                    "spread": q.spread,
                }
                for q in self._quotes_for_market(key)
            ]
        )

    def candle_frames(
        self, market: str, *, quote_df: pd.DataFrame | None = None
    ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """Return (quote_df, 5m, 15m, 60m candles) from seed + live quotes."""
        df = quote_df if quote_df is not None else self.quote_df(market)
        return df, self.candles(df, 5), self.candles(df, 15), self.candles(df, 60)

    def candles(self, df: pd.DataFrame, minutes: int) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame(
                columns=[
                    "time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "price",
                    "bid",
                    "offer",
                    "spread",
                ]
            )
        # Vectorised bucket floor — avoids a Python-level apply() loop.
        bucket = pd.to_datetime(df["time"]).dt.floor(f"{minutes}min")
        grp = df.groupby(bucket, sort=True)
        result = pd.DataFrame(
            {
                "time": grp["time"].first(),
                "open": grp["mid"].first(),
                "high": grp["mid"].max(),
                "low": grp["mid"].min(),
                "close": grp["mid"].last(),
                "price": grp["mid"].last(),
                "bid": grp["bid"].last(),
                "offer": grp["offer"].last(),
                "spread": grp["spread"].last(),
            }
        )
        return result.reset_index(drop=True)

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

    def setup_key(
        self,
        signal: str,
        row_5m: pd.Series,
        row_15m: pd.Series,
        atr_series: pd.Series | None = None,
    ) -> str:
        if (
            row_5m["fast_ema"] > row_5m["slow_ema"]
            and row_15m["fast_ema"] >= row_15m["slow_ema"]
        ):
            trend = "bull"
        elif (
            row_5m["fast_ema"] < row_5m["slow_ema"]
            and row_15m["fast_ema"] <= row_15m["slow_ema"]
        ):
            trend = "bear"
        else:
            trend = "mixed"
        rsi_val = float(row_5m.get("rsi", 50))
        rsi_label = "high" if rsi_val >= 60 else "low" if rsi_val <= 40 else "mid"
        regime = vol_regime(atr_series) if atr_series is not None else "unknown"
        return "|".join(
            [
                signal,
                trend,
                session_name(),
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

        wins = int(st.get("wins") or 0)
        losses = int(st.get("losses") or 0)
        decisive = wins + losses
        # Ignore pure-breakeven setups — no real P&L signal to learn from.
        # These arise from PENDING/imported IG records with entry=exit and are
        # not representative of signal quality.
        if decisive == 0:
            return 0.0, "learning neutral: no decisive trades (breakevens only)"

        wr = wins / decisive
        avg = float(st.get("avg_pnl") or 0)

        if avg > 0 and wr >= cfg.adaptive_good_winrate_threshold:
            delta = min(cfg.learning_max_bonus, (wr - 0.55) * 20 + min(avg / 20, 3))
            return delta, f"learning bonus: winrate {wr:.0%}, avg {avg:.1f} pts"

        if avg < 0 or wr < cfg.adaptive_bad_winrate_threshold:
            delta = -min(
                cfg.learning_max_penalty, (0.50 - wr) * 30 + min(abs(avg) / 10, 6)
            )
            return delta, f"learning penalty: winrate {wr:.0%}, avg {avg:.1f} pts"

        return 0.0, f"learning neutral: winrate {wr:.0%}, avg {avg:.1f} pts"

    def _append_shadow_log(
        self,
        market: str,
        *,
        direction: str,
        raw_score: float,
        adjusted_score: float,
        would_have_fired: bool,
        snapshot: dict[str, Any],
    ) -> None:
        try:
            import json
            from datetime import datetime

            last = snapshot.get("last")
            rsi = 0.0
            atr = 0.0
            if last is not None and hasattr(last, "get"):
                rsi = float(last.get("rsi", 0) or 0)
                atr = float(last.get("atr", 0) or 0)
            fitness = 0.0
            if self._environment_scorer is not None:
                try:
                    fitness = float(self._environment_scorer.last_score().total)
                except Exception:
                    pass
            gate_blocked_at: str | None = None
            if not would_have_fired:
                if not snapshot:
                    gate_blocked_at = "collecting"
                elif float(adjusted_score) < float(self._cfg.signal_threshold):
                    gate_blocked_at = "signal_confidence"
            row = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "market": market,
                "confidence": round(float(adjusted_score), 2),
                "fitness": round(fitness, 2),
                "gate_blocked_at": gate_blocked_at,
                "direction": direction,
                "raw_score": round(float(raw_score), 2),
                "adjusted_score": round(float(adjusted_score), 2),
                "would_have_fired": bool(would_have_fired),
                "rsi": round(rsi, 2),
                "atr": round(atr, 2),
                "session": session_name(),
                "setup_key": str(snapshot.get("setup_key") or ""),
            }
            path = data_dir() / "shadow_log.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            # Rotate when file exceeds 50MB — keep last file as .1
            try:
                if path.exists() and path.stat().st_size > 50 * 1024 * 1024:
                    backup = path.with_suffix(".jsonl.1")
                    if backup.exists():
                        backup.unlink()
                    path.rename(backup)
            except Exception:
                pass
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except Exception:
            pass

    def evaluate(self, market: str) -> SignalResult:
        cfg = self._cfg
        df = self.quote_df(market)
        c5 = self.candles(df, 5)
        c15 = self.candles(df, 15)
        c60 = self.candles(df, 60)

        # Need at least 4 5m bars so we have 3 confirmed closed bars (iloc[-4..-2])
        # plus one currently-open bar (iloc[-1]) that is excluded from signal logic.
        if len(c5) < 4 or len(c15) < 3:
            self.last_snapshot[market] = {}
            empty = SignalResult(
                "WAIT", 0.0, 0.0, 0.0, "WAIT|collecting", "Collecting live data", {}
            )
            self._append_shadow_log(
                market,
                direction="WAIT",
                raw_score=0.0,
                adjusted_score=0.0,
                would_have_fired=False,
                snapshot={},
            )
            return empty

        c5i = self.add_indicators(c5)
        c15i = self.add_indicators(c15)
        c60i = self.add_indicators(c60)

        # Use confirmed closed bars only — iloc[-2] is the last fully closed 5m bar.
        # iloc[-1] is the currently open bar and is intentionally excluded so that
        # RSI/EMA values cannot shift before the bar closes (incomplete-candle bias).
        last = c5i.iloc[-2]
        prev = c5i.iloc[-3]
        prev2 = c5i.iloc[-4]
        trend15 = c15i.iloc[-2]
        trend60 = c60i.iloc[-2] if len(c60i) >= 2 else None
        h1_bearish = trend60 is not None and float(trend60["fast_ema"]) < float(
            trend60["slow_ema"]
        )
        h1_bullish = trend60 is not None and float(trend60["fast_ema"]) > float(
            trend60["slow_ema"]
        )

        # Suppress duplicate signals that already fired on this closed bar.
        # Include the close price in the key so that re-evaluating after quotes are
        # updated (even with the same timestamps) produces a fresh evaluation.
        close_px_key = round(float(last.get("close", last.get("price", 0))), 0)
        closed_bar_key = (market, str(last.get("time", last.name)), close_px_key)
        if self._last_signal_bar.get(market) == closed_bar_key:
            snap = self.last_snapshot.get(market, {})
            raw = float(snap.get("raw_confidence", 0) or 0)
            adjusted = float(snap.get("adjusted_confidence", 0) or 0)
            delta = float(snap.get("learning_delta", 0) or 0)
            return SignalResult(
                "WAIT",
                raw,
                adjusted,
                delta,
                str(snap.get("setup_key") or "WAIT|dup"),
                "Awaiting next closed bar (duplicate suppressed)",
                snap,
            )

        atr_ok = (
            cfg.min_atr_points <= 0 or float(last.get("atr", 0)) >= cfg.min_atr_points
        )

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
        bull_momentum = (
            momentum_bonus if float(last["fast_ema"]) > float(last["slow_ema"]) else 0.0
        )
        bear_momentum = (
            momentum_bonus if float(last["fast_ema"]) < float(last["slow_ema"]) else 0.0
        )
        spread_score = (
            max(
                0,
                min(
                    20,
                    20 * (1 - float(last["spread"]) / max(cfg.max_spread_points, 0.01)),
                ),
            )
            if cfg.max_spread_points > 0
            else 0
        )

        rsi_buy_cap = cfg.rsi_buy_max if cfg.rsi_buy_max > cfg.rsi_buy_min else 99.0
        rsi_sell_cap = cfg.rsi_sell_min if cfg.rsi_sell_min < cfg.rsi_sell_max else 0.0
        buy = (
            (
                30
                if trend15["fast_ema"] >= trend15["slow_ema"] and trend15["rsi"] >= 50
                else 0
            )
            + (20 if last["fast_ema"] > last["slow_ema"] else 0)
            + (
                min(20, max(0, min(float(last["rsi"]), rsi_buy_cap) - cfg.rsi_buy_min))
                if last["rsi"] >= cfg.rsi_buy_min
                else 0
            )
            + (10 if last["price"] >= prev["price"] >= prev2["price"] else 0)
            + spread_score
            + (10 if two_bull else 0)
            + bull_momentum
        )
        sell = (
            (
                30
                if trend15["fast_ema"] <= trend15["slow_ema"] and trend15["rsi"] <= 50
                else 0
            )
            + (20 if last["fast_ema"] < last["slow_ema"] else 0)
            + (
                min(
                    20, max(0, cfg.rsi_sell_max - max(float(last["rsi"]), rsi_sell_cap))
                )
                if last["rsi"] <= cfg.rsi_sell_max
                else 0
            )
            + (10 if last["price"] <= prev["price"] <= prev2["price"] else 0)
            + spread_score
            + (10 if two_bear else 0)
            + bear_momentum
        )

        if vol_blocked:
            buy *= 0.5
            sell *= 0.5
        elif current_regime == "high":
            # Soft penalty in high vol — replay shows ~5pp lower WR vs normal regime.
            buy *= 0.9
            sell *= 0.9
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
            elif (
                candidate == "SELL"
                and cfg.rsi_sell_min > 0
                and rsi_val < cfg.rsi_sell_min
            ):
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
                    "trend60": trend60,
                    "setup_key": setup,
                    "raw_signal": raw_sig,
                    "raw_confidence": raw_conf,
                    "adjusted_confidence": adjusted,
                    "learning_delta": delta,
                    "buy_score": buy,
                    "sell_score": sell,
                    "rsi_block": rsi_block,
                    "h1_bearish": h1_bearish,
                    "h1_bullish": h1_bullish,
                }
                self.last_snapshot[market] = snapshot
                return SignalResult(
                    "WAIT",
                    float(raw_conf),
                    float(adjusted),
                    float(delta),
                    setup,
                    notes,
                    snapshot,
                )

            setup = self.setup_key(candidate, last, trend15, atr_series)
            side_score = buy if candidate == "BUY" else sell
            delta, learn_note = self.learning_adjustment(setup)
            adjusted = max(0, min(99, side_score + delta))
            if (
                cfg.get("enforce_1h_ema_filter", True)
                and candidate == "SELL"
                and (trend60 is None or not h1_bearish)
            ):
                h1_note = (
                    "1h EMA not bearish (fast >= slow)"
                    if trend60 is not None
                    else "1h data collecting"
                )
                notes = (
                    f"raw={raw_sig}, buy_score={buy:.1f}, sell_score={sell:.1f}, "
                    f"threshold={threshold:.0f}, blocked: {h1_note}, {learn_note}"
                )
                snapshot = {
                    "last": last,
                    "trend15": trend15,
                    "trend60": trend60,
                    "setup_key": setup,
                    "raw_signal": raw_sig,
                    "raw_confidence": raw_conf,
                    "adjusted_confidence": adjusted,
                    "learning_delta": delta,
                    "buy_score": buy,
                    "sell_score": sell,
                    "h1_block": h1_note,
                    "h1_bearish": h1_bearish,
                    "h1_bullish": h1_bullish,
                }
                self.last_snapshot[market] = snapshot
                return SignalResult(
                    "WAIT",
                    float(raw_conf),
                    float(adjusted),
                    float(delta),
                    setup,
                    notes,
                    snapshot,
                )
            if adjusted >= threshold:
                signal = candidate
                raw_conf = side_score
        else:
            setup = self.setup_key(raw_sig, last, trend15, atr_series)
            delta, learn_note = self.learning_adjustment(setup)
            adjusted = max(0, min(99, raw_conf + delta))

        vol_note = f", vol_regime={current_regime}" + (
            f" BLOCKED: {vol_block_reason}" if vol_blocked else ""
        )
        notes = (
            f"raw={raw_sig}, buy_score={buy:.1f}, sell_score={sell:.1f}, "
            f"threshold={threshold:.0f}, adjusted={adjusted:.1f}, "
            f"spread={float(last['spread']):.1f}, atr={float(last.get('atr', 0)):.1f}"
            f"{vol_note}, {learn_note}"
        )
        snapshot = {
            "last": last,
            "trend15": trend15,
            "trend60": trend60,
            "setup_key": setup,
            "raw_signal": raw_sig,
            "raw_confidence": raw_conf,
            "adjusted_confidence": adjusted,
            "learning_delta": delta,
            "buy_score": buy,
            "sell_score": sell,
            "vol_regime": current_regime,
            "h1_bearish": h1_bearish,
            "h1_bullish": h1_bullish,
        }
        self.last_snapshot[market] = snapshot

        if (
            cfg.get("enforce_1h_ema_filter", True)
            and signal == "SELL"
            and (trend60 is None or not h1_bearish)
        ):
            signal = "WAIT"
            h1_note = (
                "1h EMA not bearish (fast >= slow)"
                if trend60 is not None
                else "1h data collecting"
            )
            notes = f"{notes} | blocked: {h1_note}"

        if (
            cfg.vol_regime_filter_enabled
            and current_regime == "low"
            and signal in ("BUY", "SELL")
        ):
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

        raw_conf = min(100.0, float(raw_conf))
        adjusted = min(100.0, float(adjusted))
        would_fire = signal in ("BUY", "SELL") and adjusted >= float(threshold)
        self._append_shadow_log(
            market,
            direction=signal,
            raw_score=raw_conf,
            adjusted_score=adjusted,
            would_have_fired=would_fire,
            snapshot=snapshot,
        )
        return SignalResult(
            signal,
            raw_conf,
            adjusted,
            float(delta),
            setup,
            notes,
            snapshot,
        )
