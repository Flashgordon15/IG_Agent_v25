"""Diagnose bar_close quality for S2/S3 shadow strategies."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ingest.lake_reader import iter_events


@dataclass
class BarAnalyzerReport:
    day: str
    total_bars: int = 0
    by_epic: Counter[str] = field(default_factory=Counter)
    range_pct_buckets: Counter[str] = field(default_factory=Counter)
    s2_eligible: int = 0
    s3_eligible: int = 0
    s2_would_trade: int = 0
    s3_would_trade: int = 0


def _range_bucket(pct: float) -> str:
    if pct < 0.0002:
        return "<0.02%"
    if pct < 0.0005:
        return "0.02-0.05%"
    if pct < 0.0008:
        return "0.05-0.08%"
    if pct < 0.0015:
        return "0.08-0.15%"
    return ">=0.15%"


def _s2_momentum_would_trade(o: float, h: float, lo: float, c: float) -> bool:
    if c <= 0 or h <= lo:
        return False
    bar_range = h - lo
    if bar_range / c < 0.0008:
        return False
    pos = (c - lo) / bar_range
    if pos >= 0.75 and c > o:
        return True
    if pos <= 0.25 and c < o:
        return True
    return False


def _s3_fx_would_trade(
    epic: str, session: str, o: float, h: float, lo: float, c: float
) -> bool:
    fx_epics = {"CS.D.EURUSD.CFD.IP", "CS.D.GBPUSD.CFD.IP"}
    fx_sessions = {"london_morning", "london_us_overlap", "us_morning"}
    if epic not in fx_epics or session not in fx_sessions:
        return False
    if c <= 0 or h <= lo:
        return False
    bar_range = h - lo
    if bar_range / c < 0.00015:
        return False
    pos = (c - lo) / bar_range
    if pos >= 0.80:
        return True
    if pos <= 0.20:
        return True
    return False


def analyze_bars(*, day: str) -> BarAnalyzerReport:
    report = BarAnalyzerReport(day=day)
    for row in iter_events(day=day, event_type="bar_close"):
        payload = row.get("payload") or {}
        try:
            o = float(payload.get("open") or 0)
            h = float(payload.get("high") or 0)
            lo = float(payload.get("low") or 0)
            c = float(payload.get("close") or 0)
        except (TypeError, ValueError):
            continue
        if c <= 0:
            continue
        report.total_bars += 1
        epic = str(row.get("epic") or "?")
        session = str(row.get("session") or "")
        report.by_epic[epic] += 1
        if h > lo:
            pct = (h - lo) / c
            report.range_pct_buckets[_range_bucket(pct)] += 1
            if pct >= 0.0008:
                report.s2_eligible += 1
            if _s2_momentum_would_trade(o, h, lo, c):
                report.s2_would_trade += 1
            if epic in ("CS.D.EURUSD.CFD.IP", "CS.D.GBPUSD.CFD.IP") and pct >= 0.00015:
                report.s3_eligible += 1
            if _s3_fx_would_trade(epic, session, o, h, lo, c):
                report.s3_would_trade += 1
    return report


def bar_report_to_dict(report: BarAnalyzerReport) -> dict[str, Any]:
    return {
        "day": report.day,
        "total_bars": report.total_bars,
        "by_epic": dict(report.by_epic),
        "range_pct_buckets": dict(report.range_pct_buckets.most_common()),
        "s2_eligible": report.s2_eligible,
        "s2_would_trade": report.s2_would_trade,
        "s3_eligible": report.s3_eligible,
        "s3_would_trade": report.s3_would_trade,
        "s2_tune_hint": (
            "Lower _MIN_RANGE_PCT if s2_eligible << total_bars on indices"
            if report.total_bars and report.s2_eligible < report.total_bars * 0.1
            else "S2 range gate OK for current bar sample"
        ),
    }
