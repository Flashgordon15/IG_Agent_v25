"""L1 certification — 14d max forward soak (£10k); historical edge via L2/OHLC."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ingest.lake_reader import iter_events, summarize_day
from research.cert_config import load_cert_config


@dataclass
class L1DayMetrics:
    day: str
    would_fire: int = 0
    order_intents: int = 0
    fill_closes: int = 0
    fill_pnl_gbp: float = 0.0
    signal_evals: int = 0


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _ohlc_replay_wr() -> float | None:
    """Historical WR from offline OHLC replay (no calendar wait)."""
    path = _project_root() / "data_lake" / "state" / "v26_ohlc_replay.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        lab = data.get("bar_lab") or {}
        decided = int(lab.get("decided") or 0)
        wins = int(lab.get("wins") or 0)
        if decided <= 0:
            return None
        return wins / decided
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


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
    cert = load_cert_config()
    window = int(cert.get("l1_window_days") or 14)
    min_days = int(cert.get("l1_min_days") or 14)
    median_target = float(cert.get("l1_median_daily_gbp") or 100)
    stretch_target = float(cert.get("l1_stretch_day_gbp") or 250)
    pct_stretch = float(cert.get("l1_min_pct_stretch_days") or 0.21)
    max_dd_cap = float(cert.get("l1_max_drawdown_gbp") or 500)
    ohlc_wr_min = float(cert.get("l1_ohlc_wr_min") or 0.52)

    # Only the latest N UTC days count toward the soak window.
    soak_days = list(days[-window:]) if len(days) > window else list(days)
    metrics = collect_daily_metrics(soak_days)
    pnls = [m.fill_pnl_gbp for m in metrics]
    n = len(metrics)
    trading_days = sum(1 for m in metrics if m.fill_closes > 0)
    days_stretch = sum(1 for p in pnls if p >= stretch_target)
    days_positive = sum(1 for p in pnls if p > 0)
    total_pnl = sum(pnls)
    median_daily = sorted(pnls)[n // 2] if n else 0.0
    max_dd = _max_drawdown(pnls)
    pct_stretch_actual = days_stretch / n if n else 0.0
    ohlc_wr = _ohlc_replay_wr()
    ohlc_ok = ohlc_wr is not None and ohlc_wr >= ohlc_wr_min

    issues: list[str] = []
    if n < min_days:
        issues.append(f"soak {n}/{min_days} UTC days")
    if trading_days < max(3, min_days // 2):
        issues.append(f"trading days {trading_days} (need ≥{max(3, min_days // 2)})")
    if median_daily < median_target:
        issues.append(f"median £{median_daily:.0f} < £{median_target:.0f}")
    if pct_stretch_actual < pct_stretch:
        need = max(1, int(round(pct_stretch * min_days)))
        issues.append(
            f"days≥£{stretch_target:.0f}: {days_stretch}/{need} "
            f"({pct_stretch_actual:.0%} < {pct_stretch:.0%})"
        )
    if max_dd > max_dd_cap:
        issues.append(f"max DD £{max_dd:.0f} > £{max_dd_cap:.0f}")
    if not ohlc_ok:
        if ohlc_wr is None:
            issues.append("OHLC replay missing — run v26_phase_a_refresh")
        else:
            issues.append(f"OHLC WR {ohlc_wr:.1%} < {ohlc_wr_min:.0%}")

    return {
        "level": "L1",
        "status": "PASS" if not issues else "INSUFFICIENT",
        "days_available": n,
        "days_required": min_days,
        "window_days": window,
        "issues": issues,
        "metrics": {
            "total_pnl_gbp": round(total_pnl, 2),
            "median_daily_gbp": round(median_daily, 2),
            "days_ge_stretch_gbp": days_stretch,
            "stretch_target_gbp": stretch_target,
            "pct_days_ge_stretch": round(pct_stretch_actual, 4),
            "days_positive": days_positive,
            "trading_days": trading_days,
            "max_drawdown_gbp": round(max_dd, 2),
            "total_would_fire": sum(m.would_fire for m in metrics),
            "total_fills": sum(m.fill_closes for m in metrics),
            "ohlc_replay_wr": round(ohlc_wr, 4) if ohlc_wr is not None else None,
            "ohlc_replay_ok": ohlc_ok,
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
            "L1 = 14d max forward soak on demo fills. "
            "Historical edge from OHLC replay (L2) — no 90d calendar wait."
        ),
    }
