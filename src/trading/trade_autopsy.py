"""Trade autopsy records — never block execution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.paths import data_dir
from system.state_manager import atomic_write_json

_AUTOPSY_DIR = data_dir() / "autopsy"
_JSONL = data_dir() / "trade_autopsy.jsonl"


def write_trade_autopsy(deal_id: str, payload: dict[str, Any]) -> None:
    if not deal_id:
        return
    try:
        _AUTOPSY_DIR.mkdir(parents=True, exist_ok=True)
        path = _AUTOPSY_DIR / f"{deal_id}.json"
        atomic_write_json(path, payload)
        line = json.dumps({"deal_id": deal_id, **payload}, default=str)
        _JSONL.parent.mkdir(parents=True, exist_ok=True)
        with open(_JSONL, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        log_engine(f"trade_autopsy write failed deal={deal_id}: {type(e).__name__}: {e}")
