"""
3-state Markov Regime Switching Engine — ADX / ATR / spread over 1440-minute window.

Background refresh only — vectorized NumPy ring views, no Pandas on evaluate path.
"""

from __future__ import annotations

import json
import threading
import time
from enum import IntEnum
from typing import Any

import numpy as np

from signals.indicators import _np_adx, _np_atr
from system.market_data_hub import NIGHT_MATRIX_EPICS, get_market_data_hub
from trading.ohlc_cache_paths import ohlc_cache_path

_WINDOW_BARS = 288  # 288 × 5m = 1440 minutes
_REFRESH_SEC = 2.0
_ADX_PERIOD = 14
_ATR_PERIOD = 14
_FLOAT64 = np.float64

_TRANSITION = np.array(
    [
        [0.70, 0.15, 0.15],
        [0.10, 0.75, 0.15],
        [0.20, 0.15, 0.65],
    ],
    dtype=_FLOAT64,
)

_UNIFORM_PROBS = np.array([0.33, 0.34, 0.33], dtype=_FLOAT64)


class RegimeState(IntEnum):
    MEAN_REVERSION = 0
    HV_TREND = 1
    CHOP = 2


_STATE_LABELS = {
    RegimeState.MEAN_REVERSION: "mean_reversion",
    RegimeState.HV_TREND: "hv_trend",
    RegimeState.CHOP: "chop",
}

_STRATEGY_GATES = {
    RegimeState.MEAN_REVERSION: {
        "mode": "fade_extremes",
        "size_factor": 0.85,
        "stop_factor": 0.90,
        "limit_factor": 0.85,
        "allow_entries": True,
    },
    RegimeState.HV_TREND: {
        "mode": "momentum",
        "size_factor": 1.10,
        "stop_factor": 1.25,
        "limit_factor": 1.35,
        "allow_entries": True,
    },
    RegimeState.CHOP: {
        "mode": "reduced",
        "size_factor": 0.50,
        "stop_factor": 0.75,
        "limit_factor": 0.70,
        "allow_entries": False,
    },
}


class _KalmanBelief:
    __slots__ = ("x", "p", "q", "r")

    def __init__(self) -> None:
        self.x = 1.0
        self.p = 1.0
        self.q = 0.02
        self.r = 0.15

    def update(self, measurement: float) -> float:
        p_pred = self.p + self.q
        k = p_pred / (p_pred + self.r)
        self.x = self.x + k * (measurement - self.x)
        self.p = (1.0 - k) * p_pred
        return self.x


class EpicRegimeSnapshot:
    __slots__ = (
        "epic",
        "state",
        "state_label",
        "raw_state",
        "confidence",
        "adx",
        "atr",
        "atr_ratio",
        "spread_pts",
        "spread_z",
        "kalman_index",
        "strategy_gate",
        "healthy",
        "reason",
    )

    def __init__(
        self,
        *,
        epic: str,
        state: int,
        state_label: str,
        raw_state: int,
        confidence: float,
        adx: float,
        atr: float,
        atr_ratio: float,
        spread_pts: float,
        spread_z: float,
        kalman_index: float,
        strategy_gate: dict[str, Any],
        healthy: bool,
        reason: str,
    ) -> None:
        self.epic = epic
        self.state = state
        self.state_label = state_label
        self.raw_state = raw_state
        self.confidence = confidence
        self.adx = adx
        self.atr = atr
        self.atr_ratio = atr_ratio
        self.spread_pts = spread_pts
        self.spread_z = spread_z
        self.kalman_index = kalman_index
        self.strategy_gate = strategy_gate
        self.healthy = healthy
        self.reason = reason

    def to_dict(self) -> dict[str, Any]:
        return {
            "epic": self.epic,
            "state": self.state,
            "state_label": self.state_label,
            "raw_state": self.raw_state,
            "confidence": round(self.confidence, 3),
            "adx": round(self.adx, 2),
            "atr": round(self.atr, 4),
            "atr_ratio": round(self.atr_ratio, 3),
            "spread_pts": round(self.spread_pts, 4),
            "spread_z": round(self.spread_z, 3),
            "kalman_index": round(self.kalman_index, 3),
            "strategy_gate": dict(self.strategy_gate),
            "healthy": self.healthy,
            "reason": self.reason,
        }


class _EpicEngine:
    __slots__ = (
        "epic",
        "_kalman",
        "_last_probs",
        "_emit_buf",
        "_high",
        "_low",
        "_close",
        "_spread",
        "_count",
        "_cache_mtime",
        "_cache_size",
        "_ring_rev",
    )

    def __init__(self, epic: str) -> None:
        self.epic = epic
        self._kalman = _KalmanBelief()
        self._last_probs = _UNIFORM_PROBS.copy()
        self._emit_buf = np.zeros(3, dtype=_FLOAT64)
        self._high = np.zeros(_WINDOW_BARS, dtype=_FLOAT64)
        self._low = np.zeros(_WINDOW_BARS, dtype=_FLOAT64)
        self._close = np.zeros(_WINDOW_BARS, dtype=_FLOAT64)
        self._spread = np.zeros(_WINDOW_BARS, dtype=_FLOAT64)
        self._count = 0
        self._cache_mtime = -1.0
        self._cache_size = -1
        self._ring_rev = 0

    def _refresh_ohlc_ring_if_stale(self) -> int:
        """Load tail of OHLC cache into pre-allocated ring — skip if file unchanged."""
        path = ohlc_cache_path(self.epic)
        if not path.is_file():
            self._count = 0
            self._ring_rev += 1
            return 0
        try:
            st = path.stat()
        except OSError:
            self._count = 0
            self._ring_rev += 1
            return 0
        if (
            st.st_mtime == self._cache_mtime
            and st.st_size == self._cache_size
            and self._count > 0
        ):
            return self._count
        try:
            from system.market_data_hub import load_binary_ohlc_cache, write_binary_ohlc_cache

            bh, bl, bc, bs, bn = load_binary_ohlc_cache(self.epic)
            if bn >= _WINDOW_BARS:
                self._high[:bn] = bh
                self._low[:bn] = bl
                self._close[:bn] = bc
                self._spread[:bn] = bs
                self._count = bn
                self._cache_mtime = st.st_mtime
                self._cache_size = st.st_size
                self._ring_rev += 1
                return bn
        except Exception:
            pass
        try:
            raw = path.read_bytes()
        except OSError:
            self._count = 0
            self._ring_rev += 1
            return 0
        if not raw:
            self._count = 0
            self._ring_rev += 1
            return 0
        # Tail scan — last ~512 KiB of jsonl lines
        tail = raw[-524288:] if len(raw) > 524288 else raw
        lines = tail.splitlines()[-_WINDOW_BARS:]
        n = 0
        for line in lines:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            c = float(obj.get("close") or obj.get("c") or 0)
            if c <= 0:
                continue
            h = float(obj.get("high") or obj.get("h") or c)
            lo = float(obj.get("low") or obj.get("l") or c)
            sp = float(obj.get("spread") or 0)
            self._high[n] = h if h > 0 else c
            self._low[n] = lo if lo > 0 else c
            self._close[n] = c
            self._spread[n] = sp
            n += 1
            if n >= _WINDOW_BARS:
                break
        self._count = n
        self._cache_mtime = st.st_mtime
        self._cache_size = st.st_size
        self._ring_rev += 1
        try:
            from system.market_data_hub import write_binary_ohlc_cache

            write_binary_ohlc_cache(
                self.epic,
                high=self._high[:n],
                low=self._low[:n],
                close=self._close[:n],
                spread=self._spread[:n],
            )
        except Exception:
            pass
        return n

    def hydrate_ring_with_fallback(self) -> dict[str, Any]:
        """
        Cache-first hydration; on missing/sparse data seed from hub tick or zero block.
        Never blocks — returns immediately with bars count and source label.
        """
        n = self._refresh_ohlc_ring_if_stale()
        if n >= _WINDOW_BARS:
            return {"bars": n, "source": "cache", "fallback": False}

        try:
            from system.market_data_hub import get_market_data_hub

            snap = get_market_data_hub().get_snapshot(self.epic)
            if snap is not None and float(getattr(snap, "bid", 0) or 0) > 0:
                bid = float(snap.bid)
                offer = float(getattr(snap, "offer", 0) or bid)
                mid = (bid + offer) * 0.5
                sp = max(0.0, offer - bid)
                self._high[0] = mid + sp * 0.5
                self._low[0] = max(1e-12, mid - sp * 0.5)
                self._close[0] = mid
                self._spread[0] = sp
                self._count = 1
                self._ring_rev += 1
                return {"bars": 1, "source": "hub", "fallback": True}
        except Exception:
            pass

        # Pre-allocated zero block — rings ready; bars accumulate from live ticks
        self._high.fill(0.0)
        self._low.fill(0.0)
        self._close.fill(0.0)
        self._spread.fill(0.0)
        self._count = 0
        self._ring_rev += 1
        return {"bars": 0, "source": "zero", "fallback": True}

    def _ohlc_views(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int]:
        n = self._refresh_ohlc_ring_if_stale()
        if n < _ATR_PERIOD + 5:
            return self._high[:0], self._low[:0], self._close[:0], self._spread[:0], 0
        return self._high[:n], self._low[:n], self._close[:n], self._spread[:n], n

    def _emit_probs(self, adx_v: float, atr_ratio: float, spread_z: float) -> np.ndarray:
        p = self._emit_buf
        p[1] = max(0.05, min(0.95, (adx_v / 40.0) * 0.5 + max(0.0, atr_ratio - 1.0) * 0.35))
        p[0] = max(
            0.05,
            min(
                0.95,
                (1.0 - adx_v / 35.0) * 0.4
                + max(0.0, 1.2 - atr_ratio) * 0.35
                + max(0.0, -spread_z) * 0.1,
            ),
        )
        p[2] = max(
            0.05,
            min(
                0.95,
                (1.0 - adx_v / 30.0) * 0.3
                + max(0.0, spread_z) * 0.25
                + (0.15 if 0.85 <= atr_ratio <= 1.15 else 0.0),
            ),
        )
        s = float(p.sum())
        if s <= 0:
            return _UNIFORM_PROBS
        p /= s
        return p

    def evaluate(self) -> EpicRegimeSnapshot:
        high, low, close, spreads, n = self._ohlc_views()
        spread_pts = 0.0
        try:
            q = get_market_data_hub().get_snapshot(self.epic)
            if q is not None:
                bid = float(getattr(q, "bid", 0) or 0)
                offer = float(getattr(q, "offer", 0) or 0)
                if bid > 0 and offer > 0:
                    spread_pts = offer - bid
        except Exception:
            pass

        if n < _ATR_PERIOD + 5:
            gate = dict(_STRATEGY_GATES[RegimeState.CHOP])
            gate["allow_entries"] = False
            return EpicRegimeSnapshot(
                epic=self.epic,
                state=int(RegimeState.CHOP),
                state_label=_STATE_LABELS[RegimeState.CHOP],
                raw_state=int(RegimeState.CHOP),
                confidence=0.0,
                adx=0.0,
                atr=0.0,
                atr_ratio=1.0,
                spread_pts=spread_pts,
                spread_z=0.0,
                kalman_index=float(self._kalman.x),
                strategy_gate=gate,
                healthy=False,
                reason="insufficient_bars",
            )

        cache_key = (self._ring_rev, n)
        with _lock:
            cached = _indicator_cache.get(self.epic)
        if cached is not None and cached[0] == cache_key:
            adx_v, atr_v, atr_ratio, sp_mean, sp_std = cached[1]
        else:
            adx_vals = _np_adx(high, low, close, _ADX_PERIOD)
            atr_vals = _np_atr(high, low, close, _ATR_PERIOD)
            adx_v = float(adx_vals[-1]) if adx_vals.size else 0.0
            atr_v = float(atr_vals[-1]) if atr_vals.size else 0.0
            tail_n = min(60, int(atr_vals.size))
            # Baseline excludes the current bar so a vol spike is not diluted
            # by its own contribution to the mean.
            if int(atr_vals.size) >= tail_n + 1:
                atr_long = float(np.nanmean(atr_vals[-tail_n - 1 : -1]))
            elif int(atr_vals.size) >= 2:
                atr_long = float(np.nanmean(atr_vals[:-1]))
            else:
                atr_long = atr_v
            atr_ratio = (atr_v / atr_long) if atr_long > 0 else 1.0

            if spreads.size > 0:
                sp_tail = spreads[-tail_n:] if tail_n > 0 else spreads
                sp_mean = float(np.mean(sp_tail))
                sp_std = float(np.std(sp_tail)) if sp_tail.size > 3 else max(sp_mean * 0.1, 0.01)
            else:
                sp_mean = spread_pts
                sp_std = max(sp_mean * 0.1, 0.01)
            with _lock:
                _indicator_cache[self.epic] = (
                    cache_key,
                    (adx_v, atr_v, atr_ratio, sp_mean, sp_std),
                )
        spread_z = (spread_pts - sp_mean) / sp_std if sp_std > 0 else 0.0

        emit = self._emit_probs(adx_v, atr_ratio, spread_z)
        prior = self._last_probs @ _TRANSITION
        post = emit * prior
        post_sum = float(post.sum())
        if post_sum > 0:
            post /= post_sum
        self._last_probs = post

        raw_state = int(np.argmax(post))
        kalman_idx = self._kalman.update(float(raw_state))
        smooth_state = int(round(max(0.0, min(2.0, kalman_idx))))
        confidence = float(post[smooth_state])

        gate = dict(_STRATEGY_GATES.get(RegimeState(smooth_state), _STRATEGY_GATES[RegimeState.CHOP]))
        try:
            from analytics.tuning_params import get_tuning_params

            tp = get_tuning_params().get("params") or {}
            if tp.get("chop_allow_entries") is True and smooth_state == int(RegimeState.CHOP):
                gate["allow_entries"] = True
                gate["size_factor"] = float(tp.get("chop_size_factor", 0.35))
        except Exception:
            pass
        try:
            from runtime.parameter_tuner import merge_tuned_gate

            merge_tuned_gate(gate, smooth_state)
        except Exception:
            pass

        return EpicRegimeSnapshot(
            epic=self.epic,
            state=smooth_state,
            state_label=_STATE_LABELS.get(RegimeState(smooth_state), "chop"),
            raw_state=raw_state,
            confidence=confidence,
            adx=adx_v,
            atr=atr_v,
            atr_ratio=atr_ratio,
            spread_pts=spread_pts,
            spread_z=spread_z,
            kalman_index=kalman_idx,
            strategy_gate=gate,
            healthy=True,
            reason="ok",
        )


_lock = threading.RLock()
_engines: dict[str, _EpicEngine] = {}
# epic -> ((ring_rev, bar_count), (adx_v, atr_v, atr_ratio, sp_mean, sp_std))
_indicator_cache: dict[str, tuple[tuple[int, int], tuple[float, float, float, float, float]]] = {}
_snapshot: dict[str, Any] = {
    "ok": True,
    "healthy": False,
    "markets": [],
    "ts": 0.0,
    "heartbeat_ts": "",
}
_refresher_thread: threading.Thread | None = None
_refresher_stop = threading.Event()
_last_ring_refresh_ts: float = 0.0
_last_warmup_meta: dict[str, Any] = {}


def _utc_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _engine(epic: str) -> _EpicEngine:
    with _lock:
        eng = _engines.get(epic)
        if eng is None:
            eng = _EpicEngine(epic)
            _engines[epic] = eng
        return eng


def evaluate_epic_regime(epic: str) -> EpicRegimeSnapshot:
    snap = _engine(epic).evaluate()
    try:
        from runtime.portfolio_exploration_engine import smooth_regime_with_kalman

        smoothed, conf = smooth_regime_with_kalman(
            epic, int(snap.state), float(snap.confidence)
        )
        if smoothed != int(snap.state):
            gate = dict(_STRATEGY_GATES.get(RegimeState(smoothed), _STRATEGY_GATES[RegimeState.CHOP]))
            return EpicRegimeSnapshot(
                epic=snap.epic,
                state=smoothed,
                state_label=_STATE_LABELS.get(RegimeState(smoothed), "chop"),
                raw_state=int(snap.raw_state),
                confidence=conf,
                adx=snap.adx,
                atr=snap.atr,
                atr_ratio=snap.atr_ratio,
                spread_pts=snap.spread_pts,
                spread_z=snap.spread_z,
                kalman_index=snap.kalman_index,
                strategy_gate=gate,
                healthy=snap.healthy,
                reason=f"kalman_smooth:{snap.reason}",
            )
    except Exception:
        pass
    return snap


def get_regime_transition_matrix() -> np.ndarray:
    """Expose Markov transition matrix for forward-walk probability engine."""
    return np.asarray(_TRANSITION, dtype=_FLOAT64).copy()


def apply_transition_matrix_strictness(*, bump: float = 0.05) -> dict[str, Any]:
    """
  Tighten Markov self-transition weights when cognitive healer detects drift.

  Increases diagonal persistence (filter chop transitions) by *bump* per correction,
  re-normalizing rows in-place.
  """
    global _TRANSITION
    delta = max(0.0, min(0.12, float(bump)))
    if delta <= 0:
        return {"ok": True, "delta": 0.0}
    with _lock:
        mat = np.asarray(_TRANSITION, dtype=_FLOAT64).copy()
        for i in range(3):
            mat[i, i] = min(0.92, float(mat[i, i]) + delta)
            row_sum = float(mat[i].sum())
            if row_sum > 0:
                mat[i] /= row_sum
        _TRANSITION = mat
    return {"ok": True, "delta": delta, "matrix": mat.tolist()}


def reset_epic_regime_ring_with_hub_seed(epic: str) -> dict[str, Any]:
    """Reset 288-bar ring view and seed from hub mid (wipes NaN/corrupt states)."""
    key = str(epic or "").strip()
    if not key:
        return {"ok": False, "bars": 0, "source": "missing_epic"}
    eng = _engine(key)
    eng._kalman = _KalmanBelief()
    eng._last_probs = _UNIFORM_PROBS.copy()
    eng._cache_mtime = -1.0
    eng._cache_size = -1
    try:
        from system.market_data_hub import get_market_data_hub

        snap = get_market_data_hub().get_snapshot(key)
        if snap is not None and float(getattr(snap, "bid", 0) or 0) > 0:
            bid = float(snap.bid)
            offer = float(getattr(snap, "offer", 0) or bid)
            mid = (bid + offer) * 0.5
            sp = max(0.0, offer - bid)
            hi = mid + sp * 0.5
            lo = max(1e-12, mid - sp * 0.5)
            eng._high.fill(hi)
            eng._low.fill(lo)
            eng._close.fill(mid)
            eng._spread.fill(sp)
            eng._count = _WINDOW_BARS
            eng._ring_rev += 1
            return {"ok": True, "bars": _WINDOW_BARS, "source": "hub_seed_block", "epic": key}
    except Exception:
        pass
    eng._high.fill(0.0)
    eng._low.fill(0.0)
    eng._close.fill(0.0)
    eng._spread.fill(0.0)
    eng._count = 0
    eng._ring_rev += 1
    return {"ok": True, "bars": 0, "source": "zero_reset", "epic": key}


def inject_synthetic_ring_continuity(epic: str, *, micro_ticks: int = 48) -> dict[str, Any]:
    """
    Anti-starvation synthetic micro-tick path — reads OHLC cache tail (512 KiB),
    derives median volatility envelope, and fills the 288-bar ring for indicator continuity.
    """
    key = str(epic or "").strip()
    if not key:
        return {"ok": False, "bars": 0, "source": "missing_epic"}
    eng = _engine(key)
    eng._refresh_ohlc_ring_if_stale()
    n = int(eng._count)
    closes = np.asarray(eng._close[: max(n, 1)], dtype=_FLOAT64)
    closes = closes[closes > 0]
    if closes.size >= 3:
        mid = float(closes[-1])
        diffs = np.abs(np.diff(closes[-min(64, closes.size) :]))
        vol = float(np.median(diffs)) if diffs.size else 0.0001
        spread_med = float(np.median(eng._spread[: max(n, 1)])) if n > 0 else vol
    else:
        defaults = None
        try:
            from system.market_data_hub import _HUB_SEED_DEFAULTS, get_market_data_hub

            snap = get_market_data_hub().get_snapshot(key)
            if snap is not None and float(getattr(snap, "bid", 0) or 0) > 0:
                bid = float(snap.bid)
                offer = float(getattr(snap, "offer", 0) or bid)
                mid = (bid + offer) * 0.5
                vol = max(0.0001, abs(offer - bid) * 0.35)
                spread_med = max(vol, abs(offer - bid))
            else:
                defaults = _HUB_SEED_DEFAULTS.get(key)
                if defaults:
                    mid = (defaults[0] + defaults[1]) * 0.5
                    vol = max(0.0001, abs(defaults[1] - defaults[0]))
                    spread_med = vol
                else:
                    mid, vol, spread_med = 100.0, 0.01, 0.01
        except Exception:
            mid, vol, spread_med = 100.0, 0.01, 0.01

    vol = max(float(vol), 1e-6)
    spread_med = max(float(spread_med), vol * 0.5)
    ticks = max(8, min(int(micro_ticks), _WINDOW_BARS))
    walk = np.zeros(_WINDOW_BARS, dtype=_FLOAT64)
    walk[0] = mid
    for i in range(1, _WINDOW_BARS):
        phase = ((i * 7) % 13) / 13.0 - 0.5
        walk[i] = walk[i - 1] + vol * phase * 1.6

    for i in range(_WINDOW_BARS):
        c = float(walk[i])
        sp = spread_med
        eng._close[i] = c
        eng._high[i] = c + sp * 0.5
        eng._low[i] = max(1e-12, c - sp * 0.5)
        eng._spread[i] = sp
    eng._count = _WINDOW_BARS
    eng._ring_rev += 1
    return {
        "ok": True,
        "bars": _WINDOW_BARS,
        "vol_envelope": round(vol, 8),
        "epic": key,
        "micro_ticks": ticks,
        "source": "synthetic_micro_ticks",
    }


def get_ring_buffer_fill_percentages() -> dict[str, float]:
    """Per-epic ring fill ratio (0-100) for AI diagnostics."""
    out: dict[str, float] = {}
    with _lock:
        epics = list(_engines.keys()) or list(NIGHT_MATRIX_EPICS)
    for epic in epics:
        try:
            eng = _engine(epic)
            n = int(eng._count)
            out[str(epic)] = round(100.0 * min(n, _WINDOW_BARS) / max(_WINDOW_BARS, 1), 2)
        except Exception:
            out[str(epic)] = 0.0
    return out


def get_regime_gate(epic: str) -> dict[str, Any]:
    with _lock:
        for row in _snapshot.get("markets") or []:
            if row.get("epic") == epic:
                return dict(row.get("strategy_gate") or {})
    return evaluate_epic_regime(epic).strategy_gate


def regime_allows_entry(epic: str) -> tuple[bool, str]:
    try:
        from system.demo_execution_plane import demo_throughput_active

        if demo_throughput_active():
            return True, ""
    except Exception:
        pass
    try:
        from system.gate_relaxation import demo_soak_enabled

        if demo_soak_enabled():
            return True, ""
    except Exception:
        pass
    gate = get_regime_gate(epic)
    if not gate:
        return False, "regime_unknown"
    if gate.get("allow_entries", False):
        return True, ""
    return False, f"regime_{gate.get('mode', 'chop')}_pause"


def _refresh_once() -> None:
    markets: list[dict[str, Any]] = []
    healthy_count = 0
    for epic in NIGHT_MATRIX_EPICS:
        snap = _engine(epic).evaluate()
        markets.append(snap.to_dict())
        if snap.healthy:
            healthy_count += 1
    body = {
        "ok": True,
        "healthy": healthy_count >= max(1, len(NIGHT_MATRIX_EPICS) // 2),
        "markets": markets,
        "healthy_count": healthy_count,
        "total_markets": len(NIGHT_MATRIX_EPICS),
        "window_minutes": 1440,
        "ts": time.time(),
        "heartbeat_ts": _utc_iso(),
    }
    with _lock:
        _snapshot.clear()
        _snapshot.update(body)


def get_regime_switch_snapshot() -> dict[str, Any]:
    with _lock:
        return dict(_snapshot) if _snapshot.get("markets") else {"ok": True, "healthy": False, "markets": []}


def get_epic_close_returns(
    epic: str,
    *,
    min_bars: int = 20,
    max_bars: int = _WINDOW_BARS,
) -> np.ndarray | None:
    """Log returns from rolling OHLC ring — for cross-market correlation guard."""
    eng = _engine(epic)
    n = eng._refresh_ohlc_ring_if_stale()
    if n < min_bars:
        return None
    close = eng._close[:n]
    tail = min(n, max_bars)
    close = close[-tail:]
    with np.errstate(divide="ignore", invalid="ignore"):
        rets = np.diff(np.log(np.maximum(close, 1e-12)))
    if rets.size < min_bars - 1:
        return None
    return rets.astype(_FLOAT64, copy=False)


def _refresh_loop() -> None:
    while not _refresher_stop.wait(_REFRESH_SEC):
        try:
            _refresh_once()
        except Exception:
            pass


def start_regime_switch_refresher() -> None:
    global _refresher_thread
    if _refresher_thread is not None and _refresher_thread.is_alive():
        return
    _refresher_stop.clear()
    _refresh_once()
    _refresher_thread = threading.Thread(
        target=_refresh_loop, name="regime-switch-refresher", daemon=True
    )
    _refresher_thread.start()


def stop_regime_switch_refresher() -> None:
    _refresher_stop.set()


def reset_regime_switch_for_tests() -> None:
    global _engines, _refresher_thread, _last_ring_refresh_ts, _last_warmup_meta
    with _lock:
        _engines.clear()
        _indicator_cache.clear()
        _snapshot.clear()
        _snapshot.update({"ok": True, "healthy": False, "markets": []})
        _last_ring_refresh_ts = 0.0
        _last_warmup_meta.clear()
    _refresher_thread = None


def warm_up_regime_ring_buffers(epics: list[str] | tuple[str, ...] | None = None) -> dict[str, int]:
    """Phase-2 warmup — cache tail or immediate hub/zero fallback; never hangs."""
    global _last_ring_refresh_ts, _last_warmup_meta
    universe = list(epics or NIGHT_MATRIX_EPICS)
    warmed: dict[str, int] = {}
    fallback_epics: list[str] = []
    hub_epics: list[str] = []
    for epic in universe:
        try:
            row = _engine(epic).hydrate_ring_with_fallback()
            warmed[epic] = int(row.get("bars") or 0)
            if row.get("fallback"):
                fallback_epics.append(epic)
                if row.get("source") == "hub":
                    hub_epics.append(epic)
        except Exception:
            warmed[epic] = 0
            fallback_epics.append(epic)
    with _lock:
        try:
            _refresh_once()
        except Exception:
            pass
        _last_ring_refresh_ts = time.time()
        _last_warmup_meta = {
            "total": len(universe),
            "fallback_count": len(fallback_epics),
            "hub_seed_count": len(hub_epics),
            "fallback_epics": fallback_epics[:20],
        }
    return warmed


def get_last_ring_warmup_meta() -> dict[str, Any]:
    with _lock:
        return dict(_last_warmup_meta)


def get_regime_ring_refresh_ts() -> float:
    with _lock:
        return float(_last_ring_refresh_ts)
