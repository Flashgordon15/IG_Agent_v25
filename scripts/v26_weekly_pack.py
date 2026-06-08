#!/usr/bin/env python3
"""Generate weekly v26 progress pack (markdown)."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "v26"))

OUT_DIR = ROOT / "docs" / "weekly"


def _utc_days_back(n: int) -> list[str]:
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]


def main() -> int:
    parser = argparse.ArgumentParser(description="v26 weekly pack")
    parser.add_argument("--days", type=int, default=7, help="Rolling UTC days")
    args = parser.parse_args()

    day_list = _utc_days_back(args.days)
    label = day_list[0]

    # Refresh snapshots
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "shadow_compare.py"),
            "--process",
            "--expectancy",
            "--days",
            str(args.days),
        ],
        cwd=str(ROOT),
        env={**dict(__import__("os").environ), "PYTHONPATH": "src:v26"},
        check=False,
    )

    from ingest.lake_reader import summarize_day
    from research.gate_blockers import build_gate_blocker_report, report_to_dict
    from research.l1_replay import replay_days
    from research.shadow_expectancy import analyze_near_miss, near_miss_to_dict

    lines = [
        f"# v26 Weekly Pack — {label}",
        "",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"Window: last {args.days} UTC days",
        "",
        "## Feeder summary (per day)",
        "",
        "| Day | Events | Evals | Would fire | Fills |",
        "|-----|--------|-------|------------|-------|",
    ]

    total_fills = 0
    for d in reversed(day_list):
        s = summarize_day(d)
        total_fills += s.fill_closes
        lines.append(
            f"| {s.day} | {s.total_events} | {s.signal_evals} | {s.would_fire} | {s.fill_closes} |"
        )

    replay = replay_days(day_list)
    lines.extend(
        [
            "",
            "## L1 replay (counterfactual fires)",
            "",
            f"- Total evals: {replay.get('total_evals', 0)}",
            f"- Would fire at ≥75%: {replay.get('total_would_fire_at_75', 0)}",
            "",
        ]
    )

    # Today / latest day gate blockers
    latest = day_list[0]
    gates = report_to_dict(build_gate_blocker_report(day=latest))
    nm = analyze_near_miss(day=latest)
    nm_d = near_miss_to_dict(nm)

    lines.extend(
        [
            f"## Latest day gate analysis ({latest})",
            "",
            "### Confidence buckets",
            "",
        ]
    )
    for bucket, n in (gates.get("confidence_buckets") or {}).items():
        lines.append(f"- {bucket}: {n}")
    lines.extend(
        [
            "",
            "### Top blockers",
            "",
        ]
    )
    for gate, n in list((gates.get("failed_gates") or {}).items())[:6]:
        lines.append(f"- {gate}: {n}")
    lines.extend(
        [
            "",
            "### Near-miss 70–79% (counterfactual)",
            "",
            f"- Evals in band: {nm_d.get('near_miss_evals', 0)}",
            f"- Shadow would_trade (±5m): {nm_d.get('shadow_would_trade_same_epic', 0)}",
            f"- Est. counterfactual E£ (setup proxy): {nm_d.get('estimated_counterfactual_e_gbp', 0)}",
            "",
        ]
    )

    state = ROOT / "data_lake" / "state"
    exp_path = state / "expectancy_snapshot.json"
    if exp_path.is_file():
        exp = json.loads(exp_path.read_text(encoding="utf-8"))
        pf = exp.get("portfolio") or {}
        lines.extend(
            [
                "## Rolling expectancy",
                "",
                f"- Trades: {pf.get('n', 0)} | WR: {float(pf.get('wr', 0)):.1%} | "
                f"E£: {pf.get('e_gbp', 0)} | Total: £{pf.get('total_pnl_gbp', 0)}",
                "",
            ]
        )

    sh_path = state / "shadow_strategy_pnl.json"
    if sh_path.is_file():
        sh = json.loads(sh_path.read_text(encoding="utf-8"))
        lines.append("## Shadow strategy attribution")
        lines.append("")
        for sid, row in (sh.get("by_strategy") or {}).items():
            lines.append(
                f"- **{sid}**: n={row.get('n')} WR={float(row.get('wr', 0)):.0%} "
                f"E£={row.get('e_gbp')} total=£{row.get('total_pnl_gbp')}"
            )
        lines.append("")

    lines.extend(
        [
            "## Operator notes",
            "",
            "- v25 live trades this window: see fills column above",
            "- On low-confidence days, prioritize `v26_progress.py` over fill P&L",
            "- Enable `ml_veto` only after replay + shadow agree within 5% WR",
            "",
        ]
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{label}_v26_pack.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
