"""Scalping framework configuration helpers."""

from __future__ import annotations

from typing import Any

from system.config import Config


def scalping_settings(cfg: Config | dict[str, Any] | None = None) -> dict[str, Any]:
    if cfg is None:
        from system.config_loader import get_config

        cfg = get_config()
    raw = cfg.get("scalping_framework", {}) if hasattr(cfg, "get") else {}
    if not isinstance(raw, dict):
        raw = {}
    defaults: dict[str, Any] = {
        "enabled": False,
        "spread_ma_periods": 20,
        "spread_ma_multiplier": 1.5,
        "spread_min_samples": 5,
        "protection_verify_ms": 200,
        "commission_points_per_side": 0.5,
        "breakeven_buffer_points": 2.0,
        "atr_trail_period": 14,
        "atr_trail_multiplier": 0.5,
        "daily_equity_drawdown_pct": 1.5,
        "halt_entries_on_protection_failure": True,
    }
    merged = {**defaults, **raw}
    return merged


def is_scalping_enabled(cfg: Config | dict[str, Any] | None = None) -> bool:
    return bool(scalping_settings(cfg).get("enabled", False))


def is_scalping_exit_management_isolated(
    cfg: Config | dict[str, Any] | None = None,
) -> bool:
    """Scalping BE/trail runs on isolated conditionals — never gated by sentiment/fitness."""
    return is_scalping_enabled(cfg)
