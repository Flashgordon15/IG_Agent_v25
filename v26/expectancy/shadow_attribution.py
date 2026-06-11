"""Attribute v25 fill_close P&L to v26 shadow strategies (counterfactual)."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from expectancy.engine import _days_back
from ingest.lake_reader import iter_events
from shadow.runner import shadow_dir


def _direction_from_payload(payload: dict[str, Any]) -> str:
    """Resolve BUY/SELL from feeder payload (fill_close often omits direction)."""
    direction = str(payload.get("direction") or payload.get("side") or "").upper()
    if direction in ("BUY", "SELL"):
        return direction
    setup_key = str(payload.get("setup_key") or "")
    if "|" in setup_key:
        prefix = setup_key.split("|", 1)[0].upper()
        if prefix in ("BUY", "SELL"):
            return prefix
    return ""


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


def load_shadow_would_trades(*, days: int = 14) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for day in _days_back(days):
        path = shadow_dir() / f"{day}.jsonl"
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = row.get("payload") or {}
            if not payload.get("would_trade"):
                continue
            rows.append(row)
    rows.sort(key=lambda r: str(r.get("ts") or ""))
    return rows


def load_fill_closes(*, days: int = 14) -> list[dict[str, Any]]:
    fills: list[dict[str, Any]] = []
    for day in _days_back(days):
        for row in iter_events(day=day, event_type="fill_close"):
            fills.append(row)
    fills.sort(key=lambda r: str(r.get("ts") or ""))
    return fills


@dataclass
class AttributedFill:
    strategy_id: str
    epic: str
    direction: str
    pnl_gbp: float
    result: str
    fill_ts: str
    intent_ts: str
    lag_sec: float


def _best_shadow_match(
    candidates: list[dict[str, Any]],
    *,
    fill_ts: datetime,
    direction: str,
    setup_key: str,
    max_lag_sec: float,
) -> tuple[dict[str, Any] | None, float]:
    """Pick nearest prior would_trade intent; prefer exact setup_key when present."""
    best: dict[str, Any] | None = None
    best_lag = max_lag_sec + 1.0
    for s in candidates:
        sp = s.get("payload") or {}
        sd = _direction_from_payload(sp)
        if direction and sd and sd != direction:
            continue
        shadow_setup = str(sp.get("setup_key") or "")
        if setup_key and shadow_setup and shadow_setup != setup_key:
            continue
        intent_ts = _parse_ts(str(s.get("ts") or ""))
        if intent_ts is None or intent_ts > fill_ts:
            continue
        lag = (fill_ts - intent_ts).total_seconds()
        if 0 <= lag <= max_lag_sec and lag < best_lag:
            best = s
            best_lag = lag
    return best, best_lag


def attribute_fills(
    fills: list[dict[str, Any]],
    shadows: list[dict[str, Any]],
    *,
    max_lag_sec: float = 3600.0,
) -> list[AttributedFill]:
    """Match each fill to the nearest prior would_trade shadow intent (epic + direction/setup)."""
    by_epic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in shadows:
        epic = str(s.get("epic") or "")
        if epic:
            by_epic[epic].append(s)

    out: list[AttributedFill] = []
    for fill in fills:
        epic = str(fill.get("epic") or "")
        fill_ts = _parse_ts(str(fill.get("ts") or ""))
        if not epic or fill_ts is None:
            continue
        payload = fill.get("payload") or {}
        direction = _direction_from_payload(payload)
        setup_key = str(payload.get("setup_key") or "")
        pnl = float(payload.get("pnl_gbp") or 0)
        result = str(payload.get("result") or "").upper()

        candidates = by_epic.get(epic, [])
        best, best_lag = _best_shadow_match(
            candidates,
            fill_ts=fill_ts,
            direction=direction,
            setup_key=setup_key,
            max_lag_sec=max_lag_sec,
        )
        if best is None and setup_key:
            # Relax setup_key but keep direction when fill had no explicit side.
            best, best_lag = _best_shadow_match(
                candidates,
                fill_ts=fill_ts,
                direction=direction,
                setup_key="",
                max_lag_sec=max_lag_sec,
            )

        if best is None:
            continue
        out.append(
            AttributedFill(
                strategy_id=str(best.get("strategy_id") or "unknown"),
                epic=epic,
                direction=direction,
                pnl_gbp=pnl,
                result=result,
                fill_ts=str(fill.get("ts") or ""),
                intent_ts=str(best.get("ts") or ""),
                lag_sec=best_lag,
            )
        )
    return out


def summarize_strategy_pnl(
    attributed: list[AttributedFill],
) -> dict[str, dict[str, float]]:
    by: dict[str, dict[str, float]] = defaultdict(
        lambda: {"n": 0, "wins": 0, "total_pnl_gbp": 0.0}
    )
    for row in attributed:
        bucket = by[row.strategy_id]
        bucket["n"] += 1
        bucket["total_pnl_gbp"] += row.pnl_gbp
        if row.result == "WIN" or (
            row.result not in ("WIN", "LOSS", "BREAKEVEN") and row.pnl_gbp > 0
        ):
            bucket["wins"] += 1
    for sid, bucket in by.items():
        n = int(bucket["n"])
        bucket["wr"] = bucket["wins"] / n if n else 0.0
        bucket["e_gbp"] = bucket["total_pnl_gbp"] / n if n else 0.0
    return dict(by)


def write_strategy_pnl_snapshot(*, days: int = 14) -> Path:
    shadows = load_shadow_would_trades(days=days)
    fills = load_fill_closes(days=days)
    attributed = attribute_fills(fills, shadows)
    summary = summarize_strategy_pnl(attributed)
    root = Path(__file__).resolve().parents[2]
    out_dir = root / "data_lake" / "state"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "shadow_strategy_pnl.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "rolling_days": days,
                "attributed_fills": len(attributed),
                "total_fills": len(fills),
                "by_strategy": summary,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path
