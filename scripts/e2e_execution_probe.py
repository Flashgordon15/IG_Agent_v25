#!/usr/bin/env python3
"""
E2E execution probe — DEMO routing validation and optional micro-order test.

Safe default (no order placed):
  PYTHONPATH=src python3 scripts/e2e_execution_probe.py

Mock pipeline only (pytest):
  PYTHONPATH=src python3 scripts/e2e_execution_probe.py --mock-only

Optional DEMO micro-order (places and closes a small position — use with care):
  PYTHONPATH=src python3 scripts/e2e_execution_probe.py --place-demo-order --confirm I_UNDERSTAND
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.e2e_execution_check import run_demo_routing_check, run_e2e_execution_check, run_mock_pipeline_tests


def _run_mock_tests() -> int:
    print("=== Mock pipeline (7 gates -> process_tick -> execute_trade) ===")
    result = run_mock_pipeline_tests()
    print(result.get("summary") or result.get("error", ""))
    return 0 if result.get("ok") else 1


def _run_demo_routing() -> int:
    print("=== IG DEMO routing validation (dry-run, no order) ===")
    result = run_demo_routing_check()
    if not result.get("ok"):
        print(f"FAIL: {result.get('error', result)}")
        return 1
    print(
        f"PASS: DEMO routing OK — base={result.get('base_url')} "
        f"account={result.get('account_id')}"
    )
    print(
        f"      market {result.get('epic')} bid/offer={result.get('bid')}/"
        f"{result.get('offer')} balance={result.get('balance')}"
    )
    return 0


def _place_and_close_demo_order(*, epic: str, size: float, stop: float) -> int:
    from system.config_loader import ConfigLoader
    from system.credentials_loader import try_load_credentials
    from system.ig_rest_session import ensure_shared_authenticated

    status = try_load_credentials()
    if not status.ok or status.credentials is None:
        print(f"FAIL: credentials — {status.error}")
        return 1

    cfg = ConfigLoader(ROOT / "config" / "config_v25.json").load_config()
    rest = ensure_shared_authenticated(status.credentials)
    if rest.account_type != "DEMO":
        print(f"FAIL: account type is {rest.account_type}, not DEMO")
        return 1

    before = len(rest.open_positions())
    print(f"Open positions before: {before}")
    print(f"Placing DEMO MARKET BUY size={size} epic={epic} stop_distance={stop} ...")

    try:
        resp = rest.place_market_order(
            epic=epic,
            direction="BUY",
            size=size,
            stop_distance=stop,
            limit_distance=float(cfg.limit_distance_points),
            currency_code=cfg.currency_code,
        )
    except Exception as e:
        print(f"FAIL: order placement — {type(e).__name__}: {e}")
        return 1

    deal_ref = str(resp.get("dealReference") or "")
    print(f"PASS: order accepted — dealReference={deal_ref}")
    time.sleep(2.0)

    positions = rest.open_positions()
    print(f"Open positions after order: {len(positions)}")
    closed = 0
    for item in positions:
        pos = item.get("position") or {}
        market = item.get("market") or {}
        deal_id = str(pos.get("dealId") or "")
        pos_epic = str(market.get("epic") or "")
        if pos_epic != epic or not deal_id:
            continue
        side = str(pos.get("direction") or "BUY").upper()
        pos_size = float(pos.get("size") or 0)
        close_dir = "SELL" if side == "BUY" else "BUY"
        print(f"Closing deal_id={deal_id} {side} size={pos_size} ...")
        rest.close_position(
            deal_id,
            direction=close_dir,
            size=pos_size,
            epic=epic,
            currency_code=cfg.currency_code,
            verify=True,
        )
        closed += 1
        time.sleep(1.0)

    print(f"Closed {closed} position(s) on {epic}")
    return 0 if closed > 0 or deal_ref else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="IG Agent v25 E2E execution probe")
    parser.add_argument(
        "--mock-only",
        action="store_true",
        help="Run mocked pytest pipeline only",
    )
    parser.add_argument(
        "--skip-mock",
        action="store_true",
        help="Skip mocked pytest (routing / live order only)",
    )
    parser.add_argument(
        "--place-demo-order",
        action="store_true",
        help="Place a small DEMO market order and close it (opt-in)",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help="Required with --place-demo-order: I_UNDERSTAND",
    )
    parser.add_argument("--size", type=float, default=0.5, help="DEMO order size")
    args = parser.parse_args()

    rc = 0
    if not args.skip_mock:
        rc = _run_mock_tests()
        if rc != 0:
            return rc

    if args.mock_only:
        return 0

    rc = _run_demo_routing()
    if rc != 0:
        return rc

    if args.place_demo_order:
        if args.confirm != "I_UNDERSTAND":
            print("Refusing live order: pass --confirm I_UNDERSTAND")
            return 1
        from system.config_loader import ConfigLoader

        cfg = ConfigLoader(ROOT / "config" / "config_v25.json").load_config()
        stop = float(cfg.stop_distance_points)
        print("WARNING: placing a real DEMO order on IG (will attempt to close after).")
        return _place_and_close_demo_order(epic=cfg.epic, size=args.size, stop=stop)

    print(
        "\nDone. Mock pipeline + DEMO routing validated. "
        "For optional live DEMO micro-order, re-run with --place-demo-order --confirm I_UNDERSTAND"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
