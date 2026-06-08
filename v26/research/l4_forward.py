"""L4 demo forward + L5 prove-window certification from feeder fill_close P&L."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ingest.lake_reader import summarize_day
from research.l1_certification import collect_daily_metrics
from research.learning_engine import list_event_days


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _load_cert_config() -> dict[str, Any]:
    cfg = _read_json(_project_root() / "config" / "config_v26.json")
    cert = dict(cfg.get("certification") or {})
    milestones = cfg.get("milestones") or {}
    targets = milestones.get("targets") or {}
    cert.setdefault("l4_window_days", 14)
    cert.setdefault("l4_median_daily_gbp", float(targets.get("M1") or 100))
    cert.setdefault("l4_min_profit_factor", 1.2)
    cert.setdefault("l4_min_trading_days", 7)
    cert.setdefault("l5_window_days", int(milestones.get("prove_days") or 14))
    cert.setdefault("l5_days_required", 10)
    cert.setdefault("l5_daily_target_gbp", float(targets.get("M2") or 250))
    return cert


@dataclass
class ForwardDay:
    day: str
    fill_closes: int = 0
    fill_pnl_gbp: float = 0.0
    order_intents: int = 0


def collect_forward_days(days: list[str]) -> list[ForwardDay]:
    metrics = {m.day: m for m in collect_daily_metrics(days)}
    out: list[ForwardDay] = []
    for day in sorted(days):
        m = metrics.get(day)
        if m is None:
            s = summarize_day(day)
            out.append(ForwardDay(day=day))
            continue
        out.append(
            ForwardDay(
                day=day,
                fill_closes=m.fill_closes,
                order_intents=m.order_intents,
                fill_pnl_gbp=m.fill_pnl_gbp,
            )
        )
    return out


def _profit_factor(pnls: list[float]) -> float:
    wins = sum(p for p in pnls if p > 0)
    losses = abs(sum(p for p in pnls if p < 0))
    if losses <= 0:
        return wins if wins > 0 else 0.0
    return wins / losses


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[len(s) // 2]


def evaluate_l4_forward(*, window_days: int | None = None) -> dict[str, Any]:
    cert = _load_cert_config()
    window = int(window_days or cert.get("l4_window_days") or 14)
    all_days = list_event_days(max_days=max(window, 90))
    recent = all_days[-window:] if len(all_days) > window else all_days
    rows = collect_forward_days(recent)
    pnls = [r.fill_pnl_gbp for r in rows]
    trading_days = sum(1 for r in rows if r.fill_closes > 0)
    median_daily = _median(pnls)
    total_pnl = sum(pnls)
    pf = _profit_factor(pnls)

    min_median = float(cert.get("l4_median_daily_gbp") or 100)
    min_pf = float(cert.get("l4_min_profit_factor") or 1.2)
    min_days = int(cert.get("l4_min_trading_days") or 7)

    issues: list[str] = []
    if trading_days < min_days:
        issues.append(f"trading days {trading_days}/{min_days}")
    if median_daily < min_median:
        issues.append(f"median £{median_daily:.0f} < £{min_median:.0f}")
    if pf < min_pf:
        issues.append(f"PF {pf:.2f} < {min_pf:.2f}")

    started_at = recent[0] if recent else ""
    if trading_days > 0:
        for r in rows:
            if r.fill_closes > 0:
                started_at = r.day
                break

    status = "PASS" if not issues and trading_days >= min_days else "IN_PROGRESS"
    if trading_days == 0:
        status = "NOT_STARTED"

    pct = 0
    if trading_days > 0:
        parts = [
            min(100, int(100 * trading_days / min_days)),
            min(100, int(100 * median_daily / min_median)) if min_median else 0,
            min(100, int(100 * pf / min_pf)) if min_pf else 0,
        ]
        pct = int(sum(parts) / len(parts))

    return {
        "status": status,
        "pct": pct,
        "window_days": window,
        "trading_days": trading_days,
        "min_trading_days": min_days,
        "median_daily_gbp": round(median_daily, 2),
        "min_median_daily_gbp": min_median,
        "total_pnl_gbp": round(total_pnl, 2),
        "profit_factor": round(pf, 2),
        "min_profit_factor": min_pf,
        "started_at": started_at,
        "issues": issues,
        "daily": [
            {
                "day": r.day,
                "fill_closes": r.fill_closes,
                "fill_pnl_gbp": r.fill_pnl_gbp,
                "order_intents": r.order_intents,
            }
            for r in rows
        ],
    }


def evaluate_l5_prove(*, window_days: int | None = None) -> dict[str, Any]:
    cert = _load_cert_config()
    window = int(window_days or cert.get("l5_window_days") or 14)
    required = int(cert.get("l5_days_required") or 10)
    target = float(cert.get("l5_daily_target_gbp") or 250)

    all_days = list_event_days(max_days=max(window, 90))
    recent = all_days[-window:] if len(all_days) > window else all_days
    rows = collect_forward_days(recent)
    pnls = [r.fill_pnl_gbp for r in rows]
    days_hit = sum(1 for p in pnls if p >= target)
    n = len(pnls)

    issues: list[str] = []
    if n < window:
        issues.append(f"need {window} days, have {n}")
    if days_hit < required:
        issues.append(f"days≥£{target:.0f}: {days_hit}/{required}")

    status = "PASS" if not issues and days_hit >= required else "IN_PROGRESS"
    if n == 0 or sum(1 for r in rows if r.fill_closes > 0) == 0:
        status = "NOT_STARTED"

    pct = min(100, int(100 * days_hit / required)) if required else 0

    return {
        "status": status,
        "pct": pct,
        "window_days": window,
        "days_required": required,
        "days_hit_target": days_hit,
        "daily_target_gbp": target,
        "issues": issues,
    }


def write_forward_cert(*, window_days: int | None = None) -> Path:
    l4 = evaluate_l4_forward(window_days=window_days)
    l5 = evaluate_l5_prove(window_days=window_days)
    root = _project_root() / "data_lake" / "state"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "v26_forward_cert.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": "demo_forward",
        "note": "Tracks v25 demo fill_close P&L via feeder lake (no duplicate orders)",
        "l4": l4,
        "l5": l5,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def format_forward_status() -> str:
    data = _read_json(_project_root() / "data_lake" / "state" / "v26_forward_cert.json")
    l4 = data.get("l4") or {}
    l5 = data.get("l5") or {}
    return (
        f"L4 {l4.get('status')} — median £{l4.get('median_daily_gbp', 0)} "
        f"PF {l4.get('profit_factor', 0)} ({l4.get('trading_days', 0)}d) | "
        f"L5 {l5.get('status')} — {l5.get('days_hit_target', 0)}/"
        f"{l5.get('days_required', 10)} days ≥ £{l5.get('daily_target_gbp', 0)}"
    )
