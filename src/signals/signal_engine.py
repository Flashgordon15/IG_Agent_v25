"""
Signal engine — EMA, RSI, momentum, confidence (config-driven).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from data.models import Quote
from signals.indicators import (
    bucket,
    session_name,
    vol_regime,
)
from system.config import Config
from system.config_loader import get_config
from system.paths import data_dir
from signals.feature_state import FEATURE_STATE_DIM, compile_current_feature_state

H1_EMA_SOFT_PENALTY = 8.0
H1_ML_PENALTY_WAIVER_PROB = 0.75
HIGH_CONFIDENCE_OVERRIDE_THRESHOLD = 40.0
REQUIRE_CLOSED_BAR_G5 = False
_RSI_LOCAL_MIN_BARS = 15
_MAX_MERGED_TICKS = 500
_IG_SNAPSHOT_TIME = re.compile(
    r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})[T:\s](\d{1,2}):(\d{2})(?::(\d{2}))?"
)
_rest_rate_limit_epics: set[str] = set()


_FLOAT64 = np.float64


def _indicator_row_at(df: pd.DataFrame, index: int) -> dict[str, Any]:
    """
    Extract one OHLC/indicator row via float64 column vectors (no .iloc).

    Negative indices follow Python sequence rules relative to frame length.
    """
    n = len(df)
    if n == 0:
        return {}
    i = int(index)
    if i < 0:
        i = n + i
    i = max(0, min(i, n - 1))
    row: dict[str, Any] = {}
    for col in df.columns:
        series = df[col]
        if col in (
            "rsi",
            "atr",
            "fast_ema",
            "slow_ema",
            "close",
            "price",
            "open",
            "high",
            "low",
            "bid",
            "offer",
            "spread",
            "mid",
        ):
            arr = np.asarray(series.to_numpy(dtype=_FLOAT64, copy=False))
            if len(arr) > i and np.isfinite(arr[i]):
                row[col] = float(arr[i])
            else:
                row[col] = 0.0
        else:
            row[col] = series.iat[i]
    return row


def mark_rest_rate_limit_local_fallback(*, epic: str = "", market: str = "") -> None:
    """Flag epic/market for instant local RSI hydration when IG REST is deferred."""
    key = str(epic or market or "").strip()
    if key:
        _rest_rate_limit_epics.add(key)
    try:
        from system.engine_log import log_engine

        log_engine(
            f"RSI hydrate: REST rate-limit — switching to local historical buffer "
            f"for {key or 'market'}"
        )
    except Exception:
        pass


def _parse_historical_bar_time(raw: str) -> datetime:
    s = str(raw or "").strip()
    if not s:
        return datetime.now()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        m = _IG_SNAPSHOT_TIME.match(s)
        if not m:
            return datetime.now()
        y, mo, d, h, mi, sec = m.groups()
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(sec or 0))
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def _historical_repository_paths(epic: str, market: str = "") -> list[Path]:
    """src/data/historical first, then ohlc_cache JSONL for the same epic."""
    from trading.ohlc_cache_paths import ohlc_cache_path

    filename = ohlc_cache_path(epic, market=market).name
    candidates = [
        data_dir() / "historical" / filename,
        data_dir() / "ohlc_cache" / filename,
        ohlc_cache_path(epic, market=market),
    ]
    seen: set[str] = set()
    out: list[Path] = []
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


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
        # Per-market RSI exhaustion reversion monitor (armed when 5m RSI > 90).
        self._exhaustion_monitor_armed: dict[str, bool] = {}
        self._epic_for_market: dict[str, str] = {}
        self._local_hydration_logged: set[str] = set()

    @property
    def config(self) -> Config:
        return get_config()

    def _effective_signal_threshold(self, cfg: Config) -> float:
        threshold = float(cfg.signal_threshold)
        try:
            from execution.ml_training_hooks import get_points_engine

            pe = get_points_engine()
            if pe is not None:
                prot = pe.protected_signal_threshold_floor()
                if prot is not None:
                    threshold = max(threshold, float(prot))
        except Exception:
            pass
        try:
            from system.protective_learning import signal_threshold_floor

            floor = signal_threshold_floor()
            if floor is not None:
                threshold = max(threshold, float(floor))
        except Exception:
            pass
        try:
            from trading.entry_protection import ml_insufficient_data_threshold

            ml_floor = ml_insufficient_data_threshold(cfg)
            if ml_floor is not None:
                threshold = max(threshold, float(ml_floor))
        except Exception:
            pass
        try:
            from system.protective_learning import apply_temporary_test_confidence_floor

            threshold = apply_temporary_test_confidence_floor(threshold)
        except Exception:
            pass
        return threshold

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
        epic_ref = str((aliases or [market] or [""])[0] or market).strip()
        for key in keys:
            if epic_ref:
                self._epic_for_market[key] = epic_ref
        return len(ordered)

    def _resolve_epic_for_market(self, market: str) -> str:
        key = self._resolve_market_key(market)
        if key in self._epic_for_market:
            return self._epic_for_market[key]
        if key in _rest_rate_limit_epics:
            return key
        return key

    def _rsi_min_closed_bars(self, cfg: Config | None = None) -> int:
        active = cfg or self._cfg
        period = max(1, int(getattr(active, "rsi_period", 14) or 14))
        return max(period + 3, _RSI_LOCAL_MIN_BARS // 2, 4)

    def _rolling_seed_cap(self, cfg: Config | None = None) -> int:
        return max(self._rsi_min_closed_bars(cfg) * 24, 200)

    def hydrate_from_local_repository(
        self,
        market: str,
        *,
        epic: str = "",
        min_bars: int | None = None,
    ) -> int:
        """
        Seed RSI lookback from local JSONL (src/data/historical/) — no IG REST.
        Decoupled from startup OHLC bootstrap and hard_rate_cap bursts.
        """
        cfg = self._cfg
        epic_key = str(epic or self._resolve_epic_for_market(market) or market).strip()
        need = int(min_bars if min_bars is not None else self._rsi_min_closed_bars(cfg))
        need = max(need, _RSI_LOCAL_MIN_BARS)
        quotes: list[Quote] = []
        for path in _historical_repository_paths(epic_key, market=market):
            if not path.is_file():
                continue
            try:
                lines = [
                    ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()
                ]
            except OSError:
                continue
            if not lines:
                continue
            tail = lines[-max(need * 3, _RSI_LOCAL_MIN_BARS) :]
            for line in tail:
                try:
                    bar = json.loads(line)
                except json.JSONDecodeError:
                    continue
                high = float(bar.get("h") or bar.get("high") or 0)
                low = float(bar.get("l") or bar.get("low") or 0)
                close = float(bar.get("c") or bar.get("close") or 0)
                if high <= 0 and low <= 0 and close <= 0:
                    continue
                if high <= 0:
                    high = close or low
                if low <= 0:
                    low = close or high
                if close <= 0:
                    close = (high + low) / 2.0
                spread = float(bar.get("spread") or 15.0)
                bid = close - spread / 2.0
                offer = close + spread / 2.0
                quotes.append(
                    Quote(
                        time=_parse_historical_bar_time(
                            bar.get("t") or bar.get("time") or ""
                        ),
                        bid=bid,
                        offer=offer,
                    )
                )
            if quotes:
                break
        if not quotes:
            return 0
        count = self.seed_ohlc_history(
            market,
            quotes,
            aliases=[epic_key] if epic_key else None,
        )
        if count > 0:
            log_key = self._resolve_market_key(market) or market
            if log_key not in self._local_hydration_logged:
                self._local_hydration_logged.add(log_key)
                try:
                    from system.engine_log import log_engine

                    log_engine(
                        f"RSI hydrate: injected {count} local closed bars for "
                        f"{epic_key} (market={market}, no REST)"
                    )
                except Exception:
                    pass
        return count

    def _ensure_rsi_hydrated(self, market: str) -> None:
        """Top-up OHLC seed from local disk cache — never blocks on IG REST."""
        cfg = self._cfg
        min_bars = self._rsi_min_closed_bars(cfg)
        if len(self.candles_for_market(market, 5)) >= min_bars:
            return
        epic = self._resolve_epic_for_market(market)
        self.hydrate_from_local_repository(market, epic=epic, min_bars=min_bars)
        if len(self.candles_for_market(market, 5)) >= min_bars:
            return
        if epic in _rest_rate_limit_epics or self.ohlc_seed_count(market) < min_bars:
            self.hydrate_from_local_repository(market, epic=epic, min_bars=min_bars)

    def add_quote(self, market: str, quote: Quote) -> None:
        """Append streaming tick to live buffer only — seed stays bar-discrete."""
        key = self._resolve_market_key(market)
        self.quotes_by_market.setdefault(key, []).append(quote)
        self.quotes_by_market[key] = self.quotes_by_market[key][
            -self._cfg.max_live_quotes :
        ]
        self._ensure_rsi_hydrated(market)

    def refresh_hud_indicators(self, market: str) -> None:
        """
        Lightweight RSI/ATR refresh for Card B telemetry.

        Decoupled from full ``evaluate()`` / gate stack so HUD metrics stay live
        while the 10s gate-evaluation time-lock is active.
        """
        self._ensure_rsi_hydrated(market)
        try:
            c5 = self.candles_for_market(market, 5)
            if len(c5) < 2:
                return
            c5i = self.add_indicators(c5)
            live_row = _indicator_row_at(c5i, -1)
            closed_row = _indicator_row_at(c5i, -2) if len(c5i) >= 2 else live_row
            rsi_live = float(live_row.get("rsi") or 0)
            rsi_closed = float(closed_row.get("rsi") or 0)
            rsi_val = rsi_live if rsi_live > 0 else rsi_closed
            atr = float(live_row.get("atr") or closed_row.get("atr") or 0)
            if rsi_val <= 0 and atr <= 0:
                return
            snap = dict(self.last_snapshot.get(market) or {})
            last_row: dict[str, Any] = {}
            prev = snap.get("last")
            if isinstance(prev, dict):
                last_row = dict(prev)
            elif prev is not None and hasattr(prev, "get"):
                last_row = {"rsi": prev.get("rsi"), "atr": prev.get("atr")}
            if rsi_val > 0:
                last_row["rsi"] = round(rsi_val, 2)
            if atr > 0:
                last_row["atr"] = round(atr, 2)
            snap["last"] = last_row
            snap["hud_rsi"] = last_row.get("rsi")
            self.last_snapshot[market] = snap
        except Exception:
            pass

    def ohlc_seed_count(self, market: str) -> int:
        key = self._resolve_market_key(market)
        return len(self._ohlc_seed.get(key, []))

    def _quotes_for_market(self, market: str) -> list[Quote]:
        key = self._resolve_market_key(market)
        seed = self._ohlc_seed.get(key, [])
        live = self.quotes_by_market.get(key, [])
        combined = sorted(list(seed) + list(live), key=lambda q: q.time)

        # Enforce structural thinning: if we have more than 500 ticks,
        # sample down to prevent historical array skewing during indicator extraction
        if len(combined) > _MAX_MERGED_TICKS:
            return combined[-_MAX_MERGED_TICKS:]
        return combined

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

    _CANDLE_COLUMNS: tuple[str, ...] = (
        "time",
        "open",
        "high",
        "low",
        "close",
        "price",
        "bid",
        "offer",
        "spread",
    )

    def _empty_candles(self) -> pd.DataFrame:
        return pd.DataFrame(columns=list(self._CANDLE_COLUMNS))

    def _resample_ohlc_from_quotes(
        self, raw_quotes: list[Quote], minutes: int
    ) -> pd.DataFrame:
        """Force raw tick quotes into true time-bucketed OHLC bars before indicators."""
        if not raw_quotes:
            return self._empty_candles()
        times = pd.to_datetime([q.time for q in raw_quotes])
        tick_df = pd.DataFrame(
            {
                "price": [q.mid for q in raw_quotes],
                "bid": [q.bid for q in raw_quotes],
                "offer": [q.offer for q in raw_quotes],
                "spread": [q.spread for q in raw_quotes],
            },
            index=times,
        )
        tick_df = tick_df.sort_index()
        if tick_df.index.has_duplicates:
            tick_df = tick_df[~tick_df.index.duplicated(keep="last")]
        rule = f"{minutes}min"
        ohlc = tick_df["price"].resample(rule).ohlc().ffill()
        ohlc["price"] = ohlc["close"]
        ohlc = ohlc.dropna(subset=["close"])
        if ohlc.empty:
            return self._empty_candles()
        bid = tick_df["bid"].resample(rule).last().reindex(ohlc.index)
        offer = tick_df["offer"].resample(rule).last().reindex(ohlc.index)
        spread = tick_df["spread"].resample(rule).last().reindex(ohlc.index)
        result = ohlc.reset_index().rename(columns={"index": "time"})
        result["bid"] = bid.to_numpy()
        result["offer"] = offer.to_numpy()
        result["spread"] = spread.to_numpy()
        return result[list(self._CANDLE_COLUMNS)]

    def candles_for_market(self, market: str, minutes: int) -> pd.DataFrame:
        """Resample seed + live quotes for *market* into N-minute OHLC bars."""
        return self._resample_ohlc_from_quotes(
            self._quotes_for_market(market), minutes
        )

    def candle_frames(
        self, market: str, *, quote_df: pd.DataFrame | None = None
    ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """Return (quote_df, 5m, 15m, 60m candles) from seed + live quotes."""
        df = quote_df if quote_df is not None else self.quote_df(market)
        raw = self._quotes_for_market(market)
        return (
            df,
            self._resample_ohlc_from_quotes(raw, 5),
            self._resample_ohlc_from_quotes(raw, 15),
            self._resample_ohlc_from_quotes(raw, 60),
        )

    def candles(self, df: pd.DataFrame, minutes: int) -> pd.DataFrame:
        if df.empty:
            return self._empty_candles()
        times = pd.to_datetime(df["time"])
        tick_df = pd.DataFrame(
            {
                "price": df["mid"].astype(float).to_numpy(),
                "bid": df["bid"].astype(float).to_numpy(),
                "offer": df["offer"].astype(float).to_numpy(),
                "spread": df["spread"].astype(float).to_numpy(),
            },
            index=times,
        )
        tick_df = tick_df.sort_index()
        if tick_df.index.has_duplicates:
            tick_df = tick_df[~tick_df.index.duplicated(keep="last")]
        rule = f"{minutes}min"
        ohlc = tick_df["price"].resample(rule).ohlc().ffill()
        ohlc["price"] = ohlc["close"]
        ohlc = ohlc.dropna(subset=["close"])
        if ohlc.empty:
            return self._empty_candles()
        bid = tick_df["bid"].resample(rule).last().reindex(ohlc.index)
        offer = tick_df["offer"].resample(rule).last().reindex(ohlc.index)
        spread = tick_df["spread"].resample(rule).last().reindex(ohlc.index)
        result = ohlc.reset_index().rename(columns={"index": "time"})
        result["bid"] = bid.to_numpy()
        result["offer"] = offer.to_numpy()
        result["spread"] = spread.to_numpy()
        return result[list(self._CANDLE_COLUMNS)]

    def add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Apply EMA/RSI/ATR on pre-resampled OHLC bars.

        Input *df* must already be time-bucketed via ``candles_for_market`` /
        ``_resample_ohlc_from_quotes`` — no raw tick math here.
        """
        cfg = self._cfg
        if df.empty:
            return df
        out = df.copy()
        period = max(1, int(getattr(cfg, "rsi_period", 14) or 14))
        min_bars = period + 1
        if len(out) < min_bars:
            out["rsi"] = 50.0
            out["fast_ema"] = out["price"]
            out["slow_ema"] = out["price"]
            out["atr"] = 0.0
        else:
            from signals.indicators import apply_indicators_frame

            out = apply_indicators_frame(
                out,
                fast_span=int(cfg.fast_ema),
                slow_span=int(cfg.slow_ema),
                rsi_period=period,
                atr_period=int(cfg.atr_period),
            )
        return out

    @staticmethod
    def _signal_bar_indices(frame_len: int) -> tuple[int, int, int]:
        """
        Return (last, prev, prev2) iloc indices.

        Live-open bar mode when ``REQUIRE_CLOSED_BAR_G5`` is False (Wall Street path).
        """
        if frame_len < 2:
            return 0, 0, 0
        if REQUIRE_CLOSED_BAR_G5:
            if frame_len < 4:
                return min(frame_len - 1, 1), max(0, frame_len - 2), max(0, frame_len - 3)
            return -2, -3, -4
        if frame_len < 3:
            return -1, max(-frame_len, -2), max(-frame_len, -2)
        return -1, -2, -3

    @staticmethod
    def _trend_bar_index(frame_len: int) -> int:
        if frame_len < 2:
            return 0
        return -2 if REQUIRE_CLOSED_BAR_G5 else -1

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

    def _evaluate_exhaustion_reversion(
        self,
        market: str,
        *,
        rsi_5m: float,
    ) -> tuple[bool, float, dict[str, Any]]:
        """Arm on 5m RSI extreme; fire SELL when 1m RSI rolls under 88 with bearish tick."""
        from system.protective_learning import (
            exhaustion_edge_score_boost,
            exhaustion_reversion_enabled,
            exhaustion_rsi_arm_threshold,
            exhaustion_rsi_trigger_threshold,
            log_exhaustion_reversion_trigger,
        )

        meta: dict[str, Any] = {
            "exhaustion_monitor_armed": bool(
                self._exhaustion_monitor_armed.get(market)
            ),
            "exhaustion_triggered": False,
            "exhaustion_rsi_5m": round(float(rsi_5m), 2),
        }
        if not exhaustion_reversion_enabled():
            return False, 0.0, meta

        arm_th = exhaustion_rsi_arm_threshold()
        trigger_th = exhaustion_rsi_trigger_threshold()
        if float(rsi_5m) > arm_th:
            self._exhaustion_monitor_armed[market] = True
        elif float(rsi_5m) < trigger_th:
            self._exhaustion_monitor_armed.pop(market, None)

        meta["exhaustion_monitor_armed"] = bool(
            self._exhaustion_monitor_armed.get(market)
        )
        if not self._exhaustion_monitor_armed.get(market):
            return False, 0.0, meta

        c1 = self.candles_for_market(market, 1)
        if len(c1) < 2:
            return False, 0.0, meta

        c1i = self.add_indicators(c1)
        bar_i, prev_i, _ = self._signal_bar_indices(len(c1i))
        bar_1m = _indicator_row_at(c1i, bar_i)
        prev_1m = _indicator_row_at(c1i, prev_i) if len(c1i) >= abs(prev_i) else None
        rsi_1m = float(bar_1m.get("rsi", 50) or 50)
        close_1m = float(bar_1m.get("close", bar_1m.get("price", 0)) or 0)
        open_1m = float(bar_1m.get("open", close_1m) or close_1m)
        bearish_tick = close_1m < open_1m
        if prev_1m is not None:
            prev_close = float(
                prev_1m.get("close", prev_1m.get("price", 0)) or 0
            )
            bearish_tick = bearish_tick or close_1m < prev_close

        meta["exhaustion_rsi_1m"] = round(rsi_1m, 2)
        meta["exhaustion_bearish_tick"] = bool(bearish_tick)
        if rsi_1m >= trigger_th or not bearish_tick:
            return False, 0.0, meta

        boost = exhaustion_edge_score_boost()
        self._exhaustion_monitor_armed.pop(market, None)
        meta["exhaustion_triggered"] = True
        meta["exhaustion_monitor_armed"] = False
        meta["exhaustion_edge_boost"] = boost
        log_exhaustion_reversion_trigger()
        return True, boost, meta

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

    @staticmethod
    def _peak_signal_score(
        *,
        adjusted: float,
        raw_conf: float,
        buy: float,
        sell: float,
    ) -> float:
        return max(
            float(adjusted),
            float(raw_conf),
            float(buy),
            float(sell),
        )

    def _high_confidence_override_clear(
        self,
        *,
        direction: str,
        peak_score: float,
        threshold: float,
    ) -> bool:
        """Bypass secondary WAIT rules when structural confidence clears the floor."""
        if direction not in ("BUY", "SELL"):
            return False
        if peak_score < HIGH_CONFIDENCE_OVERRIDE_THRESHOLD:
            return False
        return peak_score >= float(threshold)

    def apply_dispatch_promotion(self, market: str, sig: SignalResult) -> SignalResult:
        """
        Sync promoted BUY/SELL from trading loop into engine snapshot state.

        Prevents HUD / shadow consumers from reading stale WAIT after dispatch promotion.
        """
        direction = str(sig.signal or "").strip().upper()
        if direction not in ("BUY", "SELL"):
            return sig
        key = self._resolve_market_key(market)
        snap = dict(self.last_snapshot.get(key) or {})
        inner = dict(sig.snapshot or {})
        raw = str(inner.get("raw_signal") or direction).strip().upper()
        snap["raw_signal"] = raw
        snap["dispatch_signal"] = direction
        snap["dispatch_promoted"] = True
        snap["adjusted_confidence"] = float(sig.adjusted_confidence)
        snap["raw_confidence"] = float(sig.raw_confidence)
        for field in ("buy_score", "sell_score", "last", "hud_rsi"):
            if field in inner:
                snap[field] = inner[field]
        self.last_snapshot[key] = snap
        return sig

    def _log_evaluation_shadow(
        self,
        market: str,
        result: SignalResult,
        *,
        threshold: float | None = None,
    ) -> None:
        snap = result.snapshot or {}
        th = (
            float(threshold)
            if threshold is not None
            else float(self._effective_signal_threshold(self._cfg))
        )
        would_fire = (
            result.signal in ("BUY", "SELL") and float(result.adjusted_confidence) >= th
        )
        self._append_shadow_log(
            market,
            direction=result.signal,
            raw_score=float(result.raw_confidence),
            adjusted_score=float(result.adjusted_confidence),
            would_have_fired=would_fire,
            snapshot=snap,
        )

    def evaluate(self, market: str) -> SignalResult:
        cfg = self._cfg
        self._ensure_rsi_hydrated(market)
        from trading.strictness_resolver import resolve_strictness

        _strict = resolve_strictness(cfg, signal_engine=self, market=market)
        from system.protective_learning import apply_temporary_test_rsi_buy_max

        rsi_buy_max = apply_temporary_test_rsi_buy_max(float(_strict.rsi_buy_max))
        rsi_sell_min = float(_strict.rsi_sell_min)
        c5 = self.candles_for_market(market, 5)
        c15 = self.candles_for_market(market, 15)
        c60 = self.candles_for_market(market, 60)

        # Need at least 4 5m bars so we have 3 confirmed closed bars (iloc[-4..-2])
        # plus one currently-open bar (iloc[-1]) that is excluded from signal logic.
        if len(c5) < 4 or len(c15) < 3:
            epic = self._resolve_epic_for_market(market)
            if epic in _rest_rate_limit_epics or self.ohlc_seed_count(market) < 4:
                self.hydrate_from_local_repository(market, epic=epic)
                c5 = self.candles_for_market(market, 5)
                c15 = self.candles_for_market(market, 15)
                c60 = self.candles_for_market(market, 60)
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

        last_i, prev_i, prev2_i = self._signal_bar_indices(len(c5i))
        last = _indicator_row_at(c5i, last_i)
        prev = _indicator_row_at(c5i, prev_i)
        prev2 = _indicator_row_at(c5i, prev2_i)
        trend15 = _indicator_row_at(c15i, self._trend_bar_index(len(c15i)))
        trend60 = (
            _indicator_row_at(c60i, self._trend_bar_index(len(c60i)))
            if len(c60i) >= 2
            else None
        )
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
        closed_bar_key = (market, str(last.get("time", "")), close_px_key)
        if REQUIRE_CLOSED_BAR_G5 and self._last_signal_bar.get(market) == closed_bar_key:
            snap = self.last_snapshot.get(market, {})
            raw = float(snap.get("raw_confidence", 0) or 0)
            adjusted = float(snap.get("adjusted_confidence", 0) or 0)
            delta = float(snap.get("learning_delta", 0) or 0)
            result = SignalResult(
                "WAIT",
                raw,
                adjusted,
                delta,
                str(snap.get("setup_key") or "WAIT|dup"),
                "Awaiting next closed bar (duplicate suppressed)",
                snap,
            )
            self._log_evaluation_shadow(market, result)
            return result

        try:
            from trading.entry_protection import (
                check_daily_trade_cap,
                check_reentry_cooldown,
                check_session_blackout,
                resolve_epic_for_market,
            )
            from risk.economic_calendar import check_economic_calendar_block

            epic = resolve_epic_for_market(market, cfg)
            protection_blocks: list[tuple[bool, str, str]] = [
                (
                    *check_session_blackout(epic, cfg, market=market),
                    "session_blackout",
                ),
                (
                    *check_economic_calendar_block(
                        epic, cfg, market=market
                    ),
                    "economic_calendar",
                ),
                (
                    *check_reentry_cooldown(epic, cfg, market=market),
                    "reentry_cooldown",
                ),
                (
                    *check_daily_trade_cap(epic, cfg, market=market),
                    "session_trade_cap",
                ),
            ]
            for is_blocked, reason, block_key in protection_blocks:
                if not is_blocked:
                    continue
                blocked_result = SignalResult(
                    "WAIT",
                    0.0,
                    0.0,
                    0.0,
                    f"WAIT|{block_key}",
                    f"Entry blocked: {reason}",
                    {"entry_protection": block_key, "block_reason": reason},
                )
                self._append_shadow_log(
                    market,
                    direction="WAIT",
                    raw_score=0.0,
                    adjusted_score=0.0,
                    would_have_fired=False,
                    snapshot=blocked_result.snapshot,
                )
                return blocked_result
        except Exception:
            pass

        atr_ok = True
        try:
            from system.agent_execution_mode import demo_operational_floors_active

            min_atr = (
                0.0
                if demo_operational_floors_active()
                else float(getattr(cfg, "min_atr_points", 0) or 0)
            )
        except Exception:
            min_atr = float(getattr(cfg, "min_atr_points", 0) or 0)
        if min_atr > 0:
            atr_ok = float(last.get("atr", 0)) >= min_atr

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

        rsi_buy_cap = rsi_buy_max if rsi_buy_max > cfg.rsi_buy_min else 99.0
        rsi_sell_cap = rsi_sell_min if rsi_sell_min < cfg.rsi_sell_max else 0.0
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

        if vol_blocked and max(buy, sell) < HIGH_CONFIDENCE_OVERRIDE_THRESHOLD:
            buy *= 0.5
            sell *= 0.5
        elif current_regime == "high" and max(buy, sell) < HIGH_CONFIDENCE_OVERRIDE_THRESHOLD:
            # Soft penalty in high vol — replay shows ~5pp lower WR vs normal regime.
            buy *= 0.9
            sell *= 0.9
        if not atr_ok:
            buy *= 0.65
            sell *= 0.65
        if float(last["spread"]) > cfg.max_spread_points:
            buy *= 0.50
            sell *= 0.50

        exhaustion_triggered, exhaustion_boost, exhaustion_meta = (
            self._evaluate_exhaustion_reversion(
                market,
                rsi_5m=float(last.get("rsi", 50) or 50),
            )
        )
        if exhaustion_triggered:
            sell = min(99.0, float(sell) + float(exhaustion_boost))

        raw_conf = max(buy, sell)
        raw_sig = "BUY" if buy > sell else "SELL" if sell > buy else "WAIT"
        if exhaustion_triggered:
            raw_sig = "SELL"
            raw_conf = float(sell)
        threshold = self._effective_signal_threshold(cfg)
        buy_ok = buy >= threshold and not exhaustion_triggered
        sell_ok = sell >= threshold

        signal = "WAIT"
        candidate = raw_sig
        h1_penalty = 0.0
        h1_note = ""
        regime_note = ""
        if exhaustion_triggered and sell_ok:
            candidate = "SELL"
        elif buy_ok and sell_ok:
            candidate = "BUY" if buy >= sell else "SELL"
        elif buy_ok:
            candidate = "BUY"
        elif sell_ok:
            candidate = "SELL"

        if candidate in ("BUY", "SELL"):
            rsi_val = float(last["rsi"])
            effective_rsi_max = rsi_buy_max
            try:
                from trading.entry_protection import resolve_epic_for_market
                from intelligence.microstructure import effective_entry_rsi_ceiling

                _epic = resolve_epic_for_market(market, cfg)
                effective_rsi_max = effective_entry_rsi_ceiling(
                    _epic, base_ceiling=rsi_buy_max
                )
            except Exception:
                effective_rsi_max = rsi_buy_max
            rsi_block = ""
            try:
                from system.protective_learning import (
                    log_temporary_test_rsi_relaxation_once,
                    temporary_test_gate_active,
                )

                if temporary_test_gate_active() and candidate == "BUY":
                    log_temporary_test_rsi_relaxation_once()
            except Exception:
                pass
            if (
                exhaustion_triggered
                and candidate == "SELL"
            ):
                # Bypass production 85 RSI long ceiling — structural exhaustion short.
                rsi_block = ""
            elif candidate == "BUY" and effective_rsi_max > 0 and rsi_val > effective_rsi_max:
                rsi_block = (
                    f"RSI overbought filter: {rsi_val:.1f} > max {effective_rsi_max:.0f}"
                )
            elif candidate == "SELL" and rsi_sell_min > 0 and rsi_val < rsi_sell_min:
                rsi_block = (
                    f"RSI oversold filter: {rsi_val:.1f} < min {rsi_sell_min:.0f}"
                )
            peak_score = self._peak_signal_score(
                adjusted=0.0,
                raw_conf=float(raw_conf),
                buy=float(buy),
                sell=float(sell),
            )
            if rsi_block and self._high_confidence_override_clear(
                direction=candidate,
                peak_score=peak_score,
                threshold=threshold,
            ):
                rsi_block = ""
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
                result = SignalResult(
                    "WAIT",
                    float(raw_conf),
                    float(adjusted),
                    float(delta),
                    setup,
                    notes,
                    snapshot,
                )
                self._log_evaluation_shadow(market, result, threshold=threshold)
                return result

            setup = self.setup_key(candidate, last, trend15, atr_series)
            if exhaustion_triggered and candidate == "SELL":
                setup = f"{setup}|exhaustion_reversion"
            side_score = buy if candidate == "BUY" else sell
            delta, learn_note = self.learning_adjustment(setup)
            adjusted = max(0, min(99, side_score + delta))
            regime_penalty = 0.0
            regime_note = ""
            try:
                from trading.entry_protection import apply_ranging_penalty

                adjusted, regime_penalty, regime_note = apply_ranging_penalty(
                    self, market, cfg, float(adjusted)
                )
            except Exception:
                regime_note = ""
            stop_pts = max(1.0, float(cfg.stop_distance_points))
            atr_ratio = float(last.get("atr", 0) or 0) / stop_pts
            try:
                from system.ml_filter_overrides import evaluate_filter_block

                ml_blocked, ml_block_reason = evaluate_filter_block(
                    adjusted_score=float(adjusted),
                    raw_score=float(side_score),
                    rsi=float(last.get("rsi", 0) or 0),
                    atr_ratio=atr_ratio,
                )
            except Exception:
                ml_blocked, ml_block_reason = False, ""
            if ml_blocked and self._high_confidence_override_clear(
                direction=candidate,
                peak_score=float(adjusted),
                threshold=threshold,
            ):
                ml_blocked = False
                ml_block_reason = ""
            if ml_blocked:
                notes = (
                    f"raw={raw_sig}, buy_score={buy:.1f}, sell_score={sell:.1f}, "
                    f"threshold={threshold:.0f}, blocked: {ml_block_reason}, {learn_note}"
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
                    "ml_filter_block": ml_block_reason,
                    "h1_bearish": h1_bearish,
                    "h1_bullish": h1_bullish,
                }
                self.last_snapshot[market] = snapshot
                result = SignalResult(
                    "WAIT",
                    float(raw_conf),
                    float(adjusted),
                    float(delta),
                    setup,
                    notes,
                    snapshot,
                )
                self._log_evaluation_shadow(market, result, threshold=threshold)
                return result
            if (
                cfg.get("enforce_1h_ema_filter", True)
                and candidate == "SELL"
                and (trend60 is None or not h1_bearish)
            ):
                h1_penalty = H1_EMA_SOFT_PENALTY
                adjusted = max(0, min(99, adjusted - h1_penalty))
                h1_note = (
                    "1h EMA soft penalty -8 (fast >= slow)"
                    if trend60 is not None
                    else "1h EMA soft penalty -8 (1h collecting)"
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
        if h1_note:
            vol_note = f"{vol_note}, {h1_note}"
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
            "h1_penalty": h1_penalty,
            "regime_penalty": regime_penalty if candidate in ("BUY", "SELL") else 0.0,
            **exhaustion_meta,
        }
        self.last_snapshot[market] = snapshot

        if (
            cfg.vol_regime_filter_enabled
            and current_regime == "low"
            and signal in ("BUY", "SELL")
            and adjusted < HIGH_CONFIDENCE_OVERRIDE_THRESHOLD
        ):
            signal = "WAIT"
            notes = f"{notes} | {vol_block_reason}"

        peak_score = self._peak_signal_score(
            adjusted=float(adjusted),
            raw_conf=float(raw_conf),
            buy=float(buy),
            sell=float(sell),
        )
        direction = candidate if candidate in ("BUY", "SELL") else raw_sig
        if signal == "WAIT" and self._high_confidence_override_clear(
            direction=str(direction),
            peak_score=peak_score,
            threshold=float(threshold),
        ):
            signal = str(direction)
            adjusted = max(float(adjusted), peak_score)
            notes = (
                f"{notes} | high-confidence override "
                f"({peak_score:.1f}% >= {HIGH_CONFIDENCE_OVERRIDE_THRESHOLD:.0f}%)"
            )

        # Record bar key when an actionable signal fires so the next tick for the
        # SAME closed bar at the SAME price level is suppressed (avoids sending
        # duplicate orders between consecutive ticks within the same bar).
        if signal in ("BUY", "SELL") and REQUIRE_CLOSED_BAR_G5:
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
