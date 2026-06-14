"""CI-safe config and directory helpers for tests (no live config/ required)."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from system.config import Config

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

CI_DATA_DIRS = (
    "src/data/logs",
    "src/data/state",
    "src/data/ohlc_cache",
    "src/data/market_stream",
    "src/data/ml",
    "src/data/snapshots",
)


def ensure_test_data_directories() -> None:
    """Create runtime data dirs expected by path helpers when absent (GitHub Actions)."""
    for rel in CI_DATA_DIRS:
        (ROOT / rel).mkdir(parents=True, exist_ok=True)


def _fixture_path(name: str) -> Path:
    return FIXTURES / name


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def materialize_config_from_fixtures_if_missing() -> None:
    """On CI hosts without config/, copy bundled fixtures before tests import ConfigLoader."""
    if os.environ.get("CI") != "true":
        return
    cfg_dir = ROOT / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    pairs = (
        ("config_v25.json", "config_v25_ci_fallback.json"),
        ("config_v29.json", "config_v29_ci_fallback.json"),
    )
    for dest_name, fixture_name in pairs:
        dest = cfg_dir / dest_name
        if dest.is_file():
            continue
        src = _fixture_path(fixture_name)
        if src.is_file():
            dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def v25_config_path() -> Path:
    primary = ROOT / "config" / "config_v25.json"
    if primary.is_file():
        return primary
    fallback = _fixture_path("config_v25_ci_fallback.json")
    if fallback.is_file():
        return fallback
    raise FileNotFoundError(
        "config_v25.json missing and no tests/fixtures/config_v25_ci_fallback.json"
    )


def v29_config_path() -> Path:
    primary = ROOT / "config" / "config_v29.json"
    if primary.is_file():
        return primary
    fallback = _fixture_path("config_v29_ci_fallback.json")
    if fallback.is_file():
        return fallback
    raise FileNotFoundError(
        "config_v29.json missing and no tests/fixtures/config_v29_ci_fallback.json"
    )


def load_v25_config_dict() -> dict[str, Any]:
    """Load v25 config from disk or CI fixture (deep copy for safe mutation)."""
    return deepcopy(_read_json(v25_config_path()))


def load_v29_config_dict() -> dict[str, Any]:
    """Load v29 overlay from disk or CI fixture."""
    return deepcopy(_read_json(v29_config_path()))


def trade_manager_test_config(**overrides: Any) -> Config:
    """In-memory Config for trade_manager / stale_decay tests — never reads disk."""
    data: dict[str, Any] = {
        "operating_mode": "DEMO",
        "account_type": "DEMO",
        "epic": "IX.D.NIKKEI.IFM.IP",
        "auto_trade_enabled": True,
        "dry_run": True,
        "signal_threshold": 85,
        "trade_size": 1.0,
        "risk_points": 40,
        "reward_multiple": 2.0,
        "limit_distance_points": 80,
        "stop_distance_points": 40,
        "max_spread": 35,
        "max_spread_points": 35,
        "fast_ema": 9,
        "slow_ema": 21,
        "rsi_period": 14,
        "rsi_buy_min": 58,
        "rsi_buy_max": 68,
        "rsi_sell_max": 45,
        "breakeven_enabled": True,
        "breakeven_trigger_points": 30,
        "breakeven_lock_points": 0,
        "breakeven_offset_points": 0,
        "adaptive_trailing_stop_enabled": True,
        "adaptive_trailing_trigger_points": 30,
        "adaptive_trailing_distance_points": 25,
        "learning_enabled": False,
        "max_live_quotes": 1000,
        "trailing_stop": {
            "stale_decay_activation_minutes": 15,
            "stale_decay_factor_per_minute": 0.02,
            "limit_extension_enabled": False,
            "partial_close_enabled": True,
            "partial_close_at_r": 1.5,
            "partial_close_fraction": 0.5,
            "partial_close_rungs": [
                {"at_r_multiple": 1.5, "fraction": 0.25},
                {"at_r_multiple": 2.5, "fraction": 0.25},
            ],
        },
    }
    data.update(overrides)
    if "trailing_stop" in overrides and isinstance(overrides["trailing_stop"], dict):
        merged_ts = dict(data.get("trailing_stop") or {})
        merged_ts.update(overrides["trailing_stop"])
        data["trailing_stop"] = merged_ts
    return Config(_data=data)
