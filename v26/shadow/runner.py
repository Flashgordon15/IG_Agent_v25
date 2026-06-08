"""Process feeder lake events → v26 shadow intents."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from regime.calendar_guard import apply_calendar_guard
from regime.router import (
    classify_regime,
    regime_for_epic,
    route_strategies_for_event,
    update_regime_cache,
)
from shadow.correlation_guard import apply_shadow_correlation_guard
from shadow.strategies import default_shadow_strategies
from strategies.base import ShadowIntent, StrategyPlugin

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


def _calendar_enabled() -> bool:
    try:
        import sys
        from pathlib import Path

        src = Path(__file__).resolve().parents[2] / "src"
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
        from system.v26_config import calendar_settings

        return bool(calendar_settings().get("enabled"))
    except Exception:
        return False


def process_event(
    event: dict[str, Any],
    strategies: list[StrategyPlugin] | None = None,
    *,
    day: str,
) -> list[ShadowIntent]:
    """Run v26 strategies on one feeder event; append new shadow intents."""
    update_regime_cache(event)
    strats = strategies if strategies is not None else default_shadow_strategies()
    strats = route_strategies_for_event(event, strats)
    out: list[ShadowIntent] = []
    for strat in strats:
        intent = strat.evaluate_feeder_event(event)
        if intent is None:
            continue
        if _calendar_enabled():
            intent = apply_calendar_guard(intent, event)
        intent = _apply_setup_ban_guard(intent)
        intent = apply_shadow_correlation_guard(intent)
        intent.payload = {
            **intent.payload,
            "regime": classify_regime(intent.epic),
            "regime_detail": regime_for_epic(intent.epic),
        }
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


def warm_seen_from_shadow_day(day: str) -> int:
    """Rebuild dedupe keys from existing shadow JSONL (safe catch-up on restart)."""
    path = shadow_dir() / f"{day}.jsonl"
    if not path.is_file():
        return 0
    n = 0
    with _lock:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                key = (
                    f"{row.get('strategy_id')}|{row.get('epic')}|"
                    f"{row.get('source_ts')}|{row.get('setup_key')}"
                )
                if key.count("|") >= 3:
                    _seen.add(key)
                    n += 1
    return n


def _apply_setup_ban_guard(intent: ShadowIntent) -> ShadowIntent:
    if not intent.would_trade:
        return intent
    setup_key = str(intent.setup_key or "").strip()
    if not setup_key:
        return intent
    try:
        import sys

        src = _project_root() / "src"
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
        from system.setup_registry import is_setup_banned

        if is_setup_banned(setup_key):
            intent.would_trade = False
            intent.reason = (f"{intent.reason} | shadow ban: setup {setup_key}").strip(
                " |"
            )
            intent.payload = {**intent.payload, "setup_banned": True}
    except Exception:
        pass
    return intent
