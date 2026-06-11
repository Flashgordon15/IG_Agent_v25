"""Read append-only feeder JSONL from data_lake/events/."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


@dataclass
class LakeSummary:
    day: str
    total_events: int = 0
    by_type: Counter[str] = field(default_factory=Counter)
    epics: set[str] = field(default_factory=set)
    signal_evals: int = 0
    would_fire: int = 0
    trade_ready: int = 0
    signal_actionable: int = 0
    order_intents: int = 0
    fill_closes: int = 0
    fill_pnl_gbp: float = 0.0


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def events_dir() -> Path:
    return _project_root() / "data_lake" / "events"


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def event_utc_day(row: dict[str, Any]) -> str:
    """UTC calendar day for a feeder row (from ``ts``), else today."""
    ts = str(row.get("ts") or "").strip()
    if len(ts) >= 10 and ts[4] == "-" and ts[7] == "-":
        return ts[:10]
    return utc_today()


def iter_events(
    *,
    day: str | None = None,
    epic: str | None = None,
    event_type: str | None = None,
) -> Iterator[dict[str, Any]]:
    root = events_dir()
    if not root.is_dir():
        return
    if day:
        paths = [root / f"{day}.jsonl"]
    else:
        paths = sorted(root.glob("*.jsonl"))
    for path in paths:
        if not path.is_file():
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if epic and str(row.get("epic") or "") != epic:
                    continue
                if event_type and str(row.get("event_type") or "") != event_type:
                    continue
                yield row


def summarize_day(day: str | None = None) -> LakeSummary:
    d = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    summary = LakeSummary(day=d)
    for row in iter_events(day=d):
        summary.total_events += 1
        et = str(row.get("event_type") or "unknown")
        summary.by_type[et] += 1
        epic = str(row.get("epic") or "")
        if epic:
            summary.epics.add(epic)
        payload = row.get("payload") or {}
        if et == "signal_eval":
            summary.signal_evals += 1
            if payload.get("trade_ready") or payload.get("would_fire"):
                summary.would_fire += 1
                summary.trade_ready += 1
            if payload.get("signal_actionable"):
                summary.signal_actionable += 1
        elif et == "order_intent":
            summary.order_intents += 1
        elif et == "fill_close":
            summary.fill_closes += 1
            try:
                summary.fill_pnl_gbp += float(payload.get("pnl_gbp") or 0)
            except (TypeError, ValueError):
                pass
    return summary
