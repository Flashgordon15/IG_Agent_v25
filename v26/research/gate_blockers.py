"""Analyze feeder gate failures and signal confidence — useful on no-trade days."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from ingest.lake_reader import iter_events, summarize_day


@dataclass
class GateBlockerReport:
    day: str
    total_events: int = 0
    signal_evals: int = 0
    would_fire: int = 0
    order_intents: int = 0
    fill_closes: int = 0
    failed_gates: Counter[str] = field(default_factory=Counter)
    failed_by_epic: dict[str, Counter[str]] = field(
        default_factory=lambda: defaultdict(Counter)
    )
    confidence_buckets: Counter[str] = field(default_factory=Counter)
    near_miss_70_75: int = 0
    near_miss_75_80: int = 0
    top_setups_evaluated: Counter[str] = field(default_factory=Counter)


def _confidence_bucket(score: float) -> str:
    if score < 50:
        return "<50"
    if score < 60:
        return "50-59"
    if score < 70:
        return "60-69"
    if score < 75:
        return "70-74"
    if score < 80:
        return "75-79"
    if score < 90:
        return "80-89"
    return "90+"


def build_gate_blocker_report(*, day: str) -> GateBlockerReport:
    summary = summarize_day(day)
    report = GateBlockerReport(
        day=day,
        total_events=summary.total_events,
        signal_evals=summary.signal_evals,
        would_fire=summary.would_fire,
        order_intents=summary.order_intents,
        fill_closes=summary.fill_closes,
    )

    for row in iter_events(day=day):
        et = str(row.get("event_type") or "")
        epic = str(row.get("epic") or "")
        payload = row.get("payload") or {}

        if et == "gate_result" and not payload.get("passed"):
            gate = str(payload.get("gate_name") or "unknown")
            report.failed_gates[gate] += 1
            if epic:
                report.failed_by_epic[epic][gate] += 1

        if et == "signal_eval":
            adj = float(payload.get("adjusted_score") or 0)
            report.confidence_buckets[_confidence_bucket(adj)] += 1
            if 70 <= adj < 75:
                report.near_miss_70_75 += 1
            if 75 <= adj < 80:
                report.near_miss_75_80 += 1
            sk = str(payload.get("setup_key") or "")
            if sk:
                report.top_setups_evaluated[sk] += 1

    return report


def report_to_dict(report: GateBlockerReport) -> dict[str, Any]:
    return {
        "day": report.day,
        "total_events": report.total_events,
        "signal_evals": report.signal_evals,
        "would_fire": report.would_fire,
        "order_intents": report.order_intents,
        "fill_closes": report.fill_closes,
        "failed_gates": dict(report.failed_gates.most_common()),
        "failed_by_epic": {
            epic: dict(cnt.most_common()) for epic, cnt in report.failed_by_epic.items()
        },
        "confidence_buckets": dict(report.confidence_buckets.most_common()),
        "near_miss": {
            "70_74_pct": report.near_miss_70_75,
            "75_79_pct": report.near_miss_75_80,
        },
        "top_setups_evaluated": dict(report.top_setups_evaluated.most_common(10)),
    }
