"""Load market watch fund configs from config/market_watch/."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from system.paths import config_dir


def market_watch_root() -> Path:
    return config_dir() / "market_watch"


def registry_path() -> Path:
    return market_watch_root() / "registry.json"


@lru_cache(maxsize=1)
def load_registry() -> dict[str, Any]:
    path = registry_path()
    if not path.is_file():
        return {"funds": []}
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=16)
def load_fund_config(fund_id: str) -> dict[str, Any] | None:
    reg = load_registry()
    rel = ""
    for entry in reg.get("funds") or []:
        if str(entry.get("fund_id") or "") == fund_id:
            rel = str(entry.get("config") or "")
            break
    if not rel:
        return None
    path = market_watch_root() / rel
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("fund_id", fund_id)
    return data


def fund_id_for_epic(epic: str) -> str | None:
    target = str(epic or "").strip()
    if not target:
        return None
    for entry in load_registry().get("funds") or []:
        if str(entry.get("epic") or "").strip() == target:
            return str(entry.get("fund_id") or "") or None
    return None


def load_fund_for_epic(epic: str) -> dict[str, Any] | None:
    fid = fund_id_for_epic(epic)
    if not fid:
        return None
    return load_fund_config(fid)
