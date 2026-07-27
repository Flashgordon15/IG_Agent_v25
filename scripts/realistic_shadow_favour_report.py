#!/usr/bin/env python3
"""Synthesize favour report from realistic shadow E2E layers."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _md(report: dict[str, Any]) -> str:
    day = report.get("day")
    cycle = report.get("shadow_cycle") or {}
    loss = report.get("shadow_loss_loop") or {}
    ml = report.get("ml_dual") or {}
    lines = [
        f"# Realistic Shadow E2E Favour — {day}",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Twins: **untouched** (paused / FLAT required)",
        f"- Log: `{report.get('log')}`",
        "",
        "## Layer exits",
        "",
        f"| Layer | RC |",
        f"|---|---:|",
        f"| L1 platform SIM E2E | {report.get('e2e_rc')} |",
        f"| L2 ml_replay_learn | {report.get('ml_rc')} |",
        f"| L3 ShadowExecutor cycle | {report.get('cycle_rc')} |",
        f"| L4 shadow loss loop | {report.get('loss_rc')} |",
        "",
        "## Favour verdict",
        "",
        f"**{report.get('favour_verdict')}**",
        "",
    ]
    for n in report.get("favour_notes") or []:
        lines.append(f"- {n}")
    lines += [
        "",
        "## L3 — ShadowExecutor cycle (decision → simulated fill → forward label)",
        "",
        f"- Candidates: {cycle.get('candidates')} · Taken: {cycle.get('taken')} · Vetoed: {cycle.get('vetoed')}",
        f"- Cycle favour: `{cycle.get('favour')}`",
        "",
    ]
    by_style = cycle.get("by_style") or {}
    if by_style:
        lines += [
            "| Style | Taken | Wins | Losses | WR | PnL £ |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for style, row in by_style.items():
            lines.append(
                f"| {style} | {row.get('taken', 0)} | {row.get('wins', 0)} | "
                f"{row.get('losses', 0)} | {row.get('wr')} | {row.get('pnl_gbp')} |"
            )
        lines.append("")
    by_epic = cycle.get("by_epic") or {}
    if by_epic:
        lines += [
            "| Epic | Taken | WR | PnL £ |",
            "|---|---:|---:|---:|",
        ]
        for epic, row in sorted(by_epic.items(), key=lambda kv: -(kv[1].get("taken") or 0)):
            lines.append(
                f"| `{epic}` | {row.get('taken', 0)} | {row.get('wr')} | {row.get('pnl_gbp')} |"
            )
        lines.append("")

    mix = (loss.get("class_mix") or loss.get("summary") or {})
    cf = loss.get("counterfactual") or loss.get("shadow_counterfactual") or {}
    lines += [
        "## L4 — Shadow loss loop (LOGIC-only counterfactual on live losers)",
        "",
        f"- APP / LOGIC / UNKNOWN: `{mix or loss.get('counts') or 'see report'}`",
        f"- Would-veto: `{cf.get('would_veto') or loss.get('would_veto')}`",
        f"- Avoided £ if vetoed: `{cf.get('pnl_if_skipped_gbp') or loss.get('pnl_if_skipped_gbp')}`",
        f"- Next step: `{loss.get('next_one_step') or (loss.get('recommended') or {}).get('action')}`",
        "",
        "## What this favours for Monday",
        "",
    ]
    for item in report.get("monday_implications") or []:
        lines.append(f"1. {item}")
    lines += [
        "",
        "## Improve next (ordered)",
        "",
    ]
    for item in report.get("improve_next") or []:
        lines.append(f"1. {item}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", required=True)
    ap.add_argument("--e2e-rc", type=int, default=-1)
    ap.add_argument("--ml-rc", type=int, default=-1)
    ap.add_argument("--cycle-rc", type=int, default=-1)
    ap.add_argument("--loss-rc", type=int, default=-1)
    ap.add_argument("--shadow-root", type=Path, required=True)
    ap.add_argument("--log", type=Path, default=None)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    from system.paths import data_dir

    cycle = _read_json(args.shadow_root / "reports" / f"shadow_cycle_{args.day}.json")
    loss = _read_json(data_dir() / "reports" / f"shadow_loss_loop_{args.day}.json")
    # Prefer richer loss report fields
    if not loss:
        loss = _read_json(
            ROOT / "src" / "data" / "v31-production" / "reports" / f"shadow_loss_loop_{args.day}.json"
        )

    favour_notes: list[str] = list(cycle.get("favour_notes") or [])
    favour = str(cycle.get("favour") or "UNKNOWN")

    # Blend with loss-loop: if APP dominates, APP lane still first
    mix = loss.get("class_mix") or {}
    app_n = int((mix.get("APP") or {}).get("count") or mix.get("APP") or 0)
    logic_n = int((mix.get("LOGIC") or {}).get("count") or mix.get("LOGIC") or 0)
    if app_n and app_n >= max(1, logic_n):
        favour_notes.append(
            f"Live bleed day {args.day}: APP losses dominate ({app_n} vs LOGIC {logic_n}) — APP fixes still primary"
        )
        if favour.startswith("FAVOURS"):
            favour = f"APP_FIRST_THEN_{favour}"
        else:
            favour = "APP_FIRST"

    cf = loss.get("shadow_counterfactual") or loss.get("counterfactual") or loss
    would_veto = (
        cf.get("would_veto_count")
        or cf.get("would_veto")
        or loss.get("would_veto")
    )
    avoided = cf.get("would_veto_pnl_gbp")
    if avoided is None:
        avoided = cf.get("pnl_if_skipped_gbp") or loss.get("pnl_if_skipped_gbp")
    # Avoided loss is negative PnL skipped — report as positive £ saved
    avoided_saved = None
    if avoided is not None:
        try:
            avoided_saved = round(abs(float(avoided)), 2)
        except (TypeError, ValueError):
            avoided_saved = avoided
    if would_veto:
        favour_notes.append(
            f"LOGIC counterfactual would-veto={would_veto} "
            f"avoided≈£{avoided_saved} — ML floor has teeth on LOGIC slice"
        )

    monday = [
        "Arm via monday_dual_arm.sh only when FLAT; Instant/micro stay OFF on SB.",
        "Trust first 8 stamped closes before any strategy claim (HoldSec ≥40%).",
        "If APP share of losers stays ≥25%, pause entries and fix APP — do not retrain for edge.",
    ]
    if "SNIPER" in favour or "FAVOURS_BOTH" in favour:
        monday.append("Shadow cycle favours sniper — keep CFD QUANT_SNIPER selective (cap 1).")
    if "LONG" in favour or "FAVOURS_BOTH" in favour:
        monday.append("Shadow cycle favours long horizon — SB macro/LTR is the learning lane.")
    if favour in ("NO_TAKES", "NO_EDGE", "EDGE_WEAK") or favour.endswith(
        ("NO_TAKES", "NO_EDGE", "EDGE_WEAK")
    ):
        monday.append("Shadow did not show clean favour — trade smaller / keep A2-style selectivity if bleed recurs.")
    if favour.startswith("APP_FIRST"):
        monday.append("Do not treat offline favour as permission to ignore APP bleed — stamp/path first.")

    improve = [
        "Keep daily autopsy+review+shadow at 21:40 (LaunchAgent already loaded).",
        "Stamp MarketRegime / HoldSec / MlScore on every live close — measurement before ML claims.",
        "Feed only LOGIC losers into veto-regret / retrain; never APP.",
    ]
    if would_veto:
        improve.insert(
            0,
            "Promote profit_policy / ml_floor discipline that matched would-veto on LOGIC losers.",
        )
    if app_n >= logic_n and app_n:
        improve.insert(0, "Clear remaining APP class on live path before expecting strategy WR lift.")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "day": args.day,
        "e2e_rc": args.e2e_rc,
        "ml_rc": args.ml_rc,
        "cycle_rc": args.cycle_rc,
        "loss_rc": args.loss_rc,
        "log": str(args.log) if args.log else None,
        "shadow_cycle": cycle,
        "shadow_loss_loop": {
            "class_mix": mix,
            "counterfactual": {
                "would_veto": would_veto,
                "pnl_if_skipped_gbp": avoided,
                "avoided_saved_gbp": avoided_saved,
            },
            "next_one_step": loss.get("next_one_step")
            or (loss.get("recommended") or {}).get("action"),
        },
        "favour_verdict": favour,
        "favour_notes": favour_notes,
        "monday_implications": monday,
        "improve_next": improve,
    }

    md = _md(report)
    print(md)
    if args.write:
        out_dir = data_dir() / "reports"
        # Prefer production reports so operator finds it with other weekend docs
        prod = ROOT / "src" / "data" / "v31-production" / "reports"
        if prod.is_dir():
            out_dir = prod
        out_dir.mkdir(parents=True, exist_ok=True)
        md_path = out_dir / f"realistic_shadow_e2e_{args.day}.md"
        json_path = out_dir / f"realistic_shadow_e2e_{args.day}.json"
        md_path.write_text(md, encoding="utf-8")
        json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {md_path}", file=sys.stderr)
        print(f"wrote {json_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
