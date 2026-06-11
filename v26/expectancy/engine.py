"""Read-only rolling expectancy from feeder fill_close events."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ingest.lake_reader import iter_events

ACTIVE_WR_MIN = 0.52
BAN_WR_BELOW = 0.45
MIN_TRADES_BAN = 20
MIN_TRADES_PROBE = 30


@dataclass
class SetupStats:
    setup_key: str
    n: int
    wins: int
    losses: int
    wr: float
    avg_win_gbp: float
    avg_loss_gbp: float
    e_gbp: float
    total_pnl_gbp: float
    status: str  # ACTIVE | PROBE | BANNED | INSUFFICIENT


def _classify_status(n: int, wr: float, e_gbp: float) -> str:
    if n < MIN_TRADES_BAN:
        return "INSUFFICIENT"
    if wr < BAN_WR_BELOW or e_gbp < 0:
        return "BANNED"
    if n < MIN_TRADES_PROBE or wr < ACTIVE_WR_MIN:
        return "PROBE"
    return "ACTIVE"


def _days_back(n: int) -> list[str]:
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]


def collect_fills(*, days: int = 14) -> list[dict[str, Any]]:
    fills: list[dict[str, Any]] = []
    for day in _days_back(days):
        for row in iter_events(day=day, event_type="fill_close"):
            payload = row.get("payload") or {}
            fills.append(
                {
                    "day": day,
                    "epic": row.get("epic"),
                    "setup_key": str(payload.get("setup_key") or "unknown"),
                    "pnl_gbp": float(payload.get("pnl_gbp") or 0),
                    "result": str(payload.get("result") or "").upper(),
                }
            )
    return fills


def compute_setup_stats(fills: list[dict[str, Any]]) -> list[SetupStats]:
    by_setup: dict[str, list[float]] = {}
    results: dict[str, list[str]] = {}
    for f in fills:
        sk = f["setup_key"]
        by_setup.setdefault(sk, []).append(float(f["pnl_gbp"]))
        results.setdefault(sk, []).append(str(f.get("result") or ""))

    stats: list[SetupStats] = []
    for setup_key, pnls in sorted(by_setup.items()):
        n = len(pnls)
        res = results[setup_key]
        wins = sum(
            1
            for p, r in zip(pnls, res, strict=False)
            if r == "WIN" or (r not in ("WIN", "LOSS", "BREAKEVEN") and p > 0)
        )
        losses = sum(
            1
            for p, r in zip(pnls, res, strict=False)
            if r == "LOSS" or (r not in ("WIN", "LOSS", "BREAKEVEN") and p < 0)
        )
        win_pnls = [p for p in pnls if p > 0]
        loss_pnls = [p for p in pnls if p < 0]
        wr = wins / n if n else 0.0
        avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0.0
        avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0
        e_gbp = sum(pnls) / n if n else 0.0
        total = sum(pnls)
        stats.append(
            SetupStats(
                setup_key=setup_key,
                n=n,
                wins=wins,
                losses=losses,
                wr=round(wr, 4),
                avg_win_gbp=round(avg_win, 2),
                avg_loss_gbp=round(avg_loss, 2),
                e_gbp=round(e_gbp, 2),
                total_pnl_gbp=round(total, 2),
                status=_classify_status(n, wr, e_gbp),
            )
        )
    return sorted(stats, key=lambda s: s.total_pnl_gbp, reverse=True)


def portfolio_summary(fills: list[dict[str, Any]]) -> dict[str, Any]:
    if not fills:
        return {"n": 0, "wr": 0.0, "e_gbp": 0.0, "total_pnl_gbp": 0.0}
    pnls = [float(f["pnl_gbp"]) for f in fills]
    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    return {
        "n": n,
        "wr": round(wins / n, 4),
        "e_gbp": round(sum(pnls) / n, 2),
        "total_pnl_gbp": round(sum(pnls), 2),
    }


def write_snapshot(*, days: int = 14) -> Path:
    fills = collect_fills(days=days)
    setups = compute_setup_stats(fills)
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rolling_days": days,
        "portfolio": portfolio_summary(fills),
        "setups": [asdict(s) for s in setups],
    }
    root = Path(__file__).resolve().parents[2] / "data_lake" / "state"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "expectancy_snapshot.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    write_setup_registry(setups=setups, days=days)
    return path


def write_setup_registry(
    *,
    setups: list[SetupStats] | None = None,
    days: int = 14,
) -> Path:
    """Mirror banned setups into v25 live gate file."""
    if setups is None:
        fills = collect_fills(days=days)
        setups = compute_setup_stats(fills)
    import sys

    src_root = Path(__file__).resolve().parents[2] / "src"
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))
    from system.setup_registry import write_registry_from_stats

    banned_n = sum(1 for s in setups if s.status == "BANNED")
    return write_registry_from_stats(
        setups,
        rolling_days=days,
        enabled=banned_n > 0,
    )
