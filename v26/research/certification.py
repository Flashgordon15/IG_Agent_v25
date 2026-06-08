"""L0–L5 certification ladder for v26 CERT tab."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research.cert_config import max_soak_days
from research.l1_certification import evaluate_l1
from research.learning_engine import list_event_days


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _level_l0() -> dict[str, Any]:
    exp = _read_json(
        _project_root() / "data_lake" / "state" / "expectancy_snapshot.json"
    )
    ok = bool(exp.get("portfolio"))
    return {
        "id": "L0",
        "name": "P&L audit",
        "status": "PASS" if ok else "PENDING",
        "pct": 100 if ok else 0,
        "detail": "Rolling expectancy snapshot from feeder fills",
    }


def _level_l1(days: list[str]) -> dict[str, Any]:
    l1 = evaluate_l1(days)
    n = int(l1.get("days_available") or 0)
    req = int(l1.get("days_required") or max_soak_days())
    window = int(l1.get("window_days") or req)
    pct = min(100, int(100 * n / req)) if req else 0
    ohlc_wr = (l1.get("metrics") or {}).get("ohlc_replay_wr")
    ohlc_note = f" · OHLC {ohlc_wr:.0%}" if ohlc_wr is not None else ""
    return {
        "id": "L1",
        "name": f"Soak {window}d",
        "status": l1.get("status") or "INSUFFICIENT",
        "pct": pct,
        "detail": (
            f"{n}/{req} soak days · median £"
            f"{l1.get('metrics', {}).get('median_daily_gbp', 0)}{ohlc_note}"
        ),
        "metrics": l1.get("metrics"),
    }


def _level_l2() -> dict[str, Any]:
    ohlc = _read_json(_project_root() / "data_lake" / "state" / "v26_ohlc_replay.json")
    wf = ohlc.get("walk_forward") or {}
    epics = wf.get("by_epic") or {}
    with_wr = sum(1 for e in epics.values() if e.get("best_wr") is not None)
    total = len(epics) or 1
    pct = int(100 * with_wr / total)
    status = "PASS" if with_wr >= max(1, total // 2) else "IN_PROGRESS"
    return {
        "id": "L2",
        "name": "Walk-forward",
        "status": status,
        "pct": pct,
        "detail": f"{with_wr}/{total} epics with threshold curve",
    }


def _level_l3() -> dict[str, Any]:
    shadow = _read_json(
        _project_root() / "data_lake" / "state" / "shadow_strategy_pnl.json"
    )
    total = float(
        shadow.get("by_strategy", {}).get("S1_rules_v25", {}).get("total_pnl_gbp", 0)
        or 0
    )
    ok = total > 0
    return {
        "id": "L3",
        "name": "Shadow 14d",
        "status": "PASS" if ok else "IN_PROGRESS",
        "pct": 60 if ok else 20,
        "detail": f"Shadow attributed P&L £{total:+.2f}",
    }


def _level_l4() -> dict[str, Any]:
    forward = _read_json(
        _project_root() / "data_lake" / "state" / "v26_forward_cert.json"
    )
    l4 = forward.get("l4") or {}
    if not l4:
        try:
            from research.l4_forward import evaluate_l4_forward

            l4 = evaluate_l4_forward()
        except Exception:
            l4 = {}
    status = str(l4.get("status") or "NOT_STARTED")
    median = float(l4.get("median_daily_gbp") or 0)
    pf = float(l4.get("profit_factor") or 0)
    min_med = float(l4.get("min_median_daily_gbp") or 100)
    return {
        "id": "L4",
        "name": "Demo forward",
        "status": status,
        "pct": int(l4.get("pct") or 0),
        "detail": (
            f"{l4.get('trading_days', 0)}d · median £{median:.0f} "
            f"(≥£{min_med:.0f}) · PF {pf:.2f}"
        ),
        "metrics": l4,
    }


def _level_l5() -> dict[str, Any]:
    forward = _read_json(
        _project_root() / "data_lake" / "state" / "v26_forward_cert.json"
    )
    l5 = forward.get("l5") or {}
    if not l5:
        try:
            from research.l4_forward import evaluate_l5_prove

            l5 = evaluate_l5_prove()
        except Exception:
            l5 = {}
    status = str(l5.get("status") or "NOT_STARTED")
    hit = int(l5.get("days_hit_target") or 0)
    req = int(l5.get("days_required") or 10)
    target = float(l5.get("daily_target_gbp") or 250)
    return {
        "id": "L5",
        "name": f"{req}/{l5.get('window_days', 14)} ≥ £{target:.0f}",
        "status": status,
        "pct": int(l5.get("pct") or 0),
        "detail": f"{hit}/{req} days ≥ £{target:.0f} in prove window",
        "metrics": l5,
    }


def build_certification_payload(*, max_days: int | None = None) -> dict[str, Any]:
    window = max_days if max_days is not None else max_soak_days()
    days = list_event_days(max_days=window)
    levels = [
        _level_l0(),
        _level_l1(days),
        _level_l2(),
        _level_l3(),
        _level_l4(),
        _level_l5(),
    ]
    passed = sum(1 for lv in levels if lv.get("status") == "PASS")
    return {
        "ok": True,
        "target": "L5",
        "levels": levels,
        "passed_count": passed,
        "total_levels": len(levels),
        "current_milestone": _read_json(_project_root() / "config" / "config_v26.json")
        .get("milestones", {})
        .get("current", "M0"),
    }
