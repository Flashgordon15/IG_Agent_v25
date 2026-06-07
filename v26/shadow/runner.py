"""Process feeder lake events → v26 shadow intents."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from strategies.base import ShadowIntent, StrategyPlugin
from strategies.s1_rules_v25 import S1RulesV25

_lock = threading.Lock()
_seen: set[str] = set()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def shadow_dir() -> Path:
    d = _project_root() / "data_lake" / "shadow_v26"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _dedupe_key(row: dict[str, Any], intent: ShadowIntent) -> str:
    return f"{intent.strategy_id}|{intent.epic}|{intent.source_ts}|{intent.setup_key}"


def _append_shadow(day: str, row: dict[str, Any]) -> None:
    path = shadow_dir() / f"{day}.jsonl"
    line = json.dumps(row, default=str, separators=(",", ":")) + "\n"
    with _lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)


def process_event(
    event: dict[str, Any],
    strategies: list[StrategyPlugin] | None = None,
    *,
    day: str,
) -> list[ShadowIntent]:
    """Run v26 strategies on one feeder event; append new shadow intents."""
    strats = strategies if strategies is not None else [S1RulesV25()]
    out: list[ShadowIntent] = []
    for strat in strats:
        intent = strat.evaluate_feeder_event(event)
        if intent is None:
            continue
        key = _dedupe_key(event, intent)
        with _lock:
            if key in _seen:
                continue
            _seen.add(key)
        _append_shadow(day, intent.to_event_row())
        out.append(intent)
    return out


def process_day_events(
    events: list[dict[str, Any]],
    *,
    day: str,
    clear_seen: bool = False,
) -> int:
    if clear_seen:
        with _lock:
            _seen.clear()
    n = 0
    for ev in events:
        n += len(process_event(ev, day=day))
    return n


def reset_shadow_state() -> None:
    with _lock:
        _seen.clear()
