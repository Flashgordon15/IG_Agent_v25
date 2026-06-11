"""Multi-day gate blocker rollup + relaxation recommendations."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from research.gate_blockers import build_gate_blocker_report, report_to_dict
from research.shadow_expectancy import analyze_near_miss, near_miss_to_dict


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _utc_days_back(n: int) -> list[str]:
    today = datetime.now(timezone.utc).date()
    return [
        (today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n - 1, -1, -1)
    ]


def rollup_gate_blockers(*, days: int = 7) -> dict[str, Any]:
    day_list = _utc_days_back(days)
    total_failed: Counter[str] = Counter()
    by_epic: dict[str, Counter[str]] = defaultdict(Counter)
    near_miss_total = 0
    shadow_match_total = 0
    est_e_gbp_total = 0.0
    fill_closes = 0
    would_fire = 0

    per_day: list[dict[str, Any]] = []
    for day in day_list:
        report = report_to_dict(build_gate_blocker_report(day=day))
        nm = near_miss_to_dict(analyze_near_miss(day=day))
        per_day.append(
            {
                "day": day,
                "would_fire": report.get("would_fire", 0),
                "fill_closes": report.get("fill_closes", 0),
                "failed_gates": report.get("failed_gates") or {},
                "near_miss_evals": nm.get("near_miss_evals", 0),
                "shadow_match": nm.get("shadow_would_trade_same_epic", 0),
                "est_e_gbp": nm.get("estimated_counterfactual_e_gbp", 0),
            }
        )
        for gate, n in (report.get("failed_gates") or {}).items():
            total_failed[gate] += int(n)
        for epic, gates in (report.get("failed_by_epic") or {}).items():
            for gate, n in gates.items():
                by_epic[epic][gate] += int(n)
        near_miss_total += int(nm.get("near_miss_evals") or 0)
        shadow_match_total += int(nm.get("shadow_would_trade_same_epic") or 0)
        est_e_gbp_total += float(nm.get("estimated_counterfactual_e_gbp") or 0)
        fill_closes += int(report.get("fill_closes") or 0)
        would_fire += int(report.get("would_fire") or 0)

    ranked = [
        {
            "gate": gate,
            "fail_count": count,
            "weight": round(count / max(1, sum(total_failed.values())), 3),
        }
        for gate, count in total_failed.most_common()
    ]

    return {
        "window_days": days,
        "days": day_list,
        "totals": {
            "would_fire": would_fire,
            "fill_closes": fill_closes,
            "near_miss_evals": near_miss_total,
            "shadow_would_trade": shadow_match_total,
            "estimated_counterfactual_e_gbp": round(est_e_gbp_total, 2),
        },
        "ranked_blockers": ranked,
        "failed_by_epic": {
            epic: dict(cnt.most_common(6)) for epic, cnt in by_epic.items()
        },
        "per_day": per_day,
    }


def recommend_relaxations(rollup: dict[str, Any]) -> list[dict[str, Any]]:
    """Suggest safe relaxations when shadow counterfactual E£ is positive."""
    recs: list[dict[str, Any]] = []
    totals = rollup.get("totals") or {}
    est_e = float(totals.get("estimated_counterfactual_e_gbp") or 0)
    shadow = int(totals.get("shadow_would_trade") or 0)
    ranked = rollup.get("ranked_blockers") or []
    top_gate = ranked[0]["gate"] if ranked else ""

    index_epics = [
        epic
        for epic, gates in (rollup.get("failed_by_epic") or {}).items()
        if gates.get("environment_fitness", 0) >= gates.get("signal_confidence", 0)
        and epic.startswith("IX.")
    ]

    if (
        est_e > 0
        and shadow > 0
        and top_gate in ("environment_fitness", "signal_confidence")
    ):
        if top_gate == "environment_fitness" or index_epics:
            recs.append(
                {
                    "id": "fitness_min_indices",
                    "gate": "environment_fitness",
                    "action": "Lower fitness floor 55% → 52% for US indices when points HEALTHY",
                    "epics": index_epics or ["IX.D.DOW.IFM.IP", "IX.D.NASDAQ.IFM.IP"],
                    "config_key": "gate_relaxations.fitness_min",
                    "value": 52,
                    "require_points_healthy": True,
                    "evidence": (
                        f"7d near-miss est E£{est_e:.0f}, shadow match {shadow}, "
                        f"top blocker {top_gate}"
                    ),
                    "safe": True,
                }
            )

    if top_gate == "signal_confidence" and int(totals.get("near_miss_evals") or 0) > 10:
        recs.append(
            {
                "id": "points_warning_review",
                "gate": "signal_confidence",
                "action": "Review points WARNING bar (92%) — not auto-relaxed",
                "epics": [],
                "safe": False,
                "evidence": "High near-miss count; threshold raised by loss streak",
            }
        )

    return recs


def write_gate_relaxation_report(*, days: int = 7) -> Path:
    rollup = rollup_gate_blockers(days=days)
    recommendations = recommend_relaxations(rollup)
    root = _project_root() / "data_lake" / "state"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "v26_gate_relaxation_report.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rollup": rollup,
        "recommendations": recommendations,
        "active_relaxation": _load_active_relaxation(),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _load_active_relaxation() -> dict[str, Any]:
    try:
        import sys

        src = _project_root() / "src"
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
        from system.gate_relaxation import relaxation_snapshot

        return relaxation_snapshot()
    except Exception:
        return {}
