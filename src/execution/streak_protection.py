"""Per-account streak protection — post-win cooldown, post-loss tilt lock, CFD chop gate.

Dedicated state (not entry_halt / deploy_hold) so operator halt-clear does not fight
these timers. Arms on settled journal closes; enforces only on new entries.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from system.engine_log import log_engine

_LOCK = threading.RLock()
_MEMORY: dict[str, dict[str, Any]] = {}
_NOW_OVERRIDE: float | None = None


def _now() -> float:
    if _NOW_OVERRIDE is not None:
        return float(_NOW_OVERRIDE)
    return time.time()


def set_streak_clock_for_tests(ts: float | None) -> None:
    """Freeze streak timers in unit tests without patching global time.time."""
    global _NOW_OVERRIDE
    _NOW_OVERRIDE = float(ts) if ts is not None else None

_DEFAULT_POST_WIN_SEC = 600.0
_DEFAULT_POST_LOSS_SEC = 900.0
_DEFAULT_POST_LOSS_ML_BOOST = 0.08
_DOW_EPIC = "IX.D.DOW.IFM.IP"
_CFD_ACCOUNTS = frozenset({"Z6BAH4"})
_MEAN_REV_LABELS = frozenset(
    {
        "MEAN_REVERSION",
        "NOT_TRENDING",
        "RANGE_BOUND",
        "RANGE",
        "RANGE_COMPRESSED",
        "CHOP",
        "NEUTRAL",
        "LOW_VOL",
        "STAGNANT_DZ",
        "STAGNANT",
        "DEAD_ZONE",
    }
)


def _cfg_root(cfg: Any | None) -> Any | None:
    if cfg is not None:
        return cfg
    try:
        from system.config_loader import get_config

        return get_config()
    except Exception:
        return None


def _section(cfg: Any | None, key: str) -> dict[str, Any]:
    root = _cfg_root(cfg)
    if root is None:
        return {}
    try:
        raw = root.get(key) if hasattr(root, "get") else None
        return dict(raw) if isinstance(raw, dict) else {}
    except Exception:
        return {}


def streak_cfg(cfg: Any | None = None) -> dict[str, Any]:
    """Merged streak flags from entry_protection / dual_core / micro_risk."""
    ep = _section(cfg, "entry_protection")
    dual = _section(cfg, "dual_core")
    micro = _section(cfg, "micro_risk")
    regime = _section(cfg, "pre_entry_regime_veto")
    # Prefer nested streak_protection blocks; fall back to flat keys.
    nested: dict[str, Any] = {}
    for block in (ep, dual, micro):
        sub = block.get("streak_protection")
        if isinstance(sub, dict):
            nested.update(sub)
    out = {
        "enabled": bool(
            nested.get(
                "enabled",
                ep.get("streak_protection_enabled", True),
            )
        ),
        "post_win_cooldown_sec": float(
            nested.get(
                "post_win_cooldown_sec",
                ep.get("post_win_cooldown_sec", _DEFAULT_POST_WIN_SEC),
            )
            or _DEFAULT_POST_WIN_SEC
        ),
        "post_loss_lock_sec": float(
            nested.get(
                "post_loss_lock_sec",
                ep.get("post_loss_lock_sec", _DEFAULT_POST_LOSS_SEC),
            )
            or _DEFAULT_POST_LOSS_SEC
        ),
        "post_loss_mode": str(
            nested.get(
                "post_loss_mode",
                ep.get("post_loss_mode", "lock"),
            )
            or "lock"
        ).strip().lower(),
        "post_loss_ml_boost": float(
            nested.get(
                "post_loss_ml_boost",
                ep.get("post_loss_ml_boost", _DEFAULT_POST_LOSS_ML_BOOST),
            )
            or _DEFAULT_POST_LOSS_ML_BOOST
        ),
        "cfd_block_mean_reversion": bool(
            nested.get(
                "cfd_block_mean_reversion",
                dual.get(
                    "cfd_block_mean_reversion",
                    regime.get("cfd_block_mean_reversion", True),
                ),
            )
        ),
        "cfd_require_15m_trend_ml_obi": bool(
            nested.get(
                "cfd_require_15m_trend_ml_obi",
                dual.get("cfd_require_15m_trend_ml_obi", True),
            )
        ),
        "cfd_epics": list(
            nested.get("cfd_epics")
            or dual.get("cfd_streak_epics")
            or [_DOW_EPIC]
        ),
    }
    return out


def _norm_account(account_id: str | None) -> str:
    acct = str(account_id or "").strip().upper()
    if acct:
        return acct
    return str(os.environ.get("IG_ACCOUNT_ID") or "").strip().upper()


def _state_dir_for_account(account_id: str) -> Path:
    from system.paths import data_dir

    root = Path(data_dir())
    if account_id == "Z6BAH4":
        base = root / "state_cfd"
    elif account_id == "Z6BAH3":
        base = root / "state_sb"
    else:
        base = root / "state"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _state_path(account_id: str) -> Path:
    return _state_dir_for_account(account_id) / "streak_protection.json"


def _empty_state(account_id: str) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "post_win_until": 0.0,
        "post_loss_until": 0.0,
        "post_loss_ml_boost_until": 0.0,
        "last_deal_id": "",
        "last_pnl_gbp": None,
        "updated_at": 0.0,
    }


def _load_state(account_id: str) -> dict[str, Any]:
    with _LOCK:
        mem = _MEMORY.get(account_id)
        if isinstance(mem, dict):
            return dict(mem)
        path = _state_path(account_id)
        try:
            if path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    st = _empty_state(account_id)
                    st.update(raw)
                    st["account_id"] = account_id
                    _MEMORY[account_id] = dict(st)
                    return dict(st)
        except Exception:
            pass
        st = _empty_state(account_id)
        _MEMORY[account_id] = dict(st)
        return dict(st)


def _save_state(account_id: str, state: dict[str, Any]) -> None:
    st = dict(state)
    st["account_id"] = account_id
    st["updated_at"] = _now()
    with _LOCK:
        _MEMORY[account_id] = dict(st)
        path = _state_path(account_id)
        try:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(st, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:
            log_engine(
                f"StreakProtection: persist failed acct={account_id} "
                f"{type(exc).__name__}: {exc}"
            )


def reset_streak_protection_for_tests() -> None:
    global _NOW_OVERRIDE
    with _LOCK:
        _MEMORY.clear()
    _NOW_OVERRIDE = None


def arm_streak_protection_on_close(
    *,
    account_id: str | None,
    realized_pnl_gbp: float | None,
    deal_id: str = "",
    cfg: Any | None = None,
) -> dict[str, Any]:
    """Arm post-win cooldown or post-loss lock when a real DealID settles.

    Idempotent on deal_id. Does not touch entry_halt / deploy_hold.
    """
    sc = streak_cfg(cfg)
    if not sc.get("enabled", True):
        return {"armed": False, "reason": "streak_protection_off"}

    acct = _norm_account(account_id)
    if not acct:
        return {"armed": False, "reason": "no_account"}

    if realized_pnl_gbp is None:
        return {"armed": False, "reason": "no_pnl"}

    try:
        pnl = float(realized_pnl_gbp)
    except (TypeError, ValueError):
        return {"armed": False, "reason": "bad_pnl"}

    deal = str(deal_id or "").strip()
    now = _now()
    with _LOCK:
        st = _load_state(acct)
        if deal and str(st.get("last_deal_id") or "") == deal:
            return {"armed": False, "reason": "already_armed", "deal_id": deal}

        st["last_deal_id"] = deal
        st["last_pnl_gbp"] = pnl

        if pnl > 0:
            cool = max(0.0, float(sc["post_win_cooldown_sec"]))
            until = now + cool if cool > 0 else 0.0
            st["post_win_until"] = until
            # Fresh win clears a prior tilt lock window.
            st["post_loss_until"] = 0.0
            st["post_loss_ml_boost_until"] = 0.0
            _save_state(acct, st)
            log_engine(
                f"StreakProtection: POST_WIN_COOLDOWN armed acct={acct} "
                f"deal={deal[:16] or '-'} pnl={pnl:.2f} "
                f"block_entries_for={cool:.0f}s until={until:.0f}"
            )
            return {
                "armed": True,
                "kind": "post_win_cooldown",
                "account_id": acct,
                "until": until,
                "sec": cool,
                "deal_id": deal,
                "pnl": pnl,
            }

        if pnl < 0:
            lock_sec = max(0.0, float(sc["post_loss_lock_sec"]))
            mode = str(sc.get("post_loss_mode") or "lock")
            if mode == "ml_boost":
                until = now + lock_sec if lock_sec > 0 else 0.0
                st["post_loss_ml_boost_until"] = until
                st["post_loss_until"] = 0.0
                _save_state(acct, st)
                boost = float(sc["post_loss_ml_boost"])
                log_engine(
                    f"StreakProtection: POST_LOSS_ML_BOOST armed acct={acct} "
                    f"deal={deal[:16] or '-'} pnl={pnl:.2f} "
                    f"ml_boost=+{boost:.3f} for={lock_sec:.0f}s until={until:.0f}"
                )
                return {
                    "armed": True,
                    "kind": "post_loss_ml_boost",
                    "account_id": acct,
                    "until": until,
                    "sec": lock_sec,
                    "ml_boost": boost,
                    "deal_id": deal,
                    "pnl": pnl,
                }

            until = now + lock_sec if lock_sec > 0 else 0.0
            st["post_loss_until"] = until
            st["post_loss_ml_boost_until"] = 0.0
            _save_state(acct, st)
            log_engine(
                f"StreakProtection: POST_LOSS_TILT_LOCK armed acct={acct} "
                f"deal={deal[:16] or '-'} pnl={pnl:.2f} "
                f"block_entries_for={lock_sec:.0f}s until={until:.0f}"
            )
            return {
                "armed": True,
                "kind": "post_loss_lock",
                "account_id": acct,
                "until": until,
                "sec": lock_sec,
                "deal_id": deal,
                "pnl": pnl,
            }

        # Flat / breakeven — no arm
        _save_state(acct, st)
        return {"armed": False, "reason": "flat_pnl", "pnl": pnl}


def post_loss_ml_threshold_boost(
    account_id: str | None = None,
    *,
    cfg: Any | None = None,
    now: float | None = None,
) -> float:
    """Extra ML threshold while post-loss ml_boost window is active."""
    sc = streak_cfg(cfg)
    if not sc.get("enabled", True):
        return 0.0
    acct = _norm_account(account_id)
    if not acct:
        return 0.0
    t = float(now if now is not None else time.time())
    st = _load_state(acct)
    until = float(st.get("post_loss_ml_boost_until") or 0.0)
    if until > t:
        return max(0.0, float(sc["post_loss_ml_boost"]))
    return 0.0


def _cooldown_blocks(
    account_id: str,
    *,
    cfg: Any | None,
    now: float,
) -> tuple[bool, str]:
    sc = streak_cfg(cfg)
    if not sc.get("enabled", True):
        return False, "streak_protection_off"
    st = _load_state(account_id)
    win_until = float(st.get("post_win_until") or 0.0)
    if win_until > now:
        wait = win_until - now
        return (
            True,
            f"post_win_cooldown acct={account_id} wait={wait:.0f}s",
        )
    loss_until = float(st.get("post_loss_until") or 0.0)
    if loss_until > now:
        wait = loss_until - now
        return (
            True,
            f"post_loss_tilt_lock acct={account_id} wait={wait:.0f}s",
        )
    return False, "ok"


def _is_cfd_lane(
    account_id: str,
    *,
    product_type: str | None = None,
    engine_origin: str | None = None,
) -> bool:
    if account_id in _CFD_ACCOUNTS:
        return True
    if str(product_type or "").strip().upper() == "CFD":
        return True
    if str(engine_origin or "").strip().upper() == "QUANT_SNIPER":
        return True
    origin = str(os.environ.get("IG_ENGINE_ORIGIN") or "").strip().upper()
    return origin == "QUANT_SNIPER" and account_id in _CFD_ACCOUNTS


def _resolve_regime_label(epic: str) -> str:
    try:
        from execution.pre_entry_regime_veto import _resolve_regime_label

        return str(_resolve_regime_label(epic) or "").strip().upper()
    except Exception:
        return ""


def check_cfd_chop_selectivity(
    *,
    account_id: str | None = None,
    epic: str = "",
    direction: str = "",
    cfg: Any | None = None,
    product_type: str | None = None,
    engine_origin: str | None = None,
    bid: float = 0.0,
    offer: float = 0.0,
    regime_label: str | None = None,
) -> tuple[bool, str]:
    """Block CFD entries in MEAN_REVERSION / not_trending; SB continues.

    Also strengthens CFD DOW path: require 15m trend + ML/OBI alignment when
    ``cfd_require_15m_trend_ml_obi`` is set.
    """
    sc = streak_cfg(cfg)
    if not sc.get("enabled", True):
        return True, "streak_protection_off"

    acct = _norm_account(account_id)
    if not _is_cfd_lane(acct, product_type=product_type, engine_origin=engine_origin):
        return True, "sb_lane_exempt"

    epic_s = str(epic or "").strip()
    cfd_epics = {str(e).strip() for e in (sc.get("cfd_epics") or []) if str(e).strip()}
    if cfd_epics and epic_s and epic_s not in cfd_epics:
        # Non-DOW CFD epics still honor mean-reversion block when flag on.
        pass

    label = str(regime_label or "").strip().upper() or _resolve_regime_label(epic_s)
    if sc.get("cfd_block_mean_reversion", True) and label in _MEAN_REV_LABELS:
        reason = f"cfd_chop_block label={label or 'MEAN_REVERSION'} acct={acct}"
        log_engine(f"StreakProtection: {reason} epic={epic_s}")
        return False, reason

    if not sc.get("cfd_require_15m_trend_ml_obi", True):
        return True, "cfd_selectivity_relaxed"

    # 15m trend lock (hard-capped CFD never uncouples).
    dir_u = str(direction or "").upper()
    if dir_u in ("BUY", "SELL"):
        try:
            from runtime.dual_core_execution import macro_15min_trend_allows_direction

            if not macro_15min_trend_allows_direction(dir_u, epic_s or None):
                reason = f"cfd_15m_trend_lock dir={dir_u} acct={acct}"
                log_engine(f"StreakProtection: {reason} epic={epic_s}")
                return False, reason
        except Exception as exc:
            return False, f"cfd_15m_trend_fail_closed:{type(exc).__name__}"

    # ML + OBI alignment when book is available (strengthen trend lock).
    if dir_u in ("BUY", "SELL") and float(bid) > 0 and float(offer) > float(bid):
        from types import SimpleNamespace

        quote = SimpleNamespace(
            bid=float(bid), offer=float(offer), mid=(float(bid) + float(offer)) / 2.0
        )
        # Force require_align for CFD selectivity without mutating global config.
        align_cfg = cfg
        try:
            base = dict(cfg) if isinstance(cfg, dict) else {}
            if cfg is not None and hasattr(cfg, "get") and not isinstance(cfg, dict):
                obi_block = dict(cfg.get("obi_filter") or {})
            else:
                obi_block = dict(base.get("obi_filter") or {})
            obi_block["enabled"] = True
            obi_block["require_align"] = True
            if isinstance(cfg, dict):
                align_cfg = {**cfg, "obi_filter": obi_block}
            else:
                align_cfg = {"obi_filter": obi_block}
        except Exception:
            align_cfg = {"obi_filter": {"enabled": True, "require_align": True}}

        try:
            from execution.entry_gate_hardening import evaluate_obi_entry_filter

            obi_ok, obi_detail, _ratio = evaluate_obi_entry_filter(
                epic_s or _DOW_EPIC,
                dir_u,
                quote=quote,
                cfg=align_cfg,
            )
            if not obi_ok:
                reason = f"cfd_obi_align_fail {obi_detail}"
                log_engine(f"StreakProtection: {reason} epic={epic_s}")
                return False, reason
        except Exception as exc:
            return False, f"cfd_obi_fail_closed:{type(exc).__name__}"

        try:
            from execution.entry_gate_hardening import evaluate_sniper_ml_gate

            ml_ok, ml_detail, p = evaluate_sniper_ml_gate(
                epic_s or _DOW_EPIC,
                dir_u,
                cfg=cfg,
                quote=quote,
            )
            boost = post_loss_ml_threshold_boost(acct, cfg=cfg)
            if boost > 0 and float(p) < (0.55 + boost):
                reason = (
                    f"cfd_ml_align_fail post_loss_boost "
                    f"p={float(p):.3f}<{0.55 + boost:.3f}"
                )
                log_engine(f"StreakProtection: {reason} epic={epic_s}")
                return False, reason
            if not ml_ok:
                reason = f"cfd_ml_align_fail {ml_detail}"
                log_engine(f"StreakProtection: {reason} epic={epic_s}")
                return False, reason
        except Exception as exc:
            return False, f"cfd_ml_fail_closed:{type(exc).__name__}"

    return True, "cfd_selectivity_ok"


def check_streak_entry_allowed(
    account_id: str | None = None,
    *,
    epic: str = "",
    direction: str = "",
    cfg: Any | None = None,
    product_type: str | None = None,
    engine_origin: str | None = None,
    bid: float = 0.0,
    offer: float = 0.0,
    now: float | None = None,
    skip_cfd_chop: bool = False,
) -> tuple[bool, str]:
    """Return (allowed, reason). False ⇒ block new entry on this account."""
    sc = streak_cfg(cfg)
    if not sc.get("enabled", True):
        return True, "streak_protection_off"

    acct = _norm_account(account_id)
    if not acct:
        # Fail open only when account truly unknown (unit paths without env).
        return True, "no_account"

    t = float(now if now is not None else time.time())
    blocked, reason = _cooldown_blocks(acct, cfg=cfg, now=t)
    if blocked:
        log_engine(f"StreakProtection: ENTRY_BLOCKED {reason} epic={epic}")
        return False, reason

    if not skip_cfd_chop:
        ok_cfd, cfd_reason = check_cfd_chop_selectivity(
            account_id=acct,
            epic=epic,
            direction=direction,
            cfg=cfg,
            product_type=product_type,
            engine_origin=engine_origin,
            bid=bid,
            offer=offer,
        )
        if not ok_cfd:
            return False, cfd_reason

    return True, "ok"


def streak_protection_status(
    account_id: str | None = None,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    acct = _norm_account(account_id)
    if not acct:
        return {"account_id": "", "active": False}
    t = float(now if now is not None else time.time())
    st = _load_state(acct)
    win_rem = max(0.0, float(st.get("post_win_until") or 0.0) - t)
    loss_rem = max(0.0, float(st.get("post_loss_until") or 0.0) - t)
    boost_rem = max(0.0, float(st.get("post_loss_ml_boost_until") or 0.0) - t)
    return {
        "account_id": acct,
        "active": bool(win_rem > 0 or loss_rem > 0 or boost_rem > 0),
        "post_win_remaining_sec": round(win_rem, 1),
        "post_loss_remaining_sec": round(loss_rem, 1),
        "post_loss_ml_boost_remaining_sec": round(boost_rem, 1),
        "last_deal_id": st.get("last_deal_id") or "",
        "last_pnl_gbp": st.get("last_pnl_gbp"),
        "updated_at": st.get("updated_at"),
    }
