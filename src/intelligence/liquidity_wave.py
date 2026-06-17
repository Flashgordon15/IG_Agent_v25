"""
Time-of-day liquidity wave modulation — UK session volume nodes.

Peak windows boost trend-following confidence and autopilot scaling.
Mid-day lulls tighten entry barriers to premium set-ups only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

_LONDON = ZoneInfo("Europe/London")

PEAK_AUTOPILOT_MULTIPLIER = 1.5
LULL_CONFIDENCE_FLOOR = 0.85
LULL_CONFIDENCE_PENALTY = 0.65

# Adaptive Tokyo momentum window (01:00–04:00 UK — peak Tokyo liquidity curve)
NIKKEI_EPIC = "IX.D.NIKKEI.IFM.IP"
DEFENSIVE_PREMIUM_EPICS = frozenset(
    {
        "CS.D.CFPGOLD.CFP.IP",
        "IX.D.DOW.IFM.IP",
        "CS.D.EURUSD.CFD.IP",
    }
)
PREMIUM_DEFENSIVE_MICRO_FLOOR = 0.85
TOKYO_NIKKEI_MICRO_FLOOR = 0.65
TOKYO_MOMENTUM_START = (1, 0)  # 01:00 UK
TOKYO_MOMENTUM_END = (4, 0)  # 04:00 UK

# Volatility-sized lot scaler — overnight microstructure confidence bands
OVERNIGHT_HALF_SCALE_MIN_PCT = 65.0
OVERNIGHT_FULL_SCALE_MIN_PCT = 85.0
OVERNIGHT_HALF_SCALE_MULTIPLIER = 0.50


class LiquidityPhase(str, Enum):
    STANDARD = "standard"
    LONDON_OPEN = "london_open"
    NEW_YORK_OPEN = "new_york_open"
    LONDON_CLOSE = "london_close"
    MIDDAY_LULL = "midday_lull"
    TOKYO_MOMENTUM = "tokyo_momentum"


@dataclass(frozen=True)
class LiquidityWaveState:
    phase: LiquidityPhase
    uk_time: str
    confidence_multiplier: float
    autopilot_multiplier: float
    entry_premium_only: bool
    trend_following_focus: bool


def _minutes_since_midnight(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def _in_range(minutes: int, start_h: int, start_m: int, end_h: int, end_m: int) -> bool:
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    return start <= minutes < end


def _liquidity_wave_cfg(config: Any | None = None) -> dict[str, Any]:
    if config is None:
        try:
            from system.config import ConfigLoader

            config = ConfigLoader().load()
        except Exception:
            return {}
    try:
        block = config.get("intelligence_layer", {}).get("liquidity_wave", {})
    except Exception:
        block = getattr(getattr(config, "intelligence_layer", {}), "liquidity_wave", {})
    return dict(block) if isinstance(block, dict) else {}


def _parse_hhmm_pair(value: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        parts = str(value or "").strip().split(":")
        return int(parts[0]), int(parts[1])
    except (IndexError, TypeError, ValueError):
        return default


def in_tokyo_momentum_window(*, now: datetime | None = None, config: Any | None = None) -> bool:
    """True during 01:00–04:00 UK — adaptive Tokyo liquidity peak."""
    cfg = _liquidity_wave_cfg(config)
    sh, sm = _parse_hhmm_pair(str(cfg.get("tokyo_momentum_start", "01:00")), TOKYO_MOMENTUM_START)
    eh, em = _parse_hhmm_pair(str(cfg.get("tokyo_momentum_end", "04:00")), TOKYO_MOMENTUM_END)
    dt = now or datetime.now(tz=_LONDON)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_LONDON)
    else:
        dt = dt.astimezone(_LONDON)
    return _in_range(_minutes_since_midnight(dt), sh, sm, eh, em)


def effective_microstructure_confidence_floor(
    epic: str,
    *,
    now: datetime | None = None,
    config: Any | None = None,
) -> tuple[float, str]:
    """
    Session-aware microstructure confidence floor (0–1).

    Tokyo window (01:00–04:00 UK): Nikkei 65%; Gold / Wall St / EUR/USD stay at 85%.
    Midday lull: premium-only 85% for all. Otherwise no extra premium floor here.
    """
    key = str(epic or "").strip()
    cfg = _liquidity_wave_cfg(config)
    defensive_floor = float(cfg.get("premium_defensive_micro_floor", PREMIUM_DEFENSIVE_MICRO_FLOOR))
    tokyo_nikkei_floor = float(cfg.get("tokyo_nikkei_micro_floor", TOKYO_NIKKEI_MICRO_FLOOR))

    if in_tokyo_momentum_window(now=now, config=config):
        if key == NIKKEI_EPIC:
            return tokyo_nikkei_floor, "tokyo_momentum_nikkei_65"
        if key in DEFENSIVE_PREMIUM_EPICS:
            return defensive_floor, "premium_defensive_barrier_85"

    wave = resolve_liquidity_wave(now=now)
    if wave.entry_premium_only:
        lull_floor = float(cfg.get("lull_confidence_floor", LULL_CONFIDENCE_FLOOR))
        return lull_floor, f"{wave.phase.value}_premium_85"

    return 0.0, "standard"


def _micro_confidence_pct(micro_confidence: float) -> float:
    conf = float(micro_confidence or 0.0)
    if conf <= 1.0:
        return conf * 100.0
    return conf


def overnight_volatility_size_multiplier(
    micro_confidence: float,
    *,
    epic: str = "",
    config: Any | None = None,
    now: datetime | None = None,
) -> tuple[float, str]:
    """
    Dynamic lot scaler for overnight entries.

    65–84% micro confidence → 0.50× base size; ≥85% → 1.0× full scale.
    Only applies during overnight / Tokyo liquidity windows for night-matrix epics.
    """
    from intelligence.premium_overnight import (
        in_overnight_liquidity_window,
        is_premium_overnight_epic,
    )

    key = str(epic or "").strip()
    if not is_premium_overnight_epic(key, config):
        return 1.0, "not_premium_epic"

    in_overnight = in_overnight_liquidity_window(config=config, now=now)
    in_tokyo = in_tokyo_momentum_window(now=now, config=config)
    if not in_overnight and not in_tokyo:
        return 1.0, "outside_overnight_window"

    cfg = _liquidity_wave_cfg(config)
    half_mult = float(cfg.get("overnight_half_scale_multiplier", OVERNIGHT_HALF_SCALE_MULTIPLIER))
    full_min = float(cfg.get("overnight_full_scale_min_pct", OVERNIGHT_FULL_SCALE_MIN_PCT))
    half_min = float(cfg.get("overnight_half_scale_min_pct", OVERNIGHT_HALF_SCALE_MIN_PCT))
    conf_pct = _micro_confidence_pct(micro_confidence)

    if conf_pct >= full_min:
        return 1.0, f"overnight_full_scale_{conf_pct:.0f}pct"
    if conf_pct >= half_min:
        return half_mult, f"overnight_half_scale_{conf_pct:.0f}pct"
    return 1.0, f"below_overnight_band_{conf_pct:.0f}pct"


def resolve_liquidity_wave(*, now: datetime | None = None) -> LiquidityWaveState:
    """Classify current UK liquidity phase and return modulation parameters."""
    dt = now or datetime.now(tz=_LONDON)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_LONDON)
    else:
        dt = dt.astimezone(_LONDON)
    mins = _minutes_since_midnight(dt)
    uk_label = dt.strftime("%H:%M:%S %Z")

    if _in_range(mins, 8, 0, 10, 0):
        return LiquidityWaveState(
            phase=LiquidityPhase.LONDON_OPEN,
            uk_time=uk_label,
            confidence_multiplier=1.35,
            autopilot_multiplier=PEAK_AUTOPILOT_MULTIPLIER,
            entry_premium_only=False,
            trend_following_focus=True,
        )
    if _in_range(mins, 14, 30, 16, 30):
        return LiquidityWaveState(
            phase=LiquidityPhase.NEW_YORK_OPEN,
            uk_time=uk_label,
            confidence_multiplier=1.40,
            autopilot_multiplier=PEAK_AUTOPILOT_MULTIPLIER,
            entry_premium_only=False,
            trend_following_focus=True,
        )
    if _in_range(mins, 16, 0, 17, 0):
        return LiquidityWaveState(
            phase=LiquidityPhase.LONDON_CLOSE,
            uk_time=uk_label,
            confidence_multiplier=1.30,
            autopilot_multiplier=PEAK_AUTOPILOT_MULTIPLIER,
            entry_premium_only=False,
            trend_following_focus=True,
        )
    if _in_range(mins, 11, 0, 13, 0):
        return LiquidityWaveState(
            phase=LiquidityPhase.MIDDAY_LULL,
            uk_time=uk_label,
            confidence_multiplier=LULL_CONFIDENCE_PENALTY,
            autopilot_multiplier=1.0,
            entry_premium_only=True,
            trend_following_focus=False,
        )

    if _in_range(mins, 1, 0, 4, 0):
        return LiquidityWaveState(
            phase=LiquidityPhase.TOKYO_MOMENTUM,
            uk_time=uk_label,
            confidence_multiplier=1.18,
            autopilot_multiplier=1.25,
            entry_premium_only=False,
            trend_following_focus=True,
        )

    return LiquidityWaveState(
        phase=LiquidityPhase.STANDARD,
        uk_time=uk_label,
        confidence_multiplier=1.0,
        autopilot_multiplier=1.0,
        entry_premium_only=False,
        trend_following_focus=False,
    )


def apply_microstructure_wave(
    confidence: float,
    regime: str,
    *,
    now: datetime | None = None,
) -> tuple[float, str]:
    """Inject time-of-day weight into microstructure confidence."""
    wave = resolve_liquidity_wave(now=now)
    mult = wave.confidence_multiplier
    if wave.trend_following_focus and regime in (
        "MOMENTUM_UP",
        "MOMENTUM_DOWN",
        "SWEEP_BUY",
        "SWEEP_SELL",
    ):
        mult *= 1.08
    adjusted = max(0.05, min(0.99, float(confidence) * mult))
    note = f"{wave.phase.value} x{mult:.2f}"
    return adjusted, note


def liquidity_wave_snapshot(*, now: datetime | None = None, config: Any | None = None) -> dict[str, Any]:
    wave = resolve_liquidity_wave(now=now)
    tokyo_active = in_tokyo_momentum_window(now=now, config=config)
    nikkei_floor, nikkei_reason = effective_microstructure_confidence_floor(
        NIKKEI_EPIC, now=now, config=config
    )
    gold_floor, gold_reason = effective_microstructure_confidence_floor(
        "CS.D.CFPGOLD.CFP.IP", now=now, config=config
    )
    return {
        "phase": wave.phase.value,
        "uk_time": wave.uk_time,
        "confidence_multiplier": wave.confidence_multiplier,
        "autopilot_multiplier": wave.autopilot_multiplier,
        "entry_premium_only": wave.entry_premium_only,
        "trend_following_focus": wave.trend_following_focus,
        "tokyo_momentum_active": tokyo_active,
        "session_micro_floor_nikkei_pct": round(nikkei_floor * 100.0, 1),
        "session_micro_floor_gold_pct": round(gold_floor * 100.0, 1),
        "session_floor_reason_nikkei": nikkei_reason,
        "session_floor_reason_gold": gold_reason,
    }
