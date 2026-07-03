"""
IG order size validation — market constraints, canary caps, self-correction.

Integrates with unified runtime state and broker reject guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from system.engine_log import log_engine


@dataclass(frozen=True)
class SizeValidationResult:
    ok: bool
    adjusted_size: float
    reason: str
    ig_min_deal: float
    original_size: float = 0.0
    step_size: float = 0.0
    max_deal: float | None = None


def _canary_cap(epic: str, cfg: Any | None) -> float | None:
    try:
        from runtime.dual_core_execution import canary_lot_size

        return float(canary_lot_size(epic, cfg))
    except Exception:
        return None


def _config_max(epic: str, cfg: Any | None) -> float | None:
    if cfg is None:
        return None
    try:
        raw = getattr(cfg, "max_deal_size", None)
        if raw is not None:
            return float(raw)
        dual = getattr(cfg, "dual_core", None)
        if isinstance(dual, dict):
            mx = dual.get("max_micro_lot")
            if mx is not None:
                return float(mx)
    except Exception:
        pass
    return None


def _round_to_step(size: float, step: float) -> float:
    if step <= 0:
        return size
    steps = round(size / step)
    return max(step, steps * step)


def validate_order_size(
    epic: str,
    size: float,
    direction: str,
    cfg: Any | None,
    rest_client: Any | None,
    *,
    broker_epic: str | None = None,
) -> SizeValidationResult:
    """
    Validate and normalize order size against IG constraints and canary caps.

    Fetches/caches market constraints via rest_client when available.
    """
    _ = direction
    original = float(size)
    bepic = str(broker_epic or epic or "").strip()
    from execution.size_floors import effective_min_deal_size, hard_min_deal_size

    hard_min = hard_min_deal_size(epic, cfg=cfg)
    min_deal = hard_min
    max_deal: float | None = None
    step = 0.0
    rules_loaded = hard_min > 0

    if rest_client is not None and bepic:
        try:
            constraints = rest_client.fetch_market_constraints(bepic)
            rest_min = float(constraints.get("min_deal_size") or 0.0)
            min_deal = effective_min_deal_size(epic, cfg=cfg, rest_min=rest_min)
            max_deal_raw = constraints.get("max_deal_size")
            if max_deal_raw is not None:
                max_deal = float(max_deal_raw)
            step = float(constraints.get("deal_increment") or constraints.get("step") or 0.0)
            rules_loaded = True
            try:
                from system.unified_runtime_state import update_sizing

                update_sizing(rules_loaded=True)
            except Exception:
                pass
        except Exception as exc:
            log_engine(
                f"SizeValidator: constraints fetch skipped epic={bepic} "
                f"{type(exc).__name__}: {exc}"
            )

    adjusted = max(original, min_deal) if min_deal > 0 else original
    reasons: list[str] = []

    canary = _canary_cap(epic, cfg)
    if canary is not None and adjusted > canary:
        if canary < min_deal:
            reasons.append(f"canary_below_ig_min:canary={canary},min={min_deal}")
            adjusted = min_deal
        else:
            adjusted = canary
            reasons.append(f"canary_cap={canary}")

    cfg_max = _config_max(epic, cfg)
    if cfg_max is not None and adjusted > cfg_max:
        adjusted = cfg_max
        reasons.append(f"config_max={cfg_max}")

    if min_deal > 0 and adjusted < min_deal:
        adjusted = min_deal
        if "ig_min_deal=" not in "; ".join(reasons):
            reasons.append(f"ig_min_deal={min_deal}")

    if step > 0:
        stepped = _round_to_step(adjusted, step)
        if stepped != adjusted:
            adjusted = stepped
            reasons.append(f"step={step}")

    if max_deal is not None and max_deal > 0 and adjusted > max_deal:
        adjusted = max_deal
        reasons.append(f"ig_max_deal={max_deal}")

    ok = adjusted > 0
    reason = "; ".join(reasons) if reasons else ("ok" if ok else "invalid_size")

    result = SizeValidationResult(
        ok=ok,
        adjusted_size=round(adjusted, 4),
        reason=reason,
        ig_min_deal=min_deal,
        original_size=original,
        step_size=step,
        max_deal=max_deal,
    )

    try:
        from system.unified_runtime_state import update_sizing

        update_sizing(
            rules_loaded=rules_loaded,
            epic=epic,
            validation={
                "ok": result.ok,
                "adjusted_size": result.adjusted_size,
                "reason": result.reason,
                "ig_min_deal": result.ig_min_deal,
                "original_size": result.original_size,
            },
        )
    except Exception:
        pass

    return result


def pre_trade_check(
    instrument: str,
    size: float,
    direction: str,
    cfg: Any | None,
    rest_client: Any | None,
    *,
    broker_epic: str | None = None,
) -> dict[str, Any]:
    """
    Pre-trade validation contract: ok / adjusted / blocked.

    Returns dict with keys: status ('ok'|'adjusted'|'blocked'), adjusted_size, reason.
    """
    result = validate_order_size(
        instrument,
        size,
        direction,
        cfg,
        rest_client,
        broker_epic=broker_epic,
    )
    if not result.ok:
        return {
            "status": "blocked",
            "adjusted_size": result.adjusted_size,
            "reason": result.reason,
            "ig_min_deal": result.ig_min_deal,
        }
    status = "ok"
    if result.adjusted_size != result.original_size:
        status = "adjusted"
    return {
        "status": status,
        "adjusted_size": result.adjusted_size,
        "reason": result.reason,
        "ig_min_deal": result.ig_min_deal,
    }


def classify_size_rejection(reason: str) -> bool:
    key = str(reason or "").upper()
    return "MINIMUM_ORDER_SIZE" in key or "MINIMUM DEAL" in key


@dataclass(frozen=True)
class ExecutableLotResult:
    ok: bool
    size: float
    rejection_reason: str
    validation: SizeValidationResult | None = None


def fractional_lot_execution_enabled(cfg: Any | None = None) -> bool:
    """Spreadbet / demo throughput accepts fractional £/point sizes (not integer lots)."""
    try:
        from system.soak_live_fire import soak_mode_enabled
        from trading.live_production_probe import live_probe_enabled

        if soak_mode_enabled() or live_probe_enabled():
            return True
    except Exception:
        pass
    try:
        from trading.micro_lot_verification import micro_lot_verification_enabled

        if micro_lot_verification_enabled():
            return True
    except Exception:
        pass
    try:
        from system.demo_execution_plane import demo_throughput_active

        if demo_throughput_active(cfg):
            return True
    except Exception:
        pass
    try:
        if cfg is None:
            from system.config_loader import get_config

            cfg = get_config()
        dual = cfg.get("dual_core") if hasattr(cfg, "get") else getattr(cfg, "dual_core", {})
        if isinstance(dual, dict) and str(dual.get("broker_account_product", "")).upper() == "SPREADBET":
            return True
    except Exception:
        pass
    return False


def resolve_executable_lot_size(
    epic: str,
    size: float,
    direction: str,
    cfg: Any | None,
    rest_client: Any | None,
    *,
    broker_epic: str | None = None,
) -> ExecutableLotResult:
    """
    Normalize size against IG constraints and platform lot rules.

    Spreadbet / demo throughput keeps fractional sizes (e.g. 0.5 £/pt).
    CFD-style paths integer-floor to whole lots (min 1).
    """
    validated = validate_order_size(
        epic,
        size,
        direction,
        cfg,
        rest_client,
        broker_epic=broker_epic,
    )
    adjusted = float(validated.adjusted_size)
    if not validated.ok or adjusted <= 0:
        return ExecutableLotResult(
            ok=False,
            size=0.0,
            rejection_reason="HOLD: INVALID_SIZE",
            validation=validated,
        )

    if fractional_lot_execution_enabled(cfg):
        return ExecutableLotResult(
            ok=True,
            size=round(adjusted, 4),
            rejection_reason="",
            validation=validated,
        )

    from apex.hardening import floor_contract_size, under_min_lot_detail

    size_int, under_min = floor_contract_size(adjusted)
    if under_min:
        log_engine(under_min_lot_detail(size_int))
        return ExecutableLotResult(
            ok=False,
            size=0.0,
            rejection_reason="HOLD: UNDER_MIN_LOT",
            validation=validated,
        )
    return ExecutableLotResult(
        ok=True,
        size=float(size_int),
        rejection_reason="",
        validation=validated,
    )
