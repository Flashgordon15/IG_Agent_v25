"""Gate binding histogram — WAIT blockers + SUBMIT_TRUTH submit audit."""

from __future__ import annotations

import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG = ROOT / "src" / "data" / "logs" / "engine.log"

_SUBMIT_TRUTH_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) .*SUBMIT_TRUTH epic=(\S+) .*gate_sourced=(\w+).*"
    r"size=([\d.]+) stop=([\d.]+) risk_gbp_gate=([\d.]+) .*cap=([\d.]+)"
)


def _parse_day(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def rollup_submit_truth(
    *,
    log_path: Path = DEFAULT_LOG,
    days: int = 1,
    include_rotated: bool = False,
) -> dict[str, Any]:
    cutoff = date.today() - timedelta(days=max(0, days - 1))
    gate_sourced_counts: Counter[str] = Counter()
    cap_counts: Counter[str] = Counter()
    epic_counts: Counter[str] = Counter()
    lines = 0

    paths = [log_path] if log_path.is_file() else []
    if include_rotated and log_path.parent.is_dir():
        paths = sorted(
            log_path.parent.glob(f"{log_path.name}*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            m = _SUBMIT_TRUTH_RE.search(raw)
            if not m:
                continue
            day_s, epic, sourced, _size, _stop, _risk, cap = m.groups()
            if _parse_day(day_s) < cutoff:
                continue
            lines += 1
            gate_sourced_counts[sourced] += 1
            cap_counts[cap] += 1
            epic_counts[epic] += 1

    total = lines
    return {
        "window_days": days,
        "cutoff_date": cutoff.isoformat(),
        "submit_truth_lines": total,
        "gate_sourced_histogram": [
            {"label": k, "count": v, "pct": round(100 * v / total, 1) if total else 0}
            for k, v in gate_sourced_counts.most_common()
        ],
        "cap_histogram": [
            {"label": k, "count": v, "pct": round(100 * v / total, 1) if total else 0}
            for k, v in cap_counts.most_common()
        ],
        "epic_histogram": [
            {"label": k, "count": v, "pct": round(100 * v / total, 1) if total else 0}
            for k, v in epic_counts.most_common(15)
        ],
    }


def build_gate_binding_report(
    *,
    days: int = 7,
    include_rotated: bool = True,
) -> dict[str, Any]:
    """Combined gate blockers + submit-truth binding for dashboard API."""
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    from gate_attribution_report import rollup_gate_blocks

    blocks = rollup_gate_blocks(
        log_path=DEFAULT_LOG,
        days=days,
        include_rotated=include_rotated,
    )
    submits = rollup_submit_truth(
        log_path=DEFAULT_LOG,
        days=days,
        include_rotated=include_rotated,
    )
    sourced = submits.get("gate_sourced_histogram") or []
    true_count = next(
        (row["count"] for row in sourced if row.get("label") == "True"),
        0,
    )
    total_submit = int(submits.get("submit_truth_lines") or 0)
    gate_sourced_pct = (
        round(100.0 * true_count / total_submit, 1) if total_submit else 0.0
    )
    return {
        "window_days": days,
        "gate_blockers": blocks,
        "submit_truth": submits,
        "binding_summary": {
            "submit_truth_count": total_submit,
            "gate_sourced_true_count": true_count,
            "gate_sourced_pct": gate_sourced_pct,
            "wait_blocks": blocks.get("total_waits", 0),
        },
    }
