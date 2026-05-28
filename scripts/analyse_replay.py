#!/usr/bin/env python3
"""
Summarise replay_signals JSONL — threshold / RSI tuning evidence.

  PYTHONPATH=src python3 scripts/analyse_replay.py --input src/data/replay/bars.signals.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def _load_rows(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyse signal replay JSONL")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--threshold", type=float, default=None, help="Override threshold for pass stats")
    args = parser.parse_args()

    rows = _load_rows(args.input)
    if not rows:
        print("FAIL: no rows in input", file=sys.stderr)
        return 1

    threshold = args.threshold
    if threshold is None:
        threshold = float(rows[0].get("threshold") or 70)

    signals = Counter(str(r.get("signal") or "WAIT") for r in rows)
    rsi_blocks = sum(1 for r in rows if r.get("rsi_block"))
    actionable = [r for r in rows if r.get("signal") in ("BUY", "SELL")]
    at_threshold = [r for r in rows if float(r.get("adjusted_confidence") or 0) >= threshold]
    would_pass_gate = [
        r
        for r in rows
        if r.get("signal") in ("BUY", "SELL")
        and float(r.get("adjusted_confidence") or 0) >= threshold
        and not r.get("rsi_block")
    ]

    confs = [float(r.get("adjusted_confidence") or 0) for r in rows]
    avg_conf = sum(confs) / len(confs) if confs else 0.0
    max_conf = max(confs) if confs else 0.0

    print(f"Replay file: {args.input}")
    print(f"Evaluations: {len(rows)}")
    print(f"Threshold: {threshold}")
    print(f"Signal counts: {dict(signals)}")
    print(f"RSI blocks: {rsi_blocks} ({100 * rsi_blocks / len(rows):.1f}%)")
    print(f"Actionable BUY/SELL (any conf): {len(actionable)}")
    print(f"Confidence >= threshold: {len(at_threshold)}")
    print(f"Would pass gate 6 (signal + conf + no RSI block): {len(would_pass_gate)}")
    print(f"Avg adjusted confidence: {avg_conf:.1f}%  max: {max_conf:.1f}%")

    blocked_high = [
        r
        for r in rows
        if r.get("rsi_block")
        and float(r.get("adjusted_confidence") or 0) >= threshold - 5
    ]
    if blocked_high:
        print(f"\nHigh-score RSI blocks (conf >= {threshold - 5}): {len(blocked_high)}")
        for r in blocked_high[:5]:
            print(
                f"  {r.get('time')} {r.get('signal')} "
                f"conf={r.get('adjusted_confidence')} — {r.get('rsi_block')}"
            )
        if len(blocked_high) > 5:
            print(f"  ... +{len(blocked_high) - 5} more")

    near_miss = [
        r
        for r in rows
        if r.get("signal") in ("BUY", "SELL")
        and threshold - 10 <= float(r.get("adjusted_confidence") or 0) < threshold
    ]
    if near_miss:
        print(f"\nNear-miss below threshold ({threshold - 10}–{threshold}): {len(near_miss)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
