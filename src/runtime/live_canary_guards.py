"""Live canary cross-path guards — epic scope and shared risk envelope."""

from __future__ import annotations

from typing import Any

from runtime.live_canary_session import live_canary_enabled


def _cfg_get(cfg: Any, key: str, default: Any = None) -> Any:
    if cfg is None:
        return default
    if hasattr(cfg, "get"):
        return cfg.get(key, default)
    return getattr(cfg, key, default)


def canary_forex_hot_path_locked(cfg: Any | None = None) -> bool:
    if not live_canary_enabled(cfg):
        return False
    dual = _cfg_get(cfg, "dual_core") or {}
    return isinstance(dual, dict) and bool(dual.get("forex_rotation_locked"))


def canary_path_a_epic_allowed(epic: str, cfg: Any | None = None) -> tuple[bool, str]:
    """
    When canary pins forex hot path, Path A macro loops on indices/metals
    must not compete for the single position slot.
    """
    if not canary_forex_hot_path_locked(cfg):
        return True, ""
    try:
        from runtime.dual_core_execution import epic_allowed_on_hot_path

        if epic_allowed_on_hot_path(epic, cfg):
            return True, ""
    except Exception:
        pass
    return False, "canary_hot_path_only"


def canary_micro_dispatch_risk_ok(
    store: Any,
    cfg: Any | None = None,
) -> tuple[bool, str]:
    """Align Path B with Path A £5 daily loss + drawdown shield when canary enabled."""
    if not live_canary_enabled(cfg):
        return True, ""
    if store is None:
        return True, ""
    try:
        from system.daily_loss_policy import daily_loss_gate_status

        loss_ok, loss_detail, _meta = daily_loss_gate_status(store, cfg)
        if not loss_ok:
            return False, f"canary_daily_loss:{loss_detail}"
    except Exception:
        pass
    try:
        from trading.manual_intervention import entries_blocked_by_shield

        shield_blocked, shield_reason = entries_blocked_by_shield(store, cfg)
        if shield_blocked:
            return False, f"canary_shield:{shield_reason}"
    except Exception:
        pass
    return True, ""
