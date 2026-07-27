#!/usr/bin/env python3
"""Shadow loss loop CLI — APP/LOGIC split + LOGIC-only ML counterfactual.

Examples::

  PYTHONPATH=src IG_AGENT_CONFIG=config/config_v31_demo_throughput.json \\
    .venv/bin/python3 scripts/shadow_loss_loop.py --day 2026-07-24

  ./scripts/run_daily_loss_autopsy.sh 2026-07-24 --with-review
  PYTHONPATH=src .venv/bin/python3 scripts/shadow_loss_loop.py --day 2026-07-24

Never places orders. Never lifts A2. Never claims improvement epoch.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from diagnostics.shadow_loss_loop import write_shadow_loss_loop  # noqa: E402
from diagnostics.trade_lifecycle_witness import default_data_root  # noqa: E402


def _today_london() -> str:
    return datetime.now(ZoneInfo("Europe/London")).strftime("%Y-%m-%d")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Shadow loss loop (APP/LOGIC split + LOGIC ML counterfactual)"
    )
    ap.add_argument(
        "--day",
        default=None,
        help="Calendar day YYYY-MM-DD (default: London today)",
    )
    ap.add_argument("--data-root", type=Path, default=None)
    ap.add_argument(
        "--no-rebuild-autopsy",
        action="store_true",
        help="Do not rebuild loss_autopsy if losers list missing",
    )
    args = ap.parse_args()

    day = args.day or _today_london()
    root = args.data_root or default_data_root()
    md_path, json_path, report = write_shadow_loss_loop(
        day=day,
        data_root=root,
        rebuild_autopsy=not args.no_rebuild_autopsy,
    )
    mix = report.get("class_mix") or {}
    shadow = report.get("shadow_counterfactual") or {}
    nxt = report.get("next_one_step") or {}
    print(f"Wrote {md_path}")
    print(f"Wrote {json_path}")
    print(
        "class_mix "
        f"APP={mix.get('APP', {}).get('count', 0)} "
        f"LOGIC={mix.get('LOGIC', {}).get('count', 0)} "
        f"UNKNOWN={mix.get('UNKNOWN', {}).get('count', 0)}"
    )
    print(
        f"shadow LOGIC scored={shadow.get('scored_count')} "
        f"would_veto={shadow.get('would_veto_count')} "
        f"rate={shadow.get('would_veto_rate')}"
    )
    print(f"next_one_step lane={nxt.get('lane')} action={nxt.get('action')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
