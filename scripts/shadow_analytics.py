#!/usr/bin/env python3
"""Print shadow vs live learning-plane analytics comparison."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.shadow_analytics import build_shadow_vs_live_comparison


def _fmt_pf(value) -> str:
    if value is None:
        return "∞"
    return f"{float(value):.2f}"


def main() -> int:
    report = build_shadow_vs_live_comparison()
    for plane_key in ("shadow", "live"):
        plane = report[plane_key]
        print(f"\n=== {plane.get('label', plane_key)} ===")
        print(f"  Trades:     {plane.get('trade_count')}")
        print(f"  Win rate:   {100 * float(plane.get('win_rate') or 0):.1f}%")
        print(f"  Profit factor: {_fmt_pf(plane.get('profit_factor'))}")
        print(f"  Net P&L:    £{float(plane.get('net_pnl_gbp') or 0):.2f}")
        print(f"  Avg DD:     £{float(plane.get('average_drawdown_gbp') or 0):.2f}")
        print(f"  Max DD:     £{float(plane.get('max_drawdown_gbp') or 0):.2f}")

    print("\n=== Comparison (live − shadow) ===")
    comp = report.get("comparison") or {}
    print(json.dumps(comp, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
