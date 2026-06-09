"""Live vol-regime soft gate — index momentum penalty (v26 shadow router → live Tier 2)."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from system.paths import project_root


@lru_cache(maxsize=1)
def _regime_config() -> dict[str, Any]:
    path = project_root() / "config" / "config_v26.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        block = raw.get("regime") or {}
        return block if isinstance(block, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def reset_live_regime_cache_for_tests() -> None:
    _regime_config.cache_clear()


def live_vol_soft_gate_enabled() -> bool:
    return bool(_regime_config().get("live_vol_soft_gate", False))


def index_epics() -> frozenset[str]:
    raw = _regime_config().get("index_epics") or [
        "IX.D.DOW.IFM.IP",
        "IX.D.NASDAQ.IFM.IP",
    ]
    return frozenset(str(e) for e in raw if e)


def atr_percentile_block_above() -> float:
    try:
        return float(_regime_config().get("atr_percentile_block_above") or 95.0)
    except (TypeError, ValueError):
        return 95.0


def extreme_vol_penalty_pct() -> float:
    try:
        return float(_regime_config().get("extreme_vol_confidence_penalty_pct") or 15.0)
    except (TypeError, ValueError):
        return 15.0


def atr_percentile_rank(atr_values: Any) -> float | None:
    """Percentile rank (0–100) of latest ATR within trailing window."""
    try:
        import pandas as pd

        series = pd.Series(atr_values).dropna()
        if len(series) < 10:
            return None
        ref = series.iloc[-min(100, len(series)) :]
        current = float(ref.iloc[-1])
        below = float((ref < current).sum())
        return 100.0 * below / float(len(ref))
    except Exception:
        return None


def _atr_percentile_from_engine(signal_engine: Any, market: str) -> float | None:
    try:
        _, c5, _, _ = signal_engine.candle_frames(market)
        c5i = signal_engine.add_indicators(c5)
        if "atr" not in c5i.columns:
            return None
        return atr_percentile_rank(c5i["atr"])
    except Exception:
        return None


def momentum_vol_penalty(
    epic: str,
    snapshot: dict[str, Any] | None,
    *,
    signal_engine: Any | None = None,
    market: str = "",
) -> tuple[float, str]:
    """
    Return (confidence_multiplier, warning_detail).

    Indices only: extreme ATR percentile → soften momentum entries (Tier 2 WARNING).
    multiplier = 1 - penalty_pct/100 (default 0.85).
    """
    if not live_vol_soft_gate_enabled():
        return 1.0, ""
    if str(epic or "") not in index_epics():
        return 1.0, ""

    pct_rank: float | None = None
    if signal_engine is not None and market:
        pct_rank = _atr_percentile_from_engine(signal_engine, market)

    snap = snapshot or {}
    if pct_rank is None:
        vol = str(snap.get("vol_regime") or "").lower()
        if vol == "high":
            mult = 1.0 - extreme_vol_penalty_pct() / 100.0
            return mult, (
                f"vol_regime=high — momentum −{extreme_vol_penalty_pct():.0f}%"
            )
        return 1.0, ""

    if pct_rank is None:
        return 1.0, ""

    threshold = atr_percentile_block_above()
    if pct_rank < threshold:
        return 1.0, ""

    mult = 1.0 - extreme_vol_penalty_pct() / 100.0
    return mult, (
        f"extreme vol ATR p{pct_rank:.0f}≥{threshold:.0f} — "
        f"momentum −{extreme_vol_penalty_pct():.0f}%"
    )
