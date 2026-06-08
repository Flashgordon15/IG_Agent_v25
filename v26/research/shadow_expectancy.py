"""Counterfactual expectancy on blocked / near-miss signals (no-trade days)."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ingest.lake_reader import iter_events


def _parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _load_setup_e_gbp() -> dict[str, float]:
    root = Path(__file__).resolve().parents[2]
    path = root / "data_lake" / "state" / "expectancy_snapshot.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, float] = {}
    for row in data.get("setups") or []:
        if isinstance(row, dict) and row.get("setup_key"):
            out[str(row["setup_key"])] = float(row.get("e_gbp") or 0)
    return out


@dataclass
class NearMissAnalysis:
    day: str
    blocked_signal_confidence: int = 0
    near_miss_evals: int = 0
    near_miss_with_direction: int = 0
    shadow_would_trade_same_epic: int = 0
    by_setup: Counter[str] = field(default_factory=Counter)
    by_epic: Counter[str] = field(default_factory=Counter)
    estimated_e_gbp_sum: float = 0.0


def _load_shadow_would_trade_index(day: str) -> dict[str, list[datetime]]:
    root = Path(__file__).resolve().parents[2]
    path = root / "data_lake" / "shadow_v26" / f"{day}.jsonl"
    by_epic: dict[str, list[datetime]] = defaultdict(list)
    if not path.is_file():
        return by_epic
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
        if not (row.get("payload") or {}).get("would_trade"):
            continue
        epic = str(row.get("epic") or "")
        ts = _parse_ts(str(row.get("ts") or ""))
        if epic and ts:
            by_epic[epic].append(ts)
    for epic in by_epic:
        by_epic[epic].sort()
    return by_epic


def _shadow_match_within_sec(
    epic: str,
    at: datetime,
    shadow_idx: dict[str, list[datetime]],
    *,
    window: float = 300.0,
) -> bool:
    times = shadow_idx.get(epic) or []
    for t in times:
        lag = abs((at - t).total_seconds())
        if lag <= window:
            return True
    return False


def analyze_near_miss(
    *,
    day: str,
    min_conf: float = 70.0,
    max_conf: float = 79.99,
) -> NearMissAnalysis:
    setup_e = _load_setup_e_gbp()
    shadow_idx = _load_shadow_would_trade_index(day)
    out = NearMissAnalysis(day=day)

    for row in iter_events(day=day, event_type="signal_eval"):
        payload = row.get("payload") or {}
        if payload.get("would_fire"):
            continue
        adj = float(payload.get("adjusted_score") or 0)
        if adj < min_conf or adj > max_conf:
            continue
        out.near_miss_evals += 1
        epic = str(row.get("epic") or "")
        direction = str(payload.get("direction") or "").upper()
        setup_key = str(payload.get("setup_key") or "")
        gates = payload.get("gates_passed") or []
        if "signal_confidence" not in gates:
            out.blocked_signal_confidence += 1
        if direction in ("BUY", "SELL"):
            out.near_miss_with_direction += 1
        if setup_key:
            out.by_setup[setup_key] += 1
            out.estimated_e_gbp_sum += setup_e.get(setup_key, 0.0)
        if epic:
            out.by_epic[epic] += 1
        ts = _parse_ts(str(row.get("ts") or ""))
        if ts and _shadow_match_within_sec(epic, ts, shadow_idx):
            out.shadow_would_trade_same_epic += 1

    return out


def near_miss_to_dict(analysis: NearMissAnalysis) -> dict[str, Any]:
    return {
        "day": analysis.day,
        "near_miss_evals": analysis.near_miss_evals,
        "near_miss_with_direction": analysis.near_miss_with_direction,
        "blocked_signal_confidence": analysis.blocked_signal_confidence,
        "shadow_would_trade_same_epic": analysis.shadow_would_trade_same_epic,
        "estimated_counterfactual_e_gbp": round(analysis.estimated_e_gbp_sum, 2),
        "top_setups_near_miss": dict(analysis.by_setup.most_common(8)),
        "by_epic": dict(analysis.by_epic.most_common()),
    }
