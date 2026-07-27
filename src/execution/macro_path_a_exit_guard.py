"""Path A / MACRO_SENTINEL exit guard — no soft scalp flattens under min-hold.

APP hotfix (learning-loop Step 2): SB Path A claims macro holds, but soft_loss /
micro_gbp_exit / Scalping BE+tx were still cutting in <10s. Hard loss_cap and
critical broker stops remain in force; only soft/early scalp exits are deferred
until ``min_hold_sec`` (default from ``micro_risk.min_hold_before_trail_sec``).
"""

from __future__ import annotations

import os
from typing import Any

# Align with autopsy / trail arm floor (config default 150).
DEFAULT_PATH_A_MIN_HOLD_SEC = 150.0


def path_a_macro_claimed(*, cfg: Any | None = None) -> bool:
    """True when this process is SB MACRO_SENTINEL / Path A carve."""
    acct = str(os.environ.get("IG_ACCOUNT_ID") or "").strip().upper()
    origin = str(os.environ.get("IG_ENGINE_ORIGIN") or "").strip().upper()
    product = str(os.environ.get("IG_PRODUCT_TYPE") or "").strip().upper()
    if acct == "Z6BAH3" or origin == "MACRO_SENTINEL":
        return True
    if product in ("SPREADBET", "SPREAD_BET"):
        return True
    try:
        if cfg is not None and hasattr(cfg, "get"):
            dual = cfg.get("dual_core") or {}
            if isinstance(dual, dict) and bool(dual.get("sb_macro_ltr_entries_only")):
                # Config claims Path A even if env not yet set (tests / early boot).
                if origin in ("", "MACRO_SENTINEL") and product in (
                    "",
                    "SPREADBET",
                    "SPREAD_BET",
                ):
                    return origin == "MACRO_SENTINEL" or product in (
                        "SPREADBET",
                        "SPREAD_BET",
                    )
    except Exception:
        pass
    return False


def path_a_min_hold_sec(cfg: Any | None = None) -> float:
    """Min seconds before soft_loss / BE+tx / micro banks may flatten Path A."""
    min_hold = DEFAULT_PATH_A_MIN_HOLD_SEC
    try:
        if cfg is None:
            from system.config_loader import get_config

            cfg = get_config()
        if cfg is not None and hasattr(cfg, "get"):
            mr = cfg.get("micro_risk") or {}
            if isinstance(mr, dict):
                if mr.get("path_a_min_hold_sec") is not None:
                    min_hold = float(mr.get("path_a_min_hold_sec"))
                elif mr.get("min_hold_before_trail_sec") is not None:
                    min_hold = float(mr.get("min_hold_before_trail_sec"))
    except Exception:
        min_hold = DEFAULT_PATH_A_MIN_HOLD_SEC
    return max(0.0, float(min_hold))


def soft_exit_deferred_for_path_a(
    *,
    hold_sec: float | None,
    cfg: Any | None = None,
    engine_origin: str | None = None,
    style: str | None = None,
) -> tuple[bool, str]:
    """Return (defer, reason) when soft/early exits must wait for min-hold.

    Hard loss_cap / virtual stop / critical flatten must NOT call this as a veto
    — callers gate soft_loss / stagnant / BE+tx only.
    """
    origin = str(engine_origin or os.environ.get("IG_ENGINE_ORIGIN") or "").strip().upper()
    style_l = str(style or "").strip().lower()
    claimed = path_a_macro_claimed(cfg=cfg) or origin == "MACRO_SENTINEL" or style_l == "macro"
    if not claimed:
        return False, ""
    min_hold = path_a_min_hold_sec(cfg)
    if min_hold <= 0:
        return False, ""
    age = float(hold_sec) if hold_sec is not None else 0.0
    if age < min_hold:
        return True, f"path_a_min_hold hold={age:.1f}s < {min_hold:.0f}s"
    return False, ""


def scalping_be_suppressed_for_path_a(*, cfg: Any | None = None) -> bool:
    """Kill Scalping BE+tx arming under Path A / MACRO_SENTINEL claim."""
    return path_a_macro_claimed(cfg=cfg)
