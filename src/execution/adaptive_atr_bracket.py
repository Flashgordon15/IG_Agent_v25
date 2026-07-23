"""Adaptive ATR volatility bracket — entry TP/SL + size scale-down.

Replaces static micro TP/SL points with ATR-scaled distances during elevated
vol regimes, and shrinks lot size when vol_ratio exceeds a ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from execution.volatility_risk_bracket import (
    BracketConfig,
    BracketState,
    volatility_ratio,
)


@dataclass(frozen=True)
class AdaptiveBracketResult:
    tp_pts: float
    sl_pts: float
    size: float
    vol_ratio: float
    live_atr: float
    baseline_atr: float
    size_scale: float
    mode: str
    reason: str


def _vb_cfg(cfg: Any | None) -> dict[str, Any]:
    if cfg is None:
        try:
            from diagnostics.param_tuner import hot_section

            return hot_section("volatility_bracket")
        except Exception:
            return {}
    try:
        block = cfg.get("volatility_bracket") if hasattr(cfg, "get") else None
        base = dict(block) if isinstance(block, dict) else {}
        try:
            from diagnostics.param_tuner import merge_cfg_section

            return merge_cfg_section(cfg, "volatility_bracket") or base
        except Exception:
            return base
    except Exception:
        return {}


def _resolve_atrs(epic: str) -> tuple[float, float]:
    """Return (live_atr, baseline_atr) with safe floors."""
    live = 0.0
    baseline = 0.0
    try:
        from execution.risk_manager import resolve_atr_for_epic

        live = float(resolve_atr_for_epic(str(epic or "")) or 0)
        baseline = live
    except Exception:
        live = 0.0
    # Prefer longer baseline if hub/risk exposes it
    try:
        from system.market_data_hub import get_market_data_hub

        snap = get_market_data_hub().get_snapshot(str(epic or ""))
        if snap is not None:
            for attr in ("atr", "atr_14", "live_atr"):
                v = getattr(snap, attr, None)
                if v is not None and float(v) > 0:
                    live = float(v)
                    break
            for attr in ("atr_baseline", "atr_sma", "atr_avg"):
                v = getattr(snap, attr, None)
                if v is not None and float(v) > 0:
                    baseline = float(v)
                    break
    except Exception:
        pass
    live = max(live, 1e-6)
    baseline = max(baseline or live, 1e-6)
    return live, baseline


def size_scale_for_vol_ratio(vol_ratio: float, *, cfg: Any | None = None) -> float:
    """Scale size downward as vol expands past ceil; never scale up above 1.0."""
    block = _vb_cfg(cfg)
    ceil = float(block.get("size_scale_vol_ratio_ceil") or 1.35)
    floor_scale = float(block.get("size_scale_floor") or 0.35)
    if vol_ratio <= ceil:
        return 1.0
    # Linear shrink: at 2× ceil → ~floor_scale
    excess = vol_ratio / max(ceil, 1e-9)
    scale = 1.0 / max(excess, 1.0)
    return max(floor_scale, min(1.0, scale))


def fill_rate_size_scale(*, cfg: Any | None = None) -> float:
    """
    Additional size haircut from live fill-rate telemetry (no I/O).

    Rolling fill rate < 50% → ×0.80 to mitigate slippage drag.
    Preserves 3:1 R:R brackets (distances unchanged; only notional shrinks).
    """
    block = _vb_cfg(cfg)
    threshold = float(block.get("fill_rate_size_scale_threshold") or 0.50)
    haircut = float(block.get("fill_rate_size_scale") or 0.80)
    try:
        from diagnostics.fill_rate_monitor import get_fill_rate_monitor

        rate = get_fill_rate_monitor().rolling_fill_rate(20)
        if rate is not None and rate < threshold:
            return max(0.01, min(1.0, haircut))
    except Exception:
        pass
    return 1.0


def resolve_adaptive_entry_bracket(
    epic: str,
    direction: str,
    size: float,
    cfg: Any | None,
    *,
    entry: float | None = None,
    volatility_z: float | None = None,
) -> AdaptiveBracketResult:
    """
    Compute ATR-adaptive TP/SL points and volatility-scaled size.

    Falls back to micro_risk GBP distances when ATR unavailable / disabled.
    """
    from execution.micro_risk_profile import resolve_micro_tp_sl_for_epic

    block = _vb_cfg(cfg)
    enabled = bool(block.get("enabled", True))
    base_tp, base_sl, _prof = resolve_micro_tp_sl_for_epic(
        epic, size, cfg, volatility_z=volatility_z
    )

    if not enabled:
        return AdaptiveBracketResult(
            tp_pts=base_tp,
            sl_pts=base_sl,
            size=float(size),
            vol_ratio=1.0,
            live_atr=0.0,
            baseline_atr=0.0,
            size_scale=1.0,
            mode="static_micro_risk",
            reason="volatility_bracket_off",
        )

    live_atr, baseline_atr = _resolve_atrs(epic)
    vr = volatility_ratio(live_atr, baseline_atr)
    scale = size_scale_for_vol_ratio(vr, cfg=cfg)
    fr_scale = fill_rate_size_scale(cfg=cfg)
    combined_scale = max(0.01, min(1.0, float(scale) * float(fr_scale)))
    sized = max(0.01, float(size) * combined_scale)

    # Recompute GBP TP/SL on the scaled size so risk_per_trade stays honest.
    tp_pts, sl_pts, _ = resolve_micro_tp_sl_for_epic(
        epic, sized, cfg, volatility_z=volatility_z
    )

    # Blend ATR bracket distances when we have a usable entry/mid.
    mid = float(entry or 0)
    if mid <= 0:
        try:
            from system.market_data_hub import get_market_data_hub

            snap = get_market_data_hub().get_snapshot(str(epic or ""))
            if snap is not None:
                b = float(getattr(snap, "bid", 0) or 0)
                o = float(getattr(snap, "offer", 0) or 0)
                if b > 0 and o > b:
                    mid = (b + o) / 2.0
        except Exception:
            mid = 0.0

    mode = "micro_risk_vol_scaled"
    reason = f"vol_ratio={vr:.2f} scale={combined_scale:.2f}"
    if fr_scale < 1.0 - 1e-12:
        reason += f" fill_rate_haircut={fr_scale:.2f}"
    vol_ceil = float(block.get("size_scale_vol_ratio_ceil") or 1.35)
    # Cap RR — 3.5× TP vs 1.0× SL ignores 0.5×spread IOC slip (expectancy leak).
    rr_target = min(2.75, float(block.get("elevated_vol_reward_risk") or 2.75))
    slip_haircut = max(0.0, float(block.get("slip_rr_haircut") or 0.25))
    from execution.micro_risk_profile import _load_profile

    prof = _load_profile(cfg)
    max_sl_cap = float(prof.max_loss_cap_pts)

    if mid > 0 and live_atr > 0 and vr >= vol_ceil:
        # Asymmetric R:R in elevated vol: keep SL tight (never widen),
        # scale TP out to slip-aware reward:risk (≤2.75 raw − haircut).
        tight_sl = min(float(sl_pts), float(base_sl), max_sl_cap)
        tight_sl = max(0.5, tight_sl)
        effective_rr = max(1.5, rr_target - slip_haircut)
        tp_pts = max(float(prof.min_profit_target_pts), tight_sl * effective_rr)
        sl_pts = tight_sl
        mode = "asymmetric_rr_elevated_vol"
        reason = (
            f"vol_ratio={vr:.2f} scale={combined_scale:.2f} "
            f"rr={effective_rr:.2f}:1 (raw={rr_target:.2f}-slip={slip_haircut:.2f}) "
            f"tight_sl={tight_sl:.2f}"
        )
        if fr_scale < 1.0 - 1e-12:
            reason += f" fill_rate_haircut={fr_scale:.2f}"
    elif mid > 0 and live_atr > 0:
        bcfg = BracketConfig(
            initial_stop_atr_mult=float(
                block.get("initial_stop_atr_mult") or 2.5
            ),
            base_trail_atr_mult=float(block.get("base_trail_atr_mult") or 2.0),
        )
        dir_u = str(direction or "BUY").upper()
        state = (
            BracketState.open_long(
                entry=mid, entry_atr=live_atr, baseline_atr=baseline_atr, cfg=bcfg
            )
            if dir_u == "BUY"
            else BracketState.open_short(
                entry=mid, entry_atr=live_atr, baseline_atr=baseline_atr, cfg=bcfg
            )
        )
        atr_sl = abs(mid - state.stop)
        atr_tp = abs(state.target - mid)
        # Mild vol only — light blend; never apply in elevated branch above
        blend = min(1.0, max(0.0, (vr - 1.0) / 1.5))
        sl_pts = (1.0 - blend) * sl_pts + blend * atr_sl
        tp_pts = (1.0 - blend) * tp_pts + blend * atr_tp
        sl_pts = min(max_sl_cap, max(0.5, sl_pts))
        tp_pts = max(float(prof.min_profit_target_pts), tp_pts)
        mode = "atr_blend" if blend > 0.05 else "micro_risk_vol_scaled"

    return AdaptiveBracketResult(
        tp_pts=round(float(tp_pts), 3),
        sl_pts=round(float(sl_pts), 3),
        size=round(float(sized), 4),
        vol_ratio=round(float(vr), 4),
        live_atr=round(float(live_atr), 6),
        baseline_atr=round(float(baseline_atr), 6),
        size_scale=round(float(combined_scale), 4),
        mode=mode,
        reason=reason,
    )
