"""
Environment fitness scorer — 4-factor score (0–100) for gate 3.

Section 4.5 Step 3. Reuses SignalEngine candle/indicator pipeline (no re-fetch).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from data.models import Quote
from signals.indicators import session_name
from signals.signal_engine import SignalEngine
from system.config import Config
from system.engine_log import log_engine, record_engine_warning

GATE_PASS_MIN = 40.0
SAFE_DEFAULT_SCORE = 50.0
COLD_START_BAR_CAP = 6
GAP_CAP_MINUTES = 15
GAP_ATR_MULTIPLE = 1.0

FACTOR_ATR_MAX = 30.0
FACTOR_TREND_MAX = 25.0
FACTOR_SESSION_MAX = 20.0
FACTOR_SPREAD_MAX = 25.0


def _linear_down(value: float, start: float, end: float, max_pts: float) -> float:
    if value <= start:
        return max_pts
    if value >= end:
        return 0.0
    return max_pts * (end - value) / (end - start)


def score_atr_factor(current_atr: float, avg_atr_20: float) -> float:
    if avg_atr_20 <= 0 or current_atr <= 0:
        return 0.0
    ratio = current_atr / avg_atr_20
    if ratio <= 0.5 or ratio > 1.8:
        return 0.0
    if ratio <= 1.2:
        return FACTOR_ATR_MAX
    if ratio <= 1.5:
        return _linear_down(ratio, 1.2, 1.5, FACTOR_ATR_MAX)
    return _linear_down(ratio, 1.5, 1.8, FACTOR_ATR_MAX)


def score_trend_factor(row_15m: pd.Series) -> float:
    fast = float(row_15m.get("fast_ema", 0))
    slow = float(row_15m.get("slow_ema", 0))
    rsi_val = float(row_15m.get("rsi", 50))
    ema_bull = fast > slow
    rsi_bull = rsi_val > 50
    if ema_bull and rsi_bull:
        return FACTOR_TREND_MAX
    if ema_bull or rsi_bull:
        return FACTOR_TREND_MAX / 2.0
    return 0.0


def score_session_timing_factor(now: datetime | None = None) -> float:
    """Tokyo-session timing using BST session_name() windows (asia_early = Tokyo night)."""
    now = now or datetime.now()
    name = session_name(now)
    if name != "asia_early":
        return 0.0
    hour = now.hour
    minute = now.minute
    if hour < 2:
        return FACTOR_SESSION_MAX
    if hour == 6 and minute >= 30:
        return 5.0
    if hour < 7:
        return 15.0
    return 0.0


def score_spread_factor(current_spread: float, normal_spread: float) -> float:
    if normal_spread <= 0 or current_spread < 0:
        return 0.0
    ratio = current_spread / normal_spread
    if ratio > 2.0:
        return 0.0
    if ratio <= 1.3:
        return FACTOR_SPREAD_MAX
    return _linear_down(ratio, 1.3, 2.0, FACTOR_SPREAD_MAX)


def regime_label(score: float) -> str:
    if score >= 80:
        return "Excellent"
    if score >= 60:
        return "Good"
    if score >= GATE_PASS_MIN:
        return "Marginal"
    return "WAIT"


@dataclass
class EnvironmentScore:
    total: float = SAFE_DEFAULT_SCORE
    regime: str = "Good"
    factors: dict[str, float] = field(default_factory=dict)
    capped_cold_start: bool = False
    capped_gap_open: bool = False
    gate_passes: bool = True


class EnvironmentScorer:
    """Four-factor environment fitness (0–100)."""

    def __init__(
        self,
        signal_engine: SignalEngine | None = None,
        *,
        config: Config | None = None,
        normal_spread: float | None = None,
        rest_client: Any | None = None,
        epic: str = "",
    ) -> None:
        self._engine = signal_engine
        self._config = config
        self._normal_spread = normal_spread
        self._rest = rest_client
        self._epic = str(epic or "")
        self._last: EnvironmentScore = EnvironmentScore()
        self._session_open_at: dict[str, datetime] = {}
        self._gap_cap_until: dict[str, datetime] = {}
        self._bars_at_session_open: dict[str, int] = {}
        self._sentiment_cache: dict[str, float] = {}
        self._sentiment_detail: dict[str, dict[str, Any]] = {}
        self._primary_market: str = ""
        self._fallback_warned_for_market: set[str] = set()

    def _quote_df(self, market: str, quote_df: pd.DataFrame | None = None) -> pd.DataFrame:
        """Candle source of truth — SignalEngine seed + live quotes (Option A override)."""
        if quote_df is not None:
            return quote_df
        if self._engine is None:
            return pd.DataFrame()
        return self._engine.quote_df(market)

    def _candle_frames(
        self,
        market: str,
        *,
        quote_df: pd.DataFrame | None = None,
    ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        if self._engine is None:
            raise ValueError("signal_engine required")
        if isinstance(self._engine, SignalEngine):
            return self._engine.candle_frames(market, quote_df=quote_df)
        df = self._quote_df(market, quote_df)
        return df, self._engine.candles(df, 5), self._engine.candles(df, 15)

    def fetch_sentiment(self, epic: str | None = None) -> float:
        """Fetch IG client sentiment once per session; cache until reset."""
        key = str(epic or self._epic or "")
        if not key:
            return 50.0
        if key in self._sentiment_cache:
            return self._sentiment_cache[key]
        long_pct = 50.0
        try:
            if self._rest is not None and hasattr(self._rest, "fetch_client_sentiment"):
                long_pct = float(self._rest.fetch_client_sentiment(key))
        except Exception:
            long_pct = 50.0
        long_pct = max(0.0, min(100.0, long_pct))
        self._sentiment_cache[key] = long_pct
        if long_pct > 80.0:
            label = "crowded_long"
            adjustment = -10.0
        elif long_pct < 20.0:
            label = "crowded_short"
            adjustment = -10.0
        else:
            label = "neutral"
            adjustment = 0.0
        self._sentiment_detail[key] = {
            "value": long_pct,
            "label": label,
            "adjustment": adjustment,
        }
        return long_pct

    def get_sentiment_factor(self, market: str) -> dict[str, Any]:
        key = self._epic or market
        return dict(
            self._sentiment_detail.get(
                key,
                {"value": 50.0, "label": "neutral", "adjustment": 0.0},
            )
        )

    def reset_session(
        self,
        market: str,
        *,
        opened_at: datetime | None = None,
        reset_cold_start_baseline: bool = True,
    ) -> None:
        """Call at session open — resets gap cap; optionally cold-start bar baseline."""
        now = opened_at or datetime.now()
        self._session_open_at[market] = now
        self._gap_cap_until.pop(market, None)
        if reset_cold_start_baseline:
            self._bars_at_session_open[market] = self._complete_bar_count(market)
        self.fetch_sentiment(self._epic or market)

    def on_ohlc_bootstrapped(self, market: str) -> None:
        """After OHLC seed lands in SignalEngine.quote_df — refresh cold-start baseline."""
        self._primary_market = str(market or self._primary_market or "")
        self._bars_at_session_open[market] = self._complete_bar_count(market)

    def register_gap_open(self, market: str, *, at: datetime | None = None) -> None:
        """Apply 15-minute score cap after gap > 1.0× ATR (caller detects gap)."""
        now = at or datetime.now()
        self._gap_cap_until[market] = now + timedelta(minutes=GAP_CAP_MINUTES)

    def _complete_bar_count(self, market: str) -> int:
        if self._engine is None:
            return 0
        try:
            _, c5, _ = self._candle_frames(market)
            return max(0, len(c5) - 1)
        except Exception:
            return 0

    def _normal_spread_points(self, cfg: Config, df: pd.DataFrame) -> float:
        if self._normal_spread is not None and self._normal_spread > 0:
            return float(self._normal_spread)
        if not df.empty and "spread" in df.columns:
            recent = df["spread"].tail(60).dropna()
            if len(recent) >= 5:
                return float(recent.median())
        return float(cfg.max_spread_points)

    def _compute_factors(
        self,
        market: str,
        *,
        quote: Quote | None = None,
        quote_df: pd.DataFrame | None = None,
    ) -> tuple[dict[str, float], dict[str, Any]]:
        if self._engine is None:
            raise ValueError("signal_engine required")

        cfg = self._config or self._engine.config
        key = self._primary_market or market
        df, c5, c15 = self._candle_frames(key, quote_df=quote_df)
        if len(c5) < 2 or len(c15) < 2:
            seed_n = (
                int(self._engine.ohlc_seed_count(key))
                if hasattr(self._engine, "ohlc_seed_count")
                else 0
            )
            raise ValueError(
                "insufficient bars "
                f"(market={market!r}, quotes={len(df)}, seed={seed_n}, c5={len(c5)}, c15={len(c15)})"
            )

        c5i = self._engine.add_indicators(c5)
        c15i = self._engine.add_indicators(c15)
        last_5m = c5i.iloc[-2]
        trend_15m = c15i.iloc[-2]

        current_atr = float(last_5m.get("atr", 0))
        atr_series = c5i["atr"].dropna()
        if len(atr_series) >= 20:
            avg_atr_20 = float(atr_series.iloc[-20:].mean())
        elif len(atr_series) > 0:
            avg_atr_20 = float(atr_series.mean())
        else:
            avg_atr_20 = 0.0

        if quote is not None:
            current_spread = float(quote.spread)
        elif not df.empty:
            current_spread = float(df.iloc[-1]["spread"])
        else:
            current_spread = float(last_5m.get("spread", 0))

        normal_spread = self._normal_spread_points(cfg, df)
        now = (
            quote.time
            if quote is not None
            else (
                last_5m.get("time")
                if hasattr(last_5m.get("time"), "hour")
                else datetime.now()
            )
        )
        if not isinstance(now, datetime):
            now = datetime.now()

        factors = {
            "atr": score_atr_factor(current_atr, avg_atr_20),
            "trend": score_trend_factor(trend_15m),
            "session": score_session_timing_factor(now),
            "spread": score_spread_factor(current_spread, normal_spread),
        }
        meta = {
            "current_atr": current_atr,
            "avg_atr_20": avg_atr_20,
            "current_spread": current_spread,
            "normal_spread": normal_spread,
            "complete_bars": max(0, len(c5) - 1),
        }
        return factors, meta

    def score(
        self,
        market: str,
        *,
        quote: Quote | None = None,
        quote_df: pd.DataFrame | None = None,
    ) -> float:
        market_key = str(self._primary_market or market or "")
        try:
            factors, meta = self._compute_factors(
                market, quote=quote, quote_df=quote_df
            )
            self._fallback_warned_for_market.discard(market_key)
            total = sum(factors.values())
            sentiment = self.fetch_sentiment(self._epic or market)
            sent_detail = self.get_sentiment_factor(market)
            adj = float(sent_detail.get("adjustment", 0.0))
            if adj != 0.0:
                total = max(0.0, total + adj)
                factors["sentiment_adj"] = adj
            capped_cold = False
            capped_gap = False

            baseline = self._bars_at_session_open.get(market, 0)
            bars_from_candles = max(0, int(meta["complete_bars"]) - baseline)
            bars_from_clock = 0
            opened = self._session_open_at.get(market)
            if opened is not None:
                probe = quote.time if quote is not None and isinstance(quote.time, datetime) else datetime.now()
                if opened.tzinfo is not None:
                    if probe.tzinfo is None:
                        probe = probe.replace(tzinfo=opened.tzinfo)
                    else:
                        probe = probe.astimezone(opened.tzinfo)
                bars_from_clock = int(
                    max(0.0, (probe - opened).total_seconds()) // (5 * 60)
                )
            bars_since_open = min(
                COLD_START_BAR_CAP,
                max(bars_from_candles, bars_from_clock),
            )
            if bars_since_open < COLD_START_BAR_CAP:
                if total > GATE_PASS_MIN:
                    total = GATE_PASS_MIN
                capped_cold = True

            gap_until = self._gap_cap_until.get(market)
            if gap_until is not None and datetime.now() < gap_until:
                if total > GATE_PASS_MIN:
                    total = GATE_PASS_MIN
                capped_gap = True

            total = max(0.0, min(100.0, total))
            self._last = EnvironmentScore(
                total=total,
                regime=regime_label(total),
                factors=dict(factors),
                capped_cold_start=capped_cold,
                capped_gap_open=capped_gap,
                gate_passes=total >= GATE_PASS_MIN,
            )
            return total
        except Exception as e:
            err = str(e)
            is_warmup = isinstance(e, ValueError) and "insufficient bars" in err.lower()
            if is_warmup:
                if market_key not in self._fallback_warned_for_market:
                    log_engine(
                        f"environment_scorer warmup fallback for {market}: "
                        f"{type(e).__name__}: {e}"
                    )
                    self._fallback_warned_for_market.add(market_key)
            else:
                log_engine(
                    f"environment_scorer score failed for {market}: "
                    f"{type(e).__name__}: {e}"
                )
                record_engine_warning(
                    "env_scorer_fallback",
                    f"{market}: {type(e).__name__}: {e} — using safe default "
                    f"{SAFE_DEFAULT_SCORE:.0f}",
                )
                self._fallback_warned_for_market.add(market_key)
            self._last = EnvironmentScore(
                total=SAFE_DEFAULT_SCORE,
                regime=regime_label(SAFE_DEFAULT_SCORE),
                factors={
                    "atr": SAFE_DEFAULT_SCORE * 0.3,
                    "trend": SAFE_DEFAULT_SCORE * 0.25,
                    "session": SAFE_DEFAULT_SCORE * 0.2,
                    "spread": SAFE_DEFAULT_SCORE * 0.25,
                },
                gate_passes=True,
            )
            return SAFE_DEFAULT_SCORE

    def get_regime(self) -> str:
        try:
            return self._last.regime
        except Exception:
            return regime_label(SAFE_DEFAULT_SCORE)

    def get_factors(self) -> dict[str, Any]:
        try:
            out: dict[str, Any] = dict(self._last.factors)
            out["sentiment"] = self.get_sentiment_factor("")
            return out
        except Exception:
            return {
                "atr": 15.0,
                "trend": 12.5,
                "session": 10.0,
                "spread": 12.5,
                "sentiment": {"value": 50.0, "label": "neutral", "adjustment": 0.0},
            }

    def last_score(self) -> EnvironmentScore:
        return self._last

    def gate_passes(
        self,
        market: str,
        *,
        quote: Quote | None = None,
        quote_df: pd.DataFrame | None = None,
    ) -> bool:
        return self.score(market, quote=quote, quote_df=quote_df) >= GATE_PASS_MIN
