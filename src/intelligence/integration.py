"""Execution-engine pre-dispatch intelligence merge."""

from __future__ import annotations

from typing import Any

from intelligence.policy import intelligence_enabled


def apply_intelligence_pre_dispatch(
    signal: Any,
    execution_params: dict[str, Any],
    *,
    config: Any | None,
) -> tuple[dict[str, Any], str | None]:
    """
    Merge intelligence adjustments into execution_params before broker dispatch.

    Returns (updated_params, rejection_reason).
    """
    if not intelligence_enabled(config):
        return execution_params, None

    epic = str(getattr(signal, "epic", "") or "")
    if not epic:
        return execution_params, None

    try:
        from intelligence.pipeline_bridge import get_intelligence_layer

        adj = get_intelligence_layer().execution_adjustments(epic)
    except Exception:
        return execution_params, None

    merged = dict(execution_params)
    merged.update(adj)

    if adj.get("intelligence_spread_blocked"):
        return merged, (
            f"INTELLIGENCE_SPREAD_BLOCK z={adj.get('intelligence_spread_z', 0):.2f}"
        )

    try:
        from intelligence.liquidity_wave import (
            LULL_CONFIDENCE_FLOOR,
            effective_microstructure_confidence_floor,
            in_tokyo_momentum_window,
            overnight_volatility_size_multiplier,
            resolve_liquidity_wave,
        )

        from intelligence.premium_overnight import (
            in_overnight_liquidity_window,
            is_premium_overnight_epic,
            premium_overnight_momentum_pass,
        )

        wave = resolve_liquidity_wave()
        micro_conf = float(adj.get("intelligence_micro_confidence") or 0.0)
        micro_regime = str(adj.get("intelligence_micro_regime") or "")
        session_floor, session_floor_reason = effective_microstructure_confidence_floor(
            epic, config=config
        )
        merged["session_micro_floor"] = session_floor
        merged["session_micro_floor_reason"] = session_floor_reason
        merged["tokyo_momentum_active"] = in_tokyo_momentum_window(config=config)

        overnight_pass = premium_overnight_momentum_pass(
            epic,
            micro_regime,
            micro_conf,
            config=config,
        )
        premium_epic = is_premium_overnight_epic(epic, config)
        in_overnight = in_overnight_liquidity_window(config=config)
        momentum_regimes = (
            "MOMENTUM_UP",
            "MOMENTUM_DOWN",
            "SWEEP_BUY",
            "SWEEP_SELL",
            "ORDER_BLOCK",
        )

        if session_floor > 0 and premium_epic and (in_overnight or merged["tokyo_momentum_active"]):
            if micro_regime not in momentum_regimes:
                return merged, f"SESSION_PREMIUM regime={micro_regime}"
            if micro_conf < session_floor:
                return merged, (
                    f"SESSION_MICRO_FLOOR conf={micro_conf:.0%}<{session_floor:.0%} "
                    f"({session_floor_reason})"
                )
            merged["session_premium_clearance"] = True

        if wave.entry_premium_only:
            if overnight_pass:
                merged["premium_overnight_momentum"] = True
                merged["premium_overnight_confidence_floor"] = micro_conf
            elif micro_conf < LULL_CONFIDENCE_FLOOR:
                return merged, (
                    f"LIQUIDITY_LULL_PREMIUM conf={micro_conf:.0%}<{LULL_CONFIDENCE_FLOOR:.0%}"
                )
            elif micro_regime not in momentum_regimes:
                return merged, f"LIQUIDITY_LULL_PREMIUM regime={micro_regime}"
        elif overnight_pass and in_overnight:
            merged["premium_overnight_momentum"] = True
        merged["liquidity_wave_phase"] = wave.phase.value

        size_mult, size_reason = overnight_volatility_size_multiplier(
            micro_conf, epic=epic, config=config
        )
        if size_mult < 1.0:
            try:
                size = float(merged.get("size") or 0.0)
                if size > 0:
                    merged["size"] = max(0.01, size * size_mult)
                    merged["overnight_volatility_size_multiplier"] = size_mult
                    merged["overnight_volatility_size_reason"] = size_reason
            except (TypeError, ValueError):
                pass
    except Exception:
        pass

    throttle = float(adj.get("intelligence_throttle_factor") or 0.0)
    if throttle > 0:
        try:
            size = float(merged.get("size") or 0.0)
            if size > 0:
                merged["size"] = max(0.01, size * (1.0 - min(0.95, throttle)))
                merged["intelligence_size_throttled"] = True
        except (TypeError, ValueError):
            pass

    widen = float(adj.get("intelligence_offset_widen_pts") or 0.0)
    if widen > 0:
        for key in ("risk", "stop_distance", "stop_pts"):
            try:
                base = float(merged.get(key) or 0.0)
                if base > 0:
                    merged[key] = base + widen
                    break
            except (TypeError, ValueError):
                continue
        merged["intelligence_offset_widen_pts"] = widen

    try:
        from intelligence.target_engine import apply_target_execution_adjustments

        merged, target_reject = apply_target_execution_adjustments(
            merged, config=config
        )
        if target_reject:
            return merged, target_reject
    except Exception:
        pass

    return merged, None
