"""V37 dual-regime isolation — QUANT_SNIPER vs MACRO_SENTINEL.

Keeps CFD microstructure (ElasticGate / OBI scalp) mathematically independent
from SB macro / Trend-Retention state. Mutable gate and ML override arrays are
**engine-keyed** so a CFD scalp fill sequence cannot overwrite SB sentiment.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any

from system.engine_lane import (
    DEFAULT_ACCOUNT_CFD,
    DEFAULT_ACCOUNT_SB,
    ENGINE_ORIGIN_CFD,
    ENGINE_ORIGIN_SB,
    infer_engine_id,
    resolve_active_engine_id,
)

# Re-export origins for callers / tests.
QUANT_SNIPER = ENGINE_ORIGIN_CFD
MACRO_SENTINEL = ENGINE_ORIGIN_SB

_LOCK = threading.RLock()

# Engine-scoped mutable stores — never a single shared dict for both desks.
_ml_overrides_by_engine: dict[str, dict[str, Any]] = {}
_sniper_gate_by_engine: dict[str, dict[str, Any]] = {}
_macro_sentiment_by_engine: dict[str, dict[str, Any]] = {}


def reset_dual_regime_for_tests() -> None:
    with _LOCK:
        _ml_overrides_by_engine.clear()
        _sniper_gate_by_engine.clear()
        _macro_sentiment_by_engine.clear()


def _cfg_block(cfg: Any | None, key: str) -> dict[str, Any]:
    if cfg is None:
        return {}
    try:
        if isinstance(cfg, dict):
            raw = cfg.get(key)
        else:
            raw = getattr(cfg, key, None)
            if raw is None and hasattr(cfg, "get"):
                raw = cfg.get(key)
        return dict(raw) if isinstance(raw, dict) else {}
    except Exception:
        return {}


def normalize_engine_origin(
    engine_origin: str | None = None,
    *,
    account_id: str | None = None,
    cfg: Any | None = None,
) -> str:
    """Resolve QUANT_SNIPER | MACRO_SENTINEL from explicit args / env / account."""
    origin = str(engine_origin or "").strip().upper()
    if origin in (QUANT_SNIPER, MACRO_SENTINEL):
        return origin
    acct = str(account_id or os.environ.get("IG_ACCOUNT_ID") or "").strip().upper()
    if acct == DEFAULT_ACCOUNT_CFD:
        return QUANT_SNIPER
    if acct == DEFAULT_ACCOUNT_SB:
        return MACRO_SENTINEL
    env_origin = str(os.environ.get("IG_ENGINE_ORIGIN") or "").strip().upper()
    if env_origin in (QUANT_SNIPER, MACRO_SENTINEL):
        return env_origin
    eid = infer_engine_id(account_id=acct or None, cfg=cfg)
    if eid == "cfd_sniper" or resolve_active_engine_id(cfg) == "cfd_sniper":
        # Prefer CFD only when account/env already implied it.
        if acct == DEFAULT_ACCOUNT_CFD or env_origin == QUANT_SNIPER:
            return QUANT_SNIPER
    if eid == "cfd_sniper" and (
        acct == DEFAULT_ACCOUNT_CFD
        or str(os.environ.get("IG_ACTIVE_ENGINE_ID") or "") == "cfd_sniper"
    ):
        return QUANT_SNIPER
    if eid == "cfd_sniper" and not acct and not env_origin:
        # Dual-port CFD process typically sets env; without it default SB-safe.
        pass
    if eid == "cfd_sniper":
        return QUANT_SNIPER
    return MACRO_SENTINEL


def dual_regime_enabled(cfg: Any | None = None) -> bool:
    block = _cfg_block(cfg, "dual_regime")
    if not block:
        return True  # isolation on by default once module is loaded
    return bool(block.get("enabled", True))


def elastic_gate_owner(cfg: Any | None = None) -> str:
    """ElasticGate knobs are CFD-owned (config; not hard-calibrated here)."""
    block = _cfg_block(cfg, "dual_regime")
    owner = str(block.get("elastic_gate_owner") or QUANT_SNIPER).strip().upper()
    return owner if owner in (QUANT_SNIPER, MACRO_SENTINEL) else QUANT_SNIPER


def elastic_gate_applies(
    *,
    engine_origin: str | None = None,
    account_id: str | None = None,
    cfg: Any | None = None,
) -> bool:
    """True for CFD / unspecified sniper callers; False for explicit SB lane.

    ElasticGate knobs are CFD-owned. Legacy callers that omit engine_origin keep
    ElasticGate (selectivity is sniper-oriented). Only MACRO_SENTINEL / Z6BAH3
    (or SB dual-port env) skip the HF OBI ElasticGate path.
    """
    if not dual_regime_enabled(cfg):
        return True
    owner = elastic_gate_owner(cfg)
    origin = str(engine_origin or "").strip().upper()
    acct = str(account_id or "").strip().upper()
    if origin == MACRO_SENTINEL or acct == DEFAULT_ACCOUNT_SB:
        return owner == MACRO_SENTINEL
    if origin == QUANT_SNIPER or acct == DEFAULT_ACCOUNT_CFD:
        return owner == QUANT_SNIPER
    env_origin = str(os.environ.get("IG_ENGINE_ORIGIN") or "").strip().upper()
    env_acct = str(os.environ.get("IG_ACCOUNT_ID") or "").strip().upper()
    if env_origin == MACRO_SENTINEL or env_acct == DEFAULT_ACCOUNT_SB:
        return owner == MACRO_SENTINEL
    if env_origin == QUANT_SNIPER or env_acct == DEFAULT_ACCOUNT_CFD:
        return owner == QUANT_SNIPER
    # Unspecified → CFD ElasticGate remains active (sniper selectivity default).
    return owner == QUANT_SNIPER


def sb_forbid_obi_velocity_scalp(cfg: Any | None = None) -> bool:
    block = _cfg_block(cfg, "dual_regime")
    return bool(block.get("sb_forbid_obi_velocity_scalp", True))


def sb_disable_instant_micro(cfg: Any | None = None) -> bool:
    """MACRO_SENTINEL must not fire Instant / micro_scalp_instant tick lane."""
    block = _cfg_block(cfg, "dual_regime")
    if "sb_disable_instant_micro" in block:
        return bool(block.get("sb_disable_instant_micro"))
    # Default ON when dual_regime enabled — SB Instant was the daylight bleed path.
    return bool(block.get("enabled", True))


def sb_disable_core_b_micro(cfg: Any | None = None) -> bool:
    """MACRO_SENTINEL must not arm ENGINE_B_MICRO_SCALPER / Core-B pierce."""
    block = _cfg_block(cfg, "dual_regime")
    if "sb_disable_core_b_micro" in block:
        return bool(block.get("sb_disable_core_b_micro"))
    return bool(block.get("enabled", True))


def sb_macro_ltr_entries_only(cfg: Any | None = None) -> bool:
    """When true, SB new entries only via macro/directional + LTR path."""
    block = _cfg_block(cfg, "dual_regime")
    if "sb_macro_ltr_entries_only" in block:
        return bool(block.get("sb_macro_ltr_entries_only"))
    return sb_disable_instant_micro(cfg) and sb_disable_core_b_micro(cfg)


def sb_macro_path_a_carve_active(
    *,
    engine_origin: str | None = None,
    account_id: str | None = None,
    cfg: Any | None = None,
) -> bool:
    """SB MACRO may use PATH_A / TradingLoop even when SCALP owns the epic.

    Instant / Core-B micro stay hard-disabled via ``allow_engine_micro_scalp_path``.
    This only prevents SCALP ownership from hard/soft-blocking Path A on
    MACRO_SENTINEL when ``sb_macro_ltr_entries_only`` is set.
    """
    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            cfg = None
    origin = normalize_engine_origin(
        engine_origin, account_id=account_id, cfg=cfg
    )
    if origin != MACRO_SENTINEL:
        return False
    if not dual_regime_enabled(cfg):
        return False
    return sb_macro_ltr_entries_only(cfg)


def allow_obi_velocity_scalp_trigger(
    *,
    engine_origin: str | None = None,
    account_id: str | None = None,
    cfg: Any | None = None,
) -> bool:
    """HF OBI-velocity scalp triggers are CFD-only when dual_regime is on."""
    origin = normalize_engine_origin(engine_origin, account_id=account_id, cfg=cfg)
    if origin == QUANT_SNIPER:
        return True
    if sb_forbid_obi_velocity_scalp(cfg):
        return False
    return True


def allow_engine_micro_scalp_path(
    path: str | None,
    *,
    engine_origin: str | None = None,
    account_id: str | None = None,
    cfg: Any | None = None,
) -> tuple[bool, str]:
    """Gate Instant / Core-B micro by engine.

    CFD (QUANT_SNIPER): always allow micro paths (caller may still be paused).
    SB (MACRO_SENTINEL): reject Instant / Core-B micro when dual_regime flags set;
    long_trade_runner / other non-scalp paths remain allowed.
    """
    origin = normalize_engine_origin(engine_origin, account_id=account_id, cfg=cfg)
    p = str(path or "").strip().lower()
    is_instant = p in (
        "instant",
        "instant_scalp",
        "micro_scalp_instant",
        "predictive_micro_scalp",
    )
    is_core_b_micro = p in (
        "micro",
        "micro_scalp",
        "core_b_micro",
        "core_b",
        "engine_b_micro_scalper",
        "piercing_zone",
        "parallel_strategy_sweep",
    )
    is_ltr = p in (
        "long_trade_runner",
        "long_runner",
        "ltr",
        "macro_long",
        "macro",
        "directional",
        "signal_engine",
        "trading_loop",
    )

    if origin == QUANT_SNIPER:
        return True, "cfd_micro_path_allowed"

    if not dual_regime_enabled(cfg):
        return True, "dual_regime_disabled"

    if is_instant and sb_disable_instant_micro(cfg):
        return False, "sb_instant_micro_hard_disabled"
    if is_core_b_micro and sb_disable_core_b_micro(cfg):
        return False, "sb_core_b_micro_hard_disabled"
    if sb_macro_ltr_entries_only(cfg) and (is_instant or is_core_b_micro):
        return False, "sb_macro_ltr_entries_only"
    if sb_macro_ltr_entries_only(cfg) and is_ltr:
        return True, "sb_macro_ltr_path_ok"
    if is_instant or is_core_b_micro:
        # Flags off → allow (legacy soak).
        return True, "sb_micro_path_allowed"
    return True, "sb_non_micro_path_ok"


# ---------------------------------------------------------------------------
# Engine-scoped mutable stores
# ---------------------------------------------------------------------------


def apply_ml_overrides_for_engine(
    engine_origin: str,
    overrides: dict[str, Any],
    *,
    epic: str | None = None,
) -> None:
    """Write ML / gate overrides for one engine only."""
    origin = normalize_engine_origin(engine_origin)
    body = dict(overrides or {})
    if epic:
        body["_epic"] = str(epic)
    with _LOCK:
        _ml_overrides_by_engine[origin] = body


def get_ml_overrides_for_engine(engine_origin: str | None = None) -> dict[str, Any]:
    origin = normalize_engine_origin(engine_origin)
    with _LOCK:
        return dict(_ml_overrides_by_engine.get(origin) or {})


def apply_sniper_gate_state(
    *,
    engine_origin: str = QUANT_SNIPER,
    epic: str,
    payload: dict[str, Any],
) -> None:
    """CFD scalp fill / gate sequence — must not touch SB macro sentiment."""
    origin = normalize_engine_origin(engine_origin)
    key = str(epic or "").strip()
    if not key:
        return
    with _LOCK:
        lane = _sniper_gate_by_engine.setdefault(origin, {})
        lane[key] = dict(payload or {})


def get_sniper_gate_state(
    *,
    engine_origin: str = QUANT_SNIPER,
    epic: str,
) -> dict[str, Any]:
    origin = normalize_engine_origin(engine_origin)
    key = str(epic or "").strip()
    with _LOCK:
        return dict((_sniper_gate_by_engine.get(origin) or {}).get(key) or {})


def apply_macro_sentiment(
    *,
    engine_origin: str = MACRO_SENTINEL,
    epic: str,
    sentiment: dict[str, Any],
) -> None:
    """SB macro / directional sentiment — isolated from CFD sniper fills."""
    origin = normalize_engine_origin(engine_origin)
    key = str(epic or "").strip()
    if not key:
        return
    with _LOCK:
        lane = _macro_sentiment_by_engine.setdefault(origin, {})
        lane[key] = dict(sentiment or {})


def get_macro_sentiment(
    *,
    engine_origin: str = MACRO_SENTINEL,
    epic: str,
) -> dict[str, Any]:
    origin = normalize_engine_origin(engine_origin)
    key = str(epic or "").strip()
    with _LOCK:
        return dict((_macro_sentiment_by_engine.get(origin) or {}).get(key) or {})


def simulate_cfd_scalp_fill_sequence(
    *,
    epic: str,
    ml_overrides: dict[str, Any] | None = None,
    gate_payload: dict[str, Any] | None = None,
) -> None:
    """In-memory CFD scalp fill path used by isolation tests."""
    apply_ml_overrides_for_engine(
        QUANT_SNIPER, dict(ml_overrides or {"micro_z_threshold": 0.11}), epic=epic
    )
    apply_sniper_gate_state(
        engine_origin=QUANT_SNIPER,
        epic=epic,
        payload=dict(
            gate_payload
            or {
                "last_fill": "scalp",
                "obi_velocity": 0.42,
                "p_success": 0.71,
            }
        ),
    )


# ---------------------------------------------------------------------------
# Exit matrices
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExitMatrixDecision:
    engine_origin: str
    mode: str  # "cfd_scalp" | "sb_trend_retention" | "neutral"
    stop_floor_pts: float | None
    skip_micro_trails: bool
    floor_breakeven_plus: bool
    breakeven_offset_pts: float
    giveback_ratio: float | None
    reason: str


def trend_retention_block(cfg: Any | None = None) -> dict[str, Any]:
    dual = _cfg_block(cfg, "dual_regime")
    tr = dual.get("trend_retention")
    if isinstance(tr, dict):
        return dict(tr)
    # Fall back to profit_run knobs when dual_regime.trend_retention absent.
    pr = _cfg_block(cfg, "profit_run")
    return {
        "upl_threshold_gbp": float(pr.get("upl_threshold_gbp") or 15.0),
        "breakeven_offset_pts": float(pr.get("breakeven_offset_pts") or 1.0),
        "giveback_ratio": 0.20,
    }


def cfd_scalp_stop_floor_pts(cfg: Any | None = None) -> float:
    mr = _cfg_block(cfg, "micro_risk")
    try:
        return float(mr.get("dow_broker_stop_floor_pts") or 12.0)
    except (TypeError, ValueError):
        return 12.0


def evaluate_exit_matrix(
    *,
    engine_origin: str | None = None,
    account_id: str | None = None,
    unrealized_pnl_gbp: float | None = None,
    cfg: Any | None = None,
) -> ExitMatrixDecision:
    """CFD: 12pt scalp floor + trails active.

    SB Trend-Retention: UPL≥£15 → kill micro trails → BE+1 → ~20% peak giveback.
    """
    origin = normalize_engine_origin(engine_origin, account_id=account_id, cfg=cfg)
    floor = cfd_scalp_stop_floor_pts(cfg)

    if origin == QUANT_SNIPER:
        return ExitMatrixDecision(
            engine_origin=origin,
            mode="cfd_scalp",
            stop_floor_pts=max(12.0, floor),
            skip_micro_trails=False,
            floor_breakeven_plus=False,
            breakeven_offset_pts=0.0,
            giveback_ratio=None,
            reason="cfd_scalp_12pt_floor",
        )

    tr = trend_retention_block(cfg)
    thr = float(tr.get("upl_threshold_gbp") or 15.0)
    offset = float(tr.get("breakeven_offset_pts") or 1.0)
    giveback = float(tr.get("giveback_ratio") or 0.20)
    try:
        upl = float(unrealized_pnl_gbp) if unrealized_pnl_gbp is not None else None
    except (TypeError, ValueError):
        upl = None

    # profit_run must be enabled for Trend-Retention to arm (preserve hard stops).
    pr_on = bool(_cfg_block(cfg, "profit_run").get("enabled", False))
    if pr_on and upl is not None and upl >= thr:
        return ExitMatrixDecision(
            engine_origin=origin,
            mode="sb_trend_retention",
            stop_floor_pts=None,
            skip_micro_trails=True,
            floor_breakeven_plus=True,
            breakeven_offset_pts=offset,
            giveback_ratio=giveback,
            reason=f"sb_trend_retention upl={upl:.2f}>={thr:.2f} giveback={giveback:.2f}",
        )

    return ExitMatrixDecision(
        engine_origin=origin,
        mode="neutral",
        stop_floor_pts=None,
        skip_micro_trails=False,
        floor_breakeven_plus=False,
        breakeven_offset_pts=offset,
        giveback_ratio=None,
        reason="sb_below_trend_retention_threshold",
    )


def resolve_engine_regime_config(
    cfg: Any | None,
    *,
    engine_origin: str | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Shallow view of regime-owned knobs for the active engine (read-only merge)."""
    origin = normalize_engine_origin(engine_origin, account_id=account_id, cfg=cfg)
    base: dict[str, Any] = {}
    if isinstance(cfg, dict):
        base = dict(cfg)
    elif cfg is not None and hasattr(cfg, "get"):
        try:
            base = dict(cfg)  # type: ignore[arg-type]
        except Exception:
            base = {}
    dual = _cfg_block(cfg, "dual_regime")
    overlay = dual.get("cfd") if origin == QUANT_SNIPER else dual.get("sb")
    if isinstance(overlay, dict):
        merged = dict(base)
        for k, v in overlay.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                child = dict(merged[k])
                child.update(v)
                merged[k] = child
            else:
                merged[k] = v
        base = merged
    base["_dual_regime_engine_origin"] = origin
    base["_elastic_gate_applies"] = elastic_gate_applies(
        engine_origin=origin, cfg=cfg
    )
    return base
