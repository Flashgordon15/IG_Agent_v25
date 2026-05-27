"""
Load and validate configuration — v24 primary, legacy v23/v22 fallback.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from system.config import Config
from system.paths import config_dir, project_root, resolve_path

_MODE: str = "TEST"
_config: Config | None = None
_config_lock = threading.RLock()
_config_file_mtime: float = 0.0  # last known mtime of active config file

V25_FILE = "config_v25.json"
V24_FILE = "config_v24.json"
LEGACY_V23_FILE = "legacy_v23/config_v23.json"


def _primary_config_path() -> Path:
    """Resolve config file: v25 → v24 → legacy v23 (first that exists)."""
    cd = config_dir()
    for rel in (V25_FILE, V24_FILE):
        p = cd / rel
        if p.exists():
            return p
    legacy = cd / LEGACY_V23_FILE
    if legacy.exists():
        return legacy
    return cd / V25_FILE


def _config_file_changed() -> bool:
    """Return True when the active config file has been modified since the last load."""
    try:
        p = _primary_config_path()
        if p.exists():
            mtime = p.stat().st_mtime
            return mtime != _config_file_mtime
    except Exception:
        pass
    return False


def _update_config_mtime() -> None:
    global _config_file_mtime
    try:
        p = _primary_config_path()
        if p.exists():
            _config_file_mtime = p.stat().st_mtime
    except Exception:
        pass

V22_FALLBACK = "legacy_v22/config_v22_adaptive_autotrader.json"
SCHEMA_FILE = "config_schema.json"


def get_mode() -> str:
    return _MODE


def set_mode(mode: str) -> None:
    global _MODE
    m = str(mode).upper().strip()
    if m not in ("TEST", "DEMO", "LIVE"):
        raise ValueError(f"Invalid MODE: {mode}")
    _MODE = m


def _sync_operating_mode_from_credentials(cfg: dict[str, Any]) -> None:
    """
    When IG credentials specify DEMO, default operating_mode to DEMO (broker) not TEST (simulator).
    Explicit operating_mode=TEST in config is preserved for internal simulation only.
    """
    try:
        from system.credentials_holder import get_credentials_holder

        creds = get_credentials_holder().credentials
    except Exception:
        creds = None
    if not creds:
        return
    cfg["account_type"] = creds.account_type
    op = str(cfg.get("operating_mode", "TEST")).upper()
    if creds.account_type == "DEMO" and op == "TEST":
        cfg["operating_mode"] = "DEMO"
    elif creds.account_type == "LIVE" and op == "TEST" and cfg.get("allow_live_trading"):
        cfg["operating_mode"] = "LIVE"


def get_config(*, reload: bool = False) -> Config:
    """Return the global Config singleton.

    Auto-reloads when config_v24.json has been modified on disk so that runtime
    edits to the file take effect immediately (within one trading tick) without
    requiring a bot restart.  Pass ``reload=True`` to force a reload regardless.
    """
    global _config
    with _config_lock:
        if _config is None or reload or _config_file_changed():
            _config = ConfigLoader().load_config()
            _update_config_mtime()
        return _config


def apply_runtime_overrides(**kwargs: Any) -> Config:
    """Merge runtime overrides without persisting to disk (soak/tests)."""
    global _config
    with _config_lock:
        cfg = get_config()
        data = dict(cfg.as_dict())
        if "max_positions_per_epic" in kwargs:
            n = max(1, min(MAX_POSITIONS_PER_EPIC_LIMIT, int(kwargs["max_positions_per_epic"])))
            kwargs = dict(kwargs)
            kwargs["max_positions_per_epic"] = n
            if n > 1:
                kwargs.setdefault("one_position_per_epic", False)
        data.update(kwargs)
        errors = ConfigLoader().validate_schema(data)
        if errors:
            raise ValueError("Runtime config validation failed: " + "; ".join(errors))
        _config = Config(_data=data)
        return _config


MAX_POSITIONS_PER_EPIC_LIMIT = 6


def update_config_values(**kwargs: Any) -> Config:
    """
    Merge runtime config updates, persist to config_v24.json, and refresh the singleton.

    ``max_positions_per_epic`` is clamped to 1–6. Values above 1 disable ``one_position_per_epic``.
    """
    global _config
    with _config_lock:
        cfg = get_config()
        data = dict(cfg.as_dict())
        if "max_positions_per_epic" in kwargs:
            n = max(1, min(MAX_POSITIONS_PER_EPIC_LIMIT, int(kwargs["max_positions_per_epic"])))
            kwargs = dict(kwargs)
            kwargs["max_positions_per_epic"] = n
            if n > 1:
                kwargs.setdefault("one_position_per_epic", False)
        data.update(kwargs)
        loader = ConfigLoader()
        errors = loader.validate_schema(data)
        if errors:
            raise ValueError("Config validation failed: " + "; ".join(errors))
        _config = Config(_data=data)
        loader.save(_config)
        _update_config_mtime()
        return _config


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in overlay.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _apply_aliases(cfg: dict[str, Any]) -> dict[str, Any]:
    """Normalize v22/v23 alias keys to canonical names."""
    aliases = {
        "atr_multiplier": "adaptive_atr_risk_multiple",
        "adaptive_good_setup_multiplier": "adaptive_good_setup_size_multiplier",
        "adaptive_bad_setup_multiplier": "adaptive_bad_setup_size_multiplier",
        "adaptive_good_winrate_threshold": "adaptive_good_winrate",
        "adaptive_bad_winrate_threshold": "adaptive_bad_winrate",
        "max_spread_points": "max_spread",
        "breakeven_lock_points": "breakeven_offset_points",
        "trailing_stop_trigger_points": "adaptive_trailing_trigger_points",
        "trailing_stop_step_points": "adaptive_trailing_distance_points",
        "simulated_latency_ms": "sim_latency_ms",
        "simulated_slippage": "sim_slippage_points",
        "simulated_fill_quality": "sim_fill_quality",
        "simulated_spread_multiplier": "sim_spread_multiplier",
        "default_stop_distance_points": "risk_points",
    }
    for alias, canonical in aliases.items():
        if alias in cfg and canonical not in cfg:
            cfg[canonical] = cfg[alias]
        elif alias in cfg and canonical in cfg and cfg.get(canonical) is None:
            cfg[canonical] = cfg[alias]
    if "cooldown_minutes" in cfg and "cooldown_seconds" not in cfg:
        cfg["cooldown_seconds"] = int(float(cfg["cooldown_minutes"]) * 60)
    if "default_limit_distance_points" not in cfg:
        rp = float(cfg.get("risk_points", cfg.get("default_stop_distance_points", 0)))
        rm = float(cfg.get("reward_multiple", 1))
        cfg["default_limit_distance_points"] = rp * rm
    return cfg


class ConfigLoader:
    def __init__(self, config_path: Path | str | None = None) -> None:
        if config_path:
            self._path = Path(config_path)
        else:
            self._path = _primary_config_path()
        self._schema_path = config_dir() / SCHEMA_FILE
        self._v22_path = config_dir() / V22_FALLBACK

    def load_config(self, *, validate: bool = True) -> Config:
        merged = self._load_merged_dict()
        merged = _apply_aliases(merged)
        _sync_operating_mode_from_credentials(merged)
        self._resolve_paths(merged)
        self._apply_operating_mode(merged)
        if validate:
            errors = self.validate_schema(merged)
            if errors:
                raise ValueError("Config validation failed: " + "; ".join(errors))
        return Config(_data=merged)

    def load(self, *, validate: bool = True) -> dict[str, Any]:
        """Backward-compatible dict load."""
        cfg = self.load_config(validate=validate)
        global _config
        _config = cfg
        return cfg.as_dict()

    def _load_merged_dict(self) -> dict[str, Any]:
        base: dict[str, Any] = {}
        if self._v22_path.exists():
            with open(self._v22_path, "r", encoding="utf-8") as f:
                base = json.load(f)
        if not self._path.exists():
            if not base:
                raise FileNotFoundError(f"Config not found: {self._path}")
            merged = base
        else:
            with open(self._path, "r", encoding="utf-8") as f:
                primary = json.load(f)
            merged = _deep_merge(base, primary)
        # IG secrets: config/credentials/credentials.json via system.credentials_loader only
        if "operating_mode" not in merged:
            merged["operating_mode"] = "LIVE" if not merged.get("dry_run", True) else "TEST"
        return merged

    def _resolve_paths(self, cfg: dict[str, Any]) -> None:
        for key in ("journal_file", "learning_db", "decision_log_file"):
            if cfg.get(key):
                cfg[key] = str(resolve_path(cfg[key]))

    def _apply_operating_mode(self, cfg: dict[str, Any]) -> None:
        op = str(cfg.get("operating_mode", "TEST")).upper()
        set_mode(op if op in ("TEST", "DEMO", "LIVE") else "TEST")

    def save(self, config: Config | dict[str, Any] | None = None) -> None:
        data = config.as_dict() if isinstance(config, Config) else (config or get_config().as_dict())
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self._path)

    def get_markets(self, config: Config | None = None) -> list[dict[str, str]]:
        cfg = (config or get_config()).as_dict()
        markets = cfg.get("markets")
        if isinstance(markets, list) and markets:
            return markets
        return [{"name": cfg.get("market_search", "Market"), "epic": cfg.get("epic", "")}]

    def validate_schema(self, config: dict[str, Any] | None = None) -> list[str]:
        data = config if config is not None else get_config().as_dict()
        errors: list[str] = []
        try:
            import jsonschema
            schema = json.loads(self._schema_path.read_text(encoding="utf-8"))
            validator = jsonschema.Draft202012Validator(schema)
            for err in validator.iter_errors(data):
                errors.append(f"{'.'.join(str(p) for p in err.path)}: {err.message}")
        except ImportError:
            errors.extend(self._validate_manual(data))
        return errors

    @staticmethod
    def _validate_manual(data: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        required = ("epic", "operating_mode", "account_type", "signal_threshold", "risk_points")
        for key in required:
            if key not in data or data[key] in ("", None):
                errors.append(f"missing required field: {key}")
        if not data.get("epic") and not data.get("markets"):
            errors.append("epic or markets required")
        if str(data.get("operating_mode", "")).upper() not in ("TEST", "DEMO", "LIVE"):
            errors.append("operating_mode must be TEST, DEMO, or LIVE")
        return errors
