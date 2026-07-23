"""v32 dual-engine lane metadata — accounting columns and per-engine position caps."""

from __future__ import annotations

import os
from typing import Any

ENGINE_CFD_SNIPER = "cfd_sniper"
ENGINE_SB_SENTINEL = "sb_sentinel"

ENGINE_ORIGIN_CFD = "QUANT_SNIPER"
ENGINE_ORIGIN_SB = "MACRO_SENTINEL"

DEFAULT_ACCOUNT_CFD = "Z6BAH4"
DEFAULT_ACCOUNT_SB = "Z6BAH3"
DEFAULT_PRODUCT_CFD = "CFD"
DEFAULT_PRODUCT_SB = "SPREADBET"

_LANE_DEFAULTS: dict[str, dict[str, str]] = {
    ENGINE_CFD_SNIPER: {
        "account_id": DEFAULT_ACCOUNT_CFD,
        "product_type": DEFAULT_PRODUCT_CFD,
        "engine_origin": ENGINE_ORIGIN_CFD,
    },
    ENGINE_SB_SENTINEL: {
        "account_id": DEFAULT_ACCOUNT_SB,
        "product_type": DEFAULT_PRODUCT_SB,
        "engine_origin": ENGINE_ORIGIN_SB,
    },
}


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


def engine_lanes_config(cfg: Any | None = None) -> dict[str, dict[str, Any]]:
    """Merged engine lane block from config (``engine_lanes`` keys override defaults)."""
    lanes = _cfg_block(cfg, "engine_lanes")
    out: dict[str, dict[str, Any]] = {}
    for engine_id, defaults in _LANE_DEFAULTS.items():
        merged = dict(defaults)
        block = lanes.get(engine_id)
        if isinstance(block, dict):
            for k, v in block.items():
                if v is not None and str(v).strip():
                    merged[k] = str(v).strip()
        out[engine_id] = merged
    return out


def resolve_active_engine_id(cfg: Any | None = None) -> str:
    """Infer the live engine — CLI ``--origin`` wins in v32 dual-port mode."""
    use_env = os.environ.get("IG_V32_DUAL_PORT", "").strip() == "1"
    if use_env:
        env_engine = os.environ.get("IG_ACTIVE_ENGINE_ID", "").strip()
        if env_engine in _LANE_DEFAULTS:
            return env_engine
        env_origin = os.environ.get("IG_ENGINE_ORIGIN", "").strip().upper()
        if env_origin == ENGINE_ORIGIN_CFD:
            return ENGINE_CFD_SNIPER
        if env_origin == ENGINE_ORIGIN_SB:
            return ENGINE_SB_SENTINEL
    try:
        dc = _cfg_block(cfg, "dual_core")
        product = str(dc.get("broker_account_product") or "").upper()
        if product == DEFAULT_PRODUCT_CFD:
            return ENGINE_CFD_SNIPER
    except Exception:
        pass
    try:
        if cfg is not None:
            if isinstance(cfg, dict):
                explicit = cfg.get("active_engine_id")
            else:
                explicit = getattr(cfg, "active_engine_id", None)
            if explicit:
                return str(explicit).strip()
    except Exception:
        pass
    return ENGINE_SB_SENTINEL


def infer_engine_id(
    *,
    product_type: str | None = None,
    account_id: str | None = None,
    cfg: Any | None = None,
) -> str:
    acct = str(account_id or "").strip().upper()
    if acct == DEFAULT_ACCOUNT_CFD:
        return ENGINE_CFD_SNIPER
    if acct == DEFAULT_ACCOUNT_SB:
        return ENGINE_SB_SENTINEL
    prod = str(product_type or "").upper()
    if prod == DEFAULT_PRODUCT_CFD:
        return ENGINE_CFD_SNIPER
    if prod == DEFAULT_PRODUCT_SB:
        return ENGINE_SB_SENTINEL
    return resolve_active_engine_id(cfg)


def resolve_journal_metadata(
    *,
    engine_id: str | None = None,
    account_id: str | None = None,
    product_type: str | None = None,
    engine_origin: str | None = None,
    cfg: Any | None = None,
) -> dict[str, str]:
    """Account / product / engine_origin for journal + learning rows."""
    use_env = os.environ.get("IG_V32_DUAL_PORT", "").strip() == "1"
    env_account = os.environ.get("IG_ACCOUNT_ID", "").strip() if use_env else ""
    env_origin = os.environ.get("IG_ENGINE_ORIGIN", "").strip() if use_env else ""
    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            cfg = None
    lanes = engine_lanes_config(cfg)
    eid = str(engine_id or infer_engine_id(product_type=product_type, account_id=account_id, cfg=cfg))
    lane = lanes.get(eid) or _LANE_DEFAULTS.get(eid) or _LANE_DEFAULTS[ENGINE_SB_SENTINEL]
    return {
        "account_id": str(
            account_id or env_account or lane.get("account_id") or DEFAULT_ACCOUNT_SB
        ).strip(),
        "product_type": str(product_type or lane.get("product_type") or DEFAULT_PRODUCT_SB).strip(),
        "engine_origin": str(
            engine_origin or env_origin or lane.get("engine_origin") or ENGINE_ORIGIN_SB
        ).strip(),
        "engine_id": eid,
    }


def engine_position_caps(cfg: Any | None = None) -> dict[str, int | None]:
    """Per-engine concurrent open caps — ``None`` means unlimited."""
    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            cfg = None
    raw = _cfg_block(cfg, "engine_position_caps")
    out: dict[str, int | None] = {}
    for engine_id in (ENGINE_CFD_SNIPER, ENGINE_SB_SENTINEL):
        val = raw.get(engine_id)
        if val is None and engine_id in raw:
            out[engine_id] = None
        elif val is not None:
            try:
                n = int(val)
                out[engine_id] = max(1, n) if n > 0 else None
            except (TypeError, ValueError):
                out[engine_id] = None
        else:
            out[engine_id] = None
    return out


def engine_position_cap(engine_id: str, cfg: Any | None = None) -> int | None:
    caps = engine_position_caps(cfg)
    return caps.get(str(engine_id or "").strip())


def global_max_open_positions(cfg: Any | None = None) -> int | None:
    """Global cap — ``None`` when neutralized (v32 dual-engine)."""
    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            return 18
    try:
        if isinstance(cfg, dict):
            raw = cfg.get("max_open_positions")
        else:
            raw = getattr(cfg, "_data", {}).get("max_open_positions")
            if raw is None:
                raw = getattr(cfg, "max_open_positions", None)
        if raw is None:
            return None
        n = int(raw)
        if n <= 0:
            return None
        return max(1, min(18, n))
    except (TypeError, ValueError):
        return 18


def count_cap_for_engine(engine_id: str, cfg: Any | None = None) -> int | None:
    """Effective concurrent cap for an engine lane (``None`` = unlimited).

    CFD sniper / Z6BAH4 is hard-capped at 1 open — config cannot raise this.
    SB sentinel remains independent (config / global only).
    """
    cap = engine_position_cap(engine_id, cfg)
    eid = str(engine_id or "").strip()
    if eid == ENGINE_CFD_SNIPER:
        hard = 1
        try:
            from execution.order_in_flight_mutex import resolve_account_hard_open_cap

            acct_hard = resolve_account_hard_open_cap(DEFAULT_ACCOUNT_CFD)
            if acct_hard is not None:
                hard = int(acct_hard)
        except Exception:
            hard = 1
        if cap is None:
            return hard
        return min(int(cap), hard)
    if cap is not None:
        return cap
    global_cap = global_max_open_positions(cfg)
    return global_cap
