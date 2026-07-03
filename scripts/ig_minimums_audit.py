#!/usr/bin/env python3
"""Live IG minimum-deal audit — tradeability, sizing, time-of-day session state."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

BST = ZoneInfo("Europe/London")


def _now_bst() -> str:
    return datetime.now(BST).strftime("%Y-%m-%d %H:%M:%S %Z")


def main() -> int:
    os.environ.setdefault("IG_AGENT_CONFIG", "config/config_v31_demo_throughput.json")

    from execution.ig_minimums_matrix import evaluate_epic_minimums
    from system.config_loader import load_active_config
    from system.credentials_loader import try_load_credentials
    from system.ig_rest_session import get_shared_rest_client
    from system.market_data_hub import NIGHT_MATRIX_EPICS

    cfg = load_active_config(validate=False)
    cred = try_load_credentials()
    if not cred.ok or not cred.credentials:
        print("ERROR: IG credentials unavailable", file=sys.stderr)
        return 1

    rest = get_shared_rest_client(cred.credentials)
    rest.ensure_session()

    from execution.broker_epic_resolver import resolve_account_product

    product = resolve_account_product(rest=rest, cfg=cfg)
    print(f"IG Minimums Audit @ {_now_bst()}")
    print(f"Account product: {product}")
    print(f"Config: {os.environ.get('IG_AGENT_CONFIG', 'default')}")
    print("-" * 100)

    rows: list[dict] = []
    for epic in NIGHT_MATRIX_EPICS:
        verdict = evaluate_epic_minimums(epic, cfg=cfg, rest_client=rest)
        row = {
            "epic": verdict.epic,
            "wire_epic": verdict.wire_epic,
            "market_status": verdict.market_status,
            "session_note": verdict.session_note,
            "ig_min": verdict.ig_min_deal,
            "hard_min": verdict.hard_min_deal,
            "effective_min": verdict.effective_min_deal,
            "canary_lot": verdict.canary_lot,
            "guard_size": verdict.guard_size,
            "transmit_ok": verdict.transmit_allowed,
            "transmit_reason": verdict.transmit_reason,
            "trade_possible": verdict.trade_possible,
            "block_reason": verdict.block_reason,
        }
        rows.append(row)
        flag = "PASS" if verdict.trade_possible else "BLOCK"
        print(
            f"[{flag}] {verdict.epic}\n"
            f"  wire={verdict.wire_epic} status={verdict.market_status or 'N/A'}\n"
            f"  mins: ig={verdict.ig_min_deal} hard={verdict.hard_min_deal} "
            f"effective={verdict.effective_min_deal} canary={verdict.canary_lot} "
            f"guard_size={verdict.guard_size}\n"
            f"  session: {verdict.session_note}\n"
            f"  transmit: {verdict.transmit_allowed} ({verdict.transmit_reason or 'ok'})\n"
            f"  block: {verdict.block_reason or 'none'}\n"
        )

    possible = [r for r in rows if r["trade_possible"]]
    blocked = [r for r in rows if not r["trade_possible"]]

    print("=" * 100)
    print(f"SUMMARY: {len(possible)} tradeable / {len(blocked)} blocked / {len(rows)} total")
    if possible:
        print("TRADEABLE NOW:")
        for r in possible:
            print(
                f"  - {r['epic']} size>={r['effective_min']} "
                f"(guard normalizes to {r['guard_size']})"
            )
    if blocked:
        print("BLOCKED (time-of-day / access / size):")
        for r in blocked:
            print(f"  - {r['epic']}: {r['block_reason']} [{r['session_note']}]")

    out_path = ROOT / "src" / "data" / "ig_minimums_audit_latest.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "audited_at_bst": _now_bst(),
        "account_product": product,
        "rows": rows,
        "tradeable_count": len(possible),
        "blocked_count": len(blocked),
    }
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"\nWrote {out_path}")

    # Mismatch check: hard mins should be >= IG when IG reports (or match when IG higher)
    mismatches = []
    for r in rows:
        if r["ig_min"] > 0 and r["hard_min"] < r["ig_min"]:
            mismatches.append(
                f"{r['epic']}: hard_min {r['hard_min']} < ig_min {r['ig_min']}"
            )
    if mismatches:
        print("\nWARN: hard floor below live IG minimum (update size_floors.py):")
        for m in mismatches:
            print(f"  {m}")
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
