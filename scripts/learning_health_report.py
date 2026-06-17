#!/usr/bin/env python3
"""Print learning pipeline health (ML, registry, agent P&L)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.learning_health import build_learning_health_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Learning health report")
    parser.add_argument(
        "--refresh-registry",
        action="store_true",
        help="Rebuild setup_registry.json from agent-only closes before reporting",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON only")
    args = parser.parse_args()

    report = build_learning_health_report(refresh_registry=args.refresh_registry)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    pnl = report.get("agent_pnl") or {}
    ml = report.get("ml") or {}
    reg = report.get("setup_registry") or {}
    print("=== Learning Health ===")
    print(f"Generated: {report.get('generated_at')}")
    print()
    print("Agent P&L (excludes IG imports)")
    print(
        f"  Closes: {pnl.get('agent_closed_trades')}  "
        f"W/L: {pnl.get('agent_wins')}/{pnl.get('agent_losses')}  "
        f"WR: {float(pnl.get('agent_win_rate') or 0) * 100:.1f}%"
    )
    print(f"  IG import rows excluded: {pnl.get('ig_import_rows_excluded')}")
    print(f"  Shadow training registry: {pnl.get('shadow_training_registry_rows')}")
    print()
    print("ML")
    print(f"  USE_ML_SIGNAL: {ml.get('use_ml_signal')}")
    print(
        f"  Model trained: {ml.get('model_trained')}  Records: {ml.get('training_records')}/{ml.get('training_records_required')}"
    )
    print(f"  ML blend ready: {ml.get('ml_blend_ready')}")
    print(f"  Filter overrides active: {ml.get('filter_overrides_active')}")
    print()
    sa = report.get("shadow_analytics") or {}
    if sa.get("shadow"):
        sh = sa["shadow"]
        lv = sa.get("live") or {}
        print("Shadow vs live analytics")
        print(
            f"  Shadow WR/PF: {float(sh.get('win_rate') or 0) * 100:.1f}% / "
            f"{sh.get('profit_factor')}  ({sh.get('trade_count')} trades)"
        )
        print(
            f"  Live WR/PF:   {float(lv.get('win_rate') or 0) * 100:.1f}% / "
            f"{lv.get('profit_factor')}  ({lv.get('trade_count')} trades)"
        )
    ms = report.get("milestones") or {}
    if ms:
        print()
        print("ML milestones")
        print(
            f"  Records: {ms.get('training_records')}/{ms.get('training_records_required')}  "
            f"Next: {ms.get('next_milestone')}  Sent: {ms.get('milestones_sent')}"
        )
    print()
    print("Setup registry")
    print(f"  Gate enabled: {reg.get('enabled')}  Banned: {reg.get('banned_count')}")
    if reg.get("banned_keys"):
        for key in reg["banned_keys"][:8]:
            print(f"    - {key}")
    print()
    print("Recommendations")
    for line in report.get("recommendations") or []:
        print(f"  • {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
