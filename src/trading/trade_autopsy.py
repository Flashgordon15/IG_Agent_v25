"""Trade autopsy records — never block execution."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from system.closed_trades_display import EXCLUDED_SOURCES
from system.engine_log import log_engine
from system.paths import data_dir
from system.state_manager import atomic_write_json

_AUTOPSY_DIR = data_dir() / "autopsy"
_JSONL = data_dir() / "trade_autopsy.jsonl"


def write_trade_autopsy(
    deal_id: str,
    payload: dict,
    ig_pnl_currency_confirmed: bool = False,
    source: str = "",
) -> None:
    if not deal_id:
        return
    if source and any(source.lower().startswith(s) for s in EXCLUDED_SOURCES):
        log_engine(f"trade_autopsy: skipping {deal_id} — excluded source '{source}'")
        return
    if not ig_pnl_currency_confirmed:
        log_engine(f"trade_autopsy: skipping {deal_id} — ig_pnl_currency not confirmed")
        return

    def _write() -> None:
        try:
            _AUTOPSY_DIR.mkdir(parents=True, exist_ok=True)
            path = _AUTOPSY_DIR / f"{deal_id}.json"
            atomic_write_json(path, payload)
            line = json.dumps({"deal_id": deal_id, **payload}, default=str)
            _JSONL.parent.mkdir(parents=True, exist_ok=True)
            with open(_JSONL, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as exc:
            log_engine(f"trade_autopsy: write error for {deal_id}: {exc}")

    threading.Thread(target=_write, daemon=True, name=f"autopsy-{deal_id}").start()
