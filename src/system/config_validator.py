"""
Config validation on startup — Section 4.5 Step 10.

Critical keys must be present (refuse start). Optional keys receive safe
defaults with warnings logged. Emergency stop lock blocks startup.
"""

from __future__ import annotations

from typing import Any

from system.engine_log import log_engine
from system.paths import project_root

LOCK_FILENAME = "emergency_stop.lock"

APP_VERSION = "29.0.0"
APP_VERSION_LABEL = "v29.0"

CRITICAL_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ig_username", ("ig_username", "username")),
    ("ig_password", ("ig_password", "password")),
    ("ig_api_key", ("ig_api_key", "api_key")),
    ("ig_account_id", ("ig_account_id", "account_id")),
    ("epic", ("epic",)),
)

OPTIONAL_DEFAULTS: dict[str, Any] = {
    "trading_strictness_profile": "firm",
    "signal_threshold": 85,
    "allow_live_trading": True,
    "trade_size": 1.0,
    "stop_distance_points": 90,
    "max_spread_points": 35,
    "max_daily_loss_gbp": 200.0,
    "max_open_positions": 1,
    "cooldown_seconds": 180,
    "ohlc_strict_local_cache_first": True,
    "ohlc_local_cache_max_bars": 5000,
}


def _present(config: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        if key not in config:
            continue
        val = config[key]
        if val is None:
            continue
        if isinstance(val, str) and not val.strip():
            continue
        return str(val).strip()
    return None


def emergency_stop_lock_present() -> bool:
    return (project_root() / LOCK_FILENAME).is_file()


def _validate_instruments(config: dict[str, Any], messages: list[str]) -> bool:
    """Validate instruments registry block (Section 4.5 Step 11)."""
    instruments = config.get("instruments")
    if not isinstance(instruments, dict) or not instruments:
        msg = "missing instruments block"
        messages.append(f"ERROR: {msg}")
        log_engine(f"config_validator: {msg}")
        return False

    enabled: list[tuple[str, dict[str, Any]]] = []
    for key, inst in instruments.items():
        if not isinstance(inst, dict):
            msg = f"instruments.{key} must be an object"
            messages.append(f"ERROR: {msg}")
            log_engine(f"config_validator: {msg}")
            return False
        if bool(inst.get("enabled")):
            enabled.append((str(key), inst))

    if not enabled:
        msg = "no instruments enabled — at least one required"
        messages.append(f"ERROR: {msg}")
        log_engine(f"config_validator: {msg}")
        return False

    ok = True
    for key, inst in enabled:
        if _present(inst, ("epic",)) is None:
            ok = False
            msg = f"instruments.{key} enabled but missing epic"
            messages.append(f"ERROR: {msg}")
            log_engine(f"config_validator: {msg}")
        if _present(inst, ("name",)) is None:
            ok = False
            msg = f"instruments.{key} enabled but missing name"
            messages.append(f"ERROR: {msg}")
            log_engine(f"config_validator: {msg}")
    return ok


def apply_config_defaults(config: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with optional defaults applied (does not validate)."""
    out = dict(config)
    for key, default in OPTIONAL_DEFAULTS.items():
        if (
            key not in out
            or out[key] is None
            or (isinstance(out[key], str) and not str(out[key]).strip())
        ):
            out[key] = default
    if "max_daily_loss" not in out or out.get("max_daily_loss") in (None, "", 0):
        out["max_daily_loss"] = -float(out.get("max_daily_loss_gbp", 200.0))
    if "max_positions_per_epic" not in out or out.get("max_positions_per_epic") in (
        None,
        "",
    ):
        out["max_positions_per_epic"] = int(out.get("max_open_positions", 1))
    if "telegram" not in out or not isinstance(out.get("telegram"), dict):
        out["telegram"] = {
            "enabled": False,
            "bot_token": "",
            "chat_id": "",
            "executive_status_only": True,
        }
    else:
        tg = out["telegram"]
        if "executive_status_only" not in tg:
            tg["executive_status_only"] = True
    return out


def validate_config(config: dict[str, Any]) -> tuple[bool, list[str]]:
    """
    Validate configuration for agent startup.

    Returns (valid, messages) where valid is False if emergency lock is present
    or any critical key is missing/empty. Missing optional keys add warnings
    and are logged with safe defaults (see apply_config_defaults).
    """
    messages: list[str] = []
    valid = True

    if emergency_stop_lock_present():
        msg = "Emergency stop lock present — delete emergency_stop.lock to restart"
        messages.append(f"ERROR: {msg}")
        log_engine(f"config_validator: {msg}")
        valid = False

    for label, aliases in CRITICAL_KEYS:
        if _present(config, aliases) is None:
            valid = False
            keys_s = ", ".join(aliases)
            messages.append(f"ERROR: missing critical key {label} ({keys_s})")
            log_engine(f"config_validator: missing critical key {label}")

    for key, default in OPTIONAL_DEFAULTS.items():
        if _present(config, (key,)) is not None:
            continue
        warn = f"missing optional key {key} — using default {default!r}"
        messages.append(f"WARNING: {warn}")
        log_engine(f"config_validator: {warn}")

    if not _validate_instruments(config, messages):
        valid = False

    cfg_version = str(config.get("version") or config.get("app_version") or "").strip()
    if not cfg_version:
        warn = f"missing version — expected {APP_VERSION_LABEL}"
        messages.append(f"WARNING: {warn}")
        log_engine(f"config_validator: {warn}")
    elif not cfg_version.startswith("29"):
        warn = f"config version {cfg_version!r} — platform expects {APP_VERSION_LABEL}"
        messages.append(f"WARNING: {warn}")
        log_engine(f"config_validator: {warn}")

    return valid, messages
