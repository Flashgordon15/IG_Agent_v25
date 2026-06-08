"""Aggregate shadow intents by strategy, session, and setup."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _shadow_path(day: str) -> Path:
    root = Path(__file__).resolve().parents[2]
    return root / "data_lake" / "shadow_v26" / f"{day}.jsonl"


def summarize_shadow_day(day: str) -> dict[str, Any]:
    path = _shadow_path(day)
    if not path.is_file():
        return {"day": day, "ok": False, "intents": 0}

    by_strategy: dict[str, dict[str, int]] = defaultdict(
        lambda: {"intents": 0, "would_trade": 0, "s1_phase2": 0}
    )
    setups: Counter[str] = Counter()
    sessions: Counter[str] = Counter()
    intents = 0

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("event_type") != "shadow_intent":
            continue
        intents += 1
        sid = str(row.get("strategy_id") or "?")
        payload = row.get("payload") or {}
        by_strategy[sid]["intents"] += 1
        if payload.get("would_trade"):
            by_strategy[sid]["would_trade"] += 1
            if (
                sid == "S1_rules_v25"
                and payload.get("parity_mode") == "independent_rescore"
            ):
                by_strategy[sid]["s1_phase2"] += 1
            sk = str(payload.get("setup_key") or "")
            if sk:
                setups[sk] += 1
            sessions[str(row.get("session") or "")] += 1

    return {
        "day": day,
        "ok": True,
        "intents": intents,
        "by_strategy": dict(by_strategy),
        "top_would_trade_setups": dict(setups.most_common(12)),
        "would_trade_by_session": dict(sessions.most_common()),
    }


def summarize_shadow_days(days: list[str]) -> dict[str, Any]:
    daily = [summarize_shadow_day(d) for d in days]
    ok = [d for d in daily if d.get("ok")]
    merged: dict[str, dict[str, int]] = defaultdict(
        lambda: {"intents": 0, "would_trade": 0, "s1_phase2": 0}
    )
    for d in ok:
        for sid, row in (d.get("by_strategy") or {}).items():
            merged[sid]["intents"] += int(row.get("intents") or 0)
            merged[sid]["would_trade"] += int(row.get("would_trade") or 0)
            merged[sid]["s1_phase2"] += int(row.get("s1_phase2") or 0)
    return {
        "days": days,
        "days_ok": len(ok),
        "total_intents": sum(int(d.get("intents") or 0) for d in ok),
        "by_strategy": dict(merged),
        "daily": daily,
    }
