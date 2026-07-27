"""Widened microstructure/regime features for offline dual-engine ML replay.

Derived from real OHLC (and signal-row passthroughs). No fabricated labels —
callers supply forward WIN/LOSS separately (sniper=3-bar, long=6-bar).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

# Canonical widened set used by the offline harness + MLScorer when present.
FEATURE_NAMES: tuple[str, ...] = (
    "adjusted_score",
    "raw_score",
    "rsi",
    "atr_ratio",
    "spread_ratio",
    "range_ratio",
    "ret_1",
    "ret_3",
    "ret_6",
    "ret_12",
    "momentum_12",
    "vol_regime_idx",
    "session_window_idx",
)

_SESSION_IDX: dict[str, float] = {
    "asia_early": 0.0,
    "london_morning": 1.0,
    "london_us_overlap": 2.0,
    "us_afternoon": 3.0,
    "late": 4.0,
}

_VOL_IDX: dict[str, float] = {
    "low": 0.0,
    "normal": 1.0,
    "high": 2.0,
    "unknown": -1.0,
}


def session_window_idx(session: str | None) -> float:
    key = str(session or "").strip().lower()
    if key in _SESSION_IDX:
        return _SESSION_IDX[key]
    # Accept slot-style ids already used by auto_trainer.
    if key:
        try:
            from ml.auto_trainer import _session_slot_idx

            slot = _session_slot_idx(key)
            if slot is not None:
                return float(slot)
        except Exception:
            pass
    return -1.0


def vol_regime_idx(regime: str | None) -> float:
    return _VOL_IDX.get(str(regime or "").strip().lower(), -1.0)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _closes_from_bars(bars: Sequence[Mapping[str, Any]], end_idx: int) -> list[float]:
    out: list[float] = []
    if end_idx < 0:
        return out
    start = max(0, end_idx - 19)
    for b in bars[start : end_idx + 1]:
        c = _safe_float(b.get("c") if "c" in b else b.get("close"), 0.0)
        if c > 0:
            out.append(c)
    return out


def _return_n(closes: Sequence[float], n: int) -> float:
    if len(closes) <= n:
        return 0.0
    prev = closes[-(n + 1)]
    cur = closes[-1]
    if prev <= 0 or cur <= 0:
        return 0.0
    return (cur / prev) - 1.0


def features_from_ohlc(
    bars: Sequence[Mapping[str, Any]],
    idx: int,
    *,
    stop_pts: float,
    rsi: float | None = None,
    atr: float | None = None,
    spread: float | None = None,
    adjusted_score: float = 0.0,
    raw_score: float = 0.0,
    session_window: str | None = None,
    vol_regime: str | None = None,
) -> dict[str, float]:
    """Build the widened feature vector at bar ``idx`` from real OHLC."""
    stop = max(1.0, _safe_float(stop_pts, 1.0))
    bar = bars[idx] if 0 <= idx < len(bars) else {}
    high = _safe_float(bar.get("h") if "h" in bar else bar.get("high"), 0.0)
    low = _safe_float(bar.get("l") if "l" in bar else bar.get("low"), 0.0)
    close = _safe_float(bar.get("c") if "c" in bar else bar.get("close"), 0.0)
    spr = _safe_float(spread if spread is not None else bar.get("spread"), 0.0)

    closes = _closes_from_bars(bars, idx)
    # ATR proxy: mean true-range over last 14 bars ending at idx (no look-ahead).
    atr_val = _safe_float(atr, 0.0)
    if atr_val <= 0 and idx >= 1:
        trs: list[float] = []
        lo_i = max(1, idx - 13)
        for j in range(lo_i, idx + 1):
            bj = bars[j]
            bj1 = bars[j - 1]
            h = _safe_float(bj.get("h") if "h" in bj else bj.get("high"), 0.0)
            l = _safe_float(bj.get("l") if "l" in bj else bj.get("low"), 0.0)
            pc = _safe_float(
                bj1.get("c") if "c" in bj1 else bj1.get("close"), 0.0
            )
            if h > 0 and l > 0:
                trs.append(max(h - l, abs(h - pc), abs(l - pc)) if pc > 0 else h - l)
        if trs:
            atr_val = sum(trs) / len(trs)

    rsi_val = _safe_float(rsi, 50.0)
    if rsi is None and len(closes) >= 15:
        # Lightweight Wilder-ish RSI on closes (offline only; live prefers engine RSI).
        gains = 0.0
        losses = 0.0
        for a, b in zip(closes[-15:-1], closes[-14:]):
            d = b - a
            if d >= 0:
                gains += d
            else:
                losses -= d
        if losses <= 1e-12:
            rsi_val = 100.0 if gains > 0 else 50.0
        else:
            rs = gains / losses
            rsi_val = 100.0 - (100.0 / (1.0 + rs))

    bar_range = max(0.0, high - low) if high > 0 and low > 0 else 0.0
    range_ratio = bar_range / atr_val if atr_val > 1e-12 else 0.0

    if not session_window and bar:
        ts = str(bar.get("t") or bar.get("timestamp") or "")
        if ts:
            try:
                from signals.indicators import session_name

                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                session_window = session_name(dt)
            except Exception:
                session_window = None

    feats = {
        "adjusted_score": _safe_float(adjusted_score, 0.0),
        "raw_score": _safe_float(raw_score, 0.0),
        "rsi": rsi_val,
        "atr_ratio": atr_val / stop,
        "spread_ratio": spr / stop,
        "range_ratio": range_ratio,
        "ret_1": _return_n(closes, 1),
        "ret_3": _return_n(closes, 3),
        "ret_6": _return_n(closes, 6),
        "ret_12": _return_n(closes, 12),
        "momentum_12": _return_n(closes, 12),
        "vol_regime_idx": vol_regime_idx(vol_regime),
        "session_window_idx": session_window_idx(session_window),
    }
    # Keep close for callers that want a sanity check; not a model feature.
    if close > 0:
        feats["_close"] = close
    return {k: float(feats[k]) for k in FEATURE_NAMES}


def features_from_replay_row(
    row: Mapping[str, Any],
    *,
    bars: Sequence[Mapping[str, Any]] | None = None,
    bar_idx: int | None = None,
) -> dict[str, float]:
    """Prefer precomputed row fields; fall back to OHLC window when provided."""
    stop = _safe_float(row.get("stop_pts"), 4.0) or 4.0
    atr = row.get("atr")
    # If row already carries widened fields, honour them (idempotent re-runs).
    if all(k in row and row.get(k) is not None for k in ("ret_1", "ret_3", "range_ratio")):
        out: dict[str, float] = {}
        for name in FEATURE_NAMES:
            if name == "atr_ratio":
                if row.get("atr_ratio") is not None:
                    out[name] = _safe_float(row.get("atr_ratio"))
                else:
                    out[name] = _safe_float(atr) / max(1.0, stop)
            elif name == "spread_ratio":
                if row.get("spread_ratio") is not None:
                    out[name] = _safe_float(row.get("spread_ratio"))
                else:
                    out[name] = _safe_float(row.get("spread")) / max(1.0, stop)
            elif name == "session_window_idx":
                if row.get("session_window_idx") is not None:
                    out[name] = _safe_float(row.get("session_window_idx"))
                else:
                    out[name] = session_window_idx(
                        str(row.get("session_window") or "")
                    )
            elif name == "vol_regime_idx":
                if row.get("vol_regime_idx") is not None:
                    out[name] = _safe_float(row.get("vol_regime_idx"))
                else:
                    out[name] = vol_regime_idx(str(row.get("vol_regime") or ""))
            else:
                out[name] = _safe_float(row.get(name), 0.0)
        return out

    if bars is not None and bar_idx is not None and 0 <= bar_idx < len(bars):
        return features_from_ohlc(
            bars,
            bar_idx,
            stop_pts=stop,
            rsi=_safe_float(row.get("rsi"), 50.0) if row.get("rsi") is not None else None,
            atr=_safe_float(atr) if atr is not None else None,
            spread=_safe_float(row.get("spread")) if row.get("spread") is not None else None,
            adjusted_score=_safe_float(row.get("adjusted_score"), 0.0),
            raw_score=_safe_float(row.get("raw_score"), 0.0),
            session_window=str(row.get("session_window") or "") or None,
            vol_regime=str(row.get("vol_regime") or "") or None,
        )

    # Row-only fallback (no OHLC window) — still wider than rsi/atr alone.
    atr_f = _safe_float(atr, 0.0)
    spread_f = _safe_float(row.get("spread"), 0.0)
    return {
        "adjusted_score": _safe_float(row.get("adjusted_score"), 0.0),
        "raw_score": _safe_float(row.get("raw_score"), 0.0),
        "rsi": _safe_float(row.get("rsi"), 50.0),
        "atr_ratio": atr_f / max(1.0, stop),
        "spread_ratio": spread_f / max(1.0, stop),
        "range_ratio": _safe_float(row.get("range_ratio"), 0.0),
        "ret_1": _safe_float(row.get("ret_1"), 0.0),
        "ret_3": _safe_float(row.get("ret_3"), 0.0),
        "ret_6": _safe_float(row.get("ret_6"), 0.0),
        "ret_12": _safe_float(row.get("ret_12"), 0.0),
        "momentum_12": _safe_float(row.get("momentum_12"), 0.0),
        "vol_regime_idx": vol_regime_idx(str(row.get("vol_regime") or "")),
        "session_window_idx": session_window_idx(
            str(row.get("session_window") or "")
        ),
    }


def features_from_close_history(
    closes: Sequence[float],
    *,
    stop_pts: float,
    rsi: float,
    atr: float,
    spread: float,
    high: float = 0.0,
    low: float = 0.0,
    adjusted_score: float = 0.0,
    raw_score: float = 0.0,
    session_window: str | None = None,
    vol_regime: str | None = None,
) -> dict[str, float]:
    """Live-path helper when the engine snapshot carries recent closes."""
    stop = max(1.0, float(stop_pts) or 1.0)
    atr_f = max(0.0, float(atr) or 0.0)
    bar_range = max(0.0, float(high) - float(low)) if high > 0 and low > 0 else 0.0
    cl = [float(c) for c in closes if c and float(c) > 0]
    return {
        "adjusted_score": float(adjusted_score),
        "raw_score": float(raw_score),
        "rsi": float(rsi),
        "atr_ratio": atr_f / stop,
        "spread_ratio": float(spread) / stop,
        "range_ratio": (bar_range / atr_f) if atr_f > 1e-12 else 0.0,
        "ret_1": _return_n(cl, 1),
        "ret_3": _return_n(cl, 3),
        "ret_6": _return_n(cl, 6),
        "ret_12": _return_n(cl, 12),
        "momentum_12": _return_n(cl, 12),
        "vol_regime_idx": vol_regime_idx(vol_regime),
        "session_window_idx": session_window_idx(session_window),
    }


def enrich_replay_row(
    row: Mapping[str, Any],
    *,
    bars: Sequence[Mapping[str, Any]] | None = None,
    bar_idx: int | None = None,
) -> dict[str, Any]:
    """Return a copy of ``row`` with widened feature fields stamped."""
    out = dict(row)
    feats = features_from_replay_row(out, bars=bars, bar_idx=bar_idx)
    out.update(feats)
    return out


def attach_features_to_records(
    records: Iterable[Mapping[str, Any]],
    bars: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Stamp widened features onto signal-replay records via timestamp join."""
    ts_to_idx = {
        str(b.get("t") or ""): i for i, b in enumerate(bars) if b.get("t")
    }
    out: list[dict[str, Any]] = []
    for row in records:
        ts = str(row.get("timestamp") or "")
        idx = ts_to_idx.get(ts)
        if idx is None:
            # Best-effort: match on entry price to nearest bar close.
            entry = _safe_float(row.get("entry") or row.get("forward_close_3"), 0.0)
            idx = None
            if entry > 0:
                for i, b in enumerate(bars):
                    if abs(_safe_float(b.get("c"), 0.0) - entry) < 1e-6:
                        idx = i
                        break
        out.append(enrich_replay_row(row, bars=bars, bar_idx=idx))
    return out
