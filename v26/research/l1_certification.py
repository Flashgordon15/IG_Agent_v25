"""L1 certification metrics from feeder lake (learning proxy until 90d fills)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ingest.lake_reader import iter_events, summarize_day

# IG Agent v26 framework L1 targets (50k envelope reference)
L1_MIN_DAYS = 90
L1_MIN_PCT_DAYS_1K = 0.30
L1_MEDIAN_DAILY_GBP = 500.0
L1_MAX_DD_GBP = 4000.0
L1_STRETCH_DAY_GBP = 1000.0


@dataclass
class L1DayMetrics:
    day: str
    would_fire: int = 0
    order_intents: int = 0
    fill_closes: int = 0
    fill_pnl_gbp: float = 0.0
    signal_evals: int = 0


def collect_daily_metrics(days: list[str]) -> list[L1DayMetrics]:
    rows: list[L1DayMetrics] = []
    for day in days:
        s = summarize_day(day)
        pnl = 0.0
        for ev in iter_events(day=day, event_type="fill_close"):
            pnl += float((ev.get("payload") or {}).get("pnl_gbp") or 0)
        rows.append(
            L1DayMetrics(
                day=day,
                would_fire=s.would_fire,
                order_intents=s.order_intents,
                fill_closes=s.fill_closes,
                fill_pnl_gbp=round(pnl, 2),
                signal_evals=s.signal_evals,
            )
        )
    return rows


def _max_drawdown(daily_pnls: list[float]) -> float:
    peak = 0.0
    equity = 0.0
    max_dd = 0.0
    for p in daily_pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def evaluate_l1(days: list[str]) -> dict[str, Any]:
    metrics = collect_daily_metrics(days)
    pnls = [m.fill_pnl_gbp for m in metrics]
    n = len(metrics)
    days_1k = sum(1 for p in pnls if p >= L1_STRETCH_DAY_GBP)
    days_positive = sum(1 for p in pnls if p > 0)
    total_pnl = sum(pnls)
    median_daily = sorted(pnls)[n // 2] if n else 0.0
    max_dd = _max_drawdown(pnls)
    pct_1k = days_1k / n if n else 0.0

    issues: list[str] = []
    if n < L1_MIN_DAYS:
        issues.append(f"need {L1_MIN_DAYS} days, have {n}")
    if pct_1k < L1_MIN_PCT_DAYS_1K:
        issues.append(f"days≥£1k {pct_1k:.0%} < {L1_MIN_PCT_DAYS_1K:.0%}")
    if median_daily < L1_MEDIAN_DAILY_GBP:
        issues.append(f"median £{median_daily:.0f} < £{L1_MEDIAN_DAILY_GBP:.0f}")
    if max_dd > L1_MAX_DD_GBP:
        issues.append(f"max DD £{max_dd:.0f} > £{L1_MAX_DD_GBP:.0f}")

    return {
        "level": "L1",
        "status": "PASS" if not issues else "INSUFFICIENT",
        "days_available": n,
        "days_required": L1_MIN_DAYS,
        "issues": issues,
        "metrics": {
            "total_pnl_gbp": round(total_pnl, 2),
            "median_daily_gbp": round(median_daily, 2),
            "days_ge_1000_gbp": days_1k,
            "pct_days_ge_1000": round(pct_1k, 4),
            "days_positive": days_positive,
            "max_drawdown_gbp": round(max_dd, 2),
            "total_would_fire": sum(m.would_fire for m in metrics),
            "total_fills": sum(m.fill_closes for m in metrics),
        },
        "daily": [
            {
                "day": m.day,
                "would_fire": m.would_fire,
                "order_intents": m.order_intents,
                "fills": m.fill_closes,
                "pnl_gbp": m.fill_pnl_gbp,
                "signal_evals": m.signal_evals,
            }
            for m in metrics
        ],
        "note": (
            "L1 uses live fill_close P&L per UTC day. "
            "Need 90d window for formal cert; shadow replay extends counterfactual."
        ),
    }
