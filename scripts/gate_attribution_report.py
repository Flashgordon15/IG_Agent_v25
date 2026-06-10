#!/usr/bin/env python3
"""Daily gate attribution — rank block reasons from engine.log WAIT lines."""

from __future__ import annotations

import argparse
import re
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG = ROOT / "src" / "data" / "logs" / "engine.log"

_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) \| WAIT — ([^:]+): (.+)$"
)
_CIRCUIT_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) .*CIRCUIT_BREAKER_ACTIVE \| epic=([^\s]+) spread/atr=([\d.]+)"
)
_EXEC_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) .*REJECTED_ASYMMETRIC_RR_FLOOR_GATED")


def _log_paths(log: Path, *, include_rotated: bool) -> list[Path]:
    if not include_rotated:
        return [log] if log.is_file() else []
    log_dir = log.parent
    stem = log.name
    paths = sorted(
        log_dir.glob(f"{stem}*"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return [p for p in paths if p.is_file()]


def _parse_day(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def rollup_gate_blocks(
    *,
    log_path: Path = DEFAULT_LOG,
    days: int = 1,
    include_rotated: bool = False,
) -> dict[str, object]:
    """Parse engine.log and return ranked gate block counts."""
    cutoff = date.today() - timedelta(days=max(0, days - 1))
    gate_counts: Counter[str] = Counter()
    detail_samples: dict[str, str] = {}
    circuit_by_epic: Counter[str] = Counter()
    rr_floor_rejects = 0
    lines_scanned = 0
    wait_lines = 0

    for path in _log_paths(log_path, include_rotated=include_rotated):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            lines_scanned += 1
            m = _LINE_RE.match(raw)
            if m:
                day_s, _ts, gate, detail = m.groups()
                if _parse_day(day_s) < cutoff:
                    continue
                wait_lines += 1
                gate_counts[gate.strip()] += 1
                detail_samples.setdefault(gate.strip(), detail.strip()[:120])
                continue
            cm = _CIRCUIT_RE.match(raw)
            if cm:
                day_s, epic, _ratio = cm.groups()
                if _parse_day(day_s) < cutoff:
                    continue
                circuit_by_epic[epic] += 1
                continue
            if _EXEC_RE.match(raw):
                day_s = raw[:10]
                if _parse_day(day_s) >= cutoff:
                    rr_floor_rejects += 1

    total_waits = sum(gate_counts.values())
    ranked = [
        {
            "gate": gate,
            "count": count,
            "pct": round(100.0 * count / total_waits, 1) if total_waits else 0.0,
            "sample": detail_samples.get(gate, ""),
        }
        for gate, count in gate_counts.most_common()
    ]

    return {
        "window_days": days,
        "cutoff_date": cutoff.isoformat(),
        "log_path": str(log_path),
        "lines_scanned": lines_scanned,
        "wait_lines": wait_lines,
        "total_waits": total_waits,
        "ranked_blockers": ranked,
        "spread_atr_circuit_by_epic": dict(circuit_by_epic.most_common()),
        "rr_floor_rejects": rr_floor_rejects,
    }


def format_report(payload: dict[str, object]) -> str:
    lines = [
        "IG Agent — Gate Attribution Report",
        "=" * 40,
        f"Window: last {payload.get('window_days')} day(s) (from {payload.get('cutoff_date')})",
        f"Log: {payload.get('log_path')}",
        f"WAIT lines: {payload.get('wait_lines')} / lines scanned: {payload.get('lines_scanned')}",
        "",
        "Ranked blockers (WAIT — gate: reason):",
    ]
    ranked = payload.get("ranked_blockers") or []
    if not ranked:
        lines.append("  (no WAIT gate lines in window)")
    else:
        for row in ranked:
            lines.append(
                f"  {row['gate']:22} {row['count']:6}  ({row['pct']:5.1f}%)  {row['sample']}"
            )
    circuit = payload.get("spread_atr_circuit_by_epic") or {}
    if circuit:
        lines.extend(["", "Spread/ATR circuit breaker (by epic):"])
        for epic, count in circuit.items():
            lines.append(f"  {epic:28} {count:6}")
    rr = int(payload.get("rr_floor_rejects") or 0)
    if rr:
        lines.extend(["", f"RR floor execution rejects: {rr}"])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log",
        type=Path,
        default=DEFAULT_LOG,
        help=f"engine.log path (default: {DEFAULT_LOG})",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help="Rolling window in calendar days (default: 1 = today only)",
    )
    parser.add_argument(
        "--rotated",
        action="store_true",
        help="Include rotated engine.log.* files",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable report",
    )
    args = parser.parse_args()

    payload = rollup_gate_blocks(
        log_path=args.log,
        days=max(1, args.days),
        include_rotated=args.rotated,
    )

    if args.json:
        import json

        print(json.dumps(payload, indent=2))
    else:
        print(format_report(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
