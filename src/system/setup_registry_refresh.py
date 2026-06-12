"""Rebuild live setup registry from agent-only closes in learning_db."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from system.learning_trade_policy import agent_trades_sql_clause
from system.setup_registry import write_registry_from_stats

# Demo-friendly ban thresholds (stricter than v26 feeder MIN_TRADES_BAN=20).
MIN_TRADES_BAN = 5
BAN_WR_BELOW = 0.35
MIN_TRADES_PROBE = 8
ACTIVE_WR_MIN = 0.52


@dataclass
class AgentSetupStats:
    setup_key: str
    n: int
    wins: int
    losses: int
    wr: float
    e_gbp: float
    total_pnl_gbp: float
    status: str


def _classify_status(n: int, wr: float, e_gbp: float, losses: int) -> str:
    if n < MIN_TRADES_BAN:
        return "INSUFFICIENT"
    if wr < BAN_WR_BELOW or e_gbp < 0 or (losses >= n and n >= MIN_TRADES_BAN):
        return "BANNED"
    if n < MIN_TRADES_PROBE or wr < ACTIVE_WR_MIN:
        return "PROBE"
    return "ACTIVE"


def _pnl_gbp(row: Any) -> float:
    keys = row.keys() if hasattr(row, "keys") else ()
    if "ig_pnl_currency" in keys and row["ig_pnl_currency"] is not None:
        try:
            v = float(row["ig_pnl_currency"])
            if abs(v) <= 5000:
                return v
        except (TypeError, ValueError):
            pass
    try:
        pts = float(
            row["pnl_points"] if "pnl_points" in keys else row.get("pnl_points", 0)
        )
        size = float(row["size"] if "size" in keys else row.get("size", 1) or 1)
        if abs(pts) <= 150:
            return pts * size
    except (TypeError, ValueError, AttributeError):
        pass
    return 0.0


def collect_agent_setup_stats(
    store: Any, *, rolling_days: int = 14
) -> list[AgentSetupStats]:
    """Aggregate setup performance from learning_db agent rows only."""
    clause = agent_trades_sql_clause()
    rows = store.conn.execute(
        f"""
        SELECT setup_key, result, ig_pnl_currency, pnl_points, size, closed_at
        FROM trades
        WHERE closed_at IS NOT NULL
          AND {clause}
          AND closed_at >= datetime('now', ?)
        """,
        (f"-{int(rolling_days)} days",),
    ).fetchall()
    by_key: dict[str, list[tuple[str, float]]] = {}
    for row in rows:
        key = str(row["setup_key"] or "").strip()
        if not key:
            continue
        by_key.setdefault(key, []).append((str(row["result"] or ""), _pnl_gbp(row)))

    stats: list[AgentSetupStats] = []
    for setup_key, items in sorted(by_key.items()):
        n = len(items)
        wins = sum(1 for r, _ in items if r == "WIN")
        losses = sum(1 for r, _ in items if r == "LOSS")
        pnls = [p for _, p in items]
        wr = wins / n if n else 0.0
        total = sum(pnls)
        e_gbp = total / n if n else 0.0
        stats.append(
            AgentSetupStats(
                setup_key=setup_key,
                n=n,
                wins=wins,
                losses=losses,
                wr=round(wr, 4),
                e_gbp=round(e_gbp, 2),
                total_pnl_gbp=round(total, 2),
                status=_classify_status(n, wr, e_gbp, losses),
            )
        )
    return sorted(stats, key=lambda s: (s.status != "BANNED", s.total_pnl_gbp))


def refresh_setup_registry_from_store(
    store: Any,
    *,
    rolling_days: int = 14,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """Write setup_registry.json from agent-only learning_db stats."""
    stats = collect_agent_setup_stats(store, rolling_days=rolling_days)
    banned_n = sum(1 for s in stats if s.status == "BANNED")
    gate_on = banned_n > 0 if enabled is None else bool(enabled)
    write_registry_from_stats(
        [
            {
                "setup_key": s.setup_key,
                "status": s.status,
                "n": s.n,
                "wr": s.wr,
                "e_gbp": s.e_gbp,
                "total_pnl_gbp": s.total_pnl_gbp,
            }
            for s in stats
        ],
        rolling_days=rolling_days,
        enabled=gate_on,
    )
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rolling_days": rolling_days,
        "enabled": gate_on,
        "setups_total": len(stats),
        "banned_count": banned_n,
        "banned_keys": [s.setup_key for s in stats if s.status == "BANNED"],
        "setups": [
            {
                "setup_key": s.setup_key,
                "status": s.status,
                "n": s.n,
                "wr": s.wr,
                "e_gbp": s.e_gbp,
                "total_pnl_gbp": s.total_pnl_gbp,
            }
            for s in stats
        ],
    }
