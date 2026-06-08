#!/usr/bin/env python3
"""OHLC historical learning — signal replay + S2/S3 bar lab + walk-forward."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "v26"))


def main() -> int:
    parser = argparse.ArgumentParser(description="v26 OHLC historical learning")
    parser.add_argument(
        "--skip-signal-replay",
        action="store_true",
        help="Skip replay_signals.py (use existing replay_results.jsonl)",
    )
    parser.add_argument(
        "--max-bars",
        type=int,
        default=0,
        help="Cap bars per market for bar lab (0=all)",
    )
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    py = sys.executable

    if not args.skip_signal_replay:
        print("Running OHLC signal replay (all enabled markets, batch)…")
        rc = subprocess.run(
            [py, str(ROOT / "scripts" / "replay_signals.py"), "--all", "--batch"],
            cwd=str(ROOT),
            env={**dict(__import__("os").environ), "PYTHONPATH": str(ROOT / "src")},
        ).returncode
        if rc != 0:
            print(f"replay_signals exited {rc} (continuing with bar lab)")

    from research.historical_bar_lab import run_historical_bar_lab
    from research.walk_forward import load_replay_rows, ml_veto_hints, threshold_sweep

    bar_lab = run_historical_bar_lab(max_bars_per_market=args.max_bars)
    sweep = threshold_sweep(load_replay_rows())
    hints = ml_veto_hints(sweep)

    print("\n=== Historical bar lab (S2/S3) ===")
    print(f"Markets: {bar_lab.get('markets')} | bars: {bar_lab.get('total_bars')}")
    for sid, row in (bar_lab.get("by_strategy") or {}).items():
        print(
            f"  {sid}: would_trade={row.get('would_trade')} intents={row.get('intents')}"
        )
    for m in bar_lab.get("markets_detail") or []:
        if m.get("s2_would_trade") or m.get("s3_would_trade"):
            print(
                f"  {m.get('market')}: bars={m.get('bars')} "
                f"S2={m.get('s2_would_trade')} S3={m.get('s3_would_trade')}"
            )

    print("\n=== Walk-forward threshold sweep ===")
    print(f"Replay rows: {sweep.get('total_rows')}")
    for epic, data in (sweep.get("by_epic") or {}).items():
        rec = data.get("recommended_threshold")
        wr = data.get("best_wr")
        if wr is not None:
            print(f"  {epic}: best ≥{rec}% WR={wr:.1%} (rows={data.get('total_rows')})")

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bar_lab": bar_lab,
        "walk_forward": sweep,
        "ml_veto_hints": hints,
    }

    if args.write:
        out = ROOT / "data_lake" / "state" / "v26_ohlc_replay.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nWrote {out}")

    return 0 if bar_lab.get("ok") or sweep.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
