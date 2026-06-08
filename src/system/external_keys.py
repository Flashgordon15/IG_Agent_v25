"""Load optional third-party API keys (never log values)."""

from __future__ import annotations

import json
import os
from functools import lru_cache

from system.paths import project_root

_KEY_ALIASES = {
    "finnhub": ("FINNHUB_API_KEY", "finnhub_api_key"),
    "alphavantage": (
        "ALPHAVANTAGE_API_KEY",
        "alpha_vantage_api_key",
        "alphavantage_api_key",
    ),
}


def _optional_file() -> dict:
    for rel in ("config/external_keys.json", "config/credentials/external_keys.json"):
        path = project_root() / rel
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


@lru_cache(maxsize=1)
def _file_keys() -> dict[str, str]:
    raw = _optional_file()
    out: dict[str, str] = {}
    for name, aliases in _KEY_ALIASES.items():
        for key in aliases:
            val = raw.get(key) or raw.get(key.lower())
            if val:
                out[name] = str(val).strip()
                break
    return out


def finnhub_api_key() -> str:
    return os.environ.get("FINNHUB_API_KEY", "").strip() or _file_keys().get(
        "finnhub", ""
    )


def alphavantage_api_key() -> str:
    env = os.environ.get("ALPHAVANTAGE_API_KEY", "").strip()
    if env:
        return env
    return _file_keys().get("alphavantage", "")


def reset_external_keys_cache_for_tests() -> None:
    _file_keys.cache_clear()
