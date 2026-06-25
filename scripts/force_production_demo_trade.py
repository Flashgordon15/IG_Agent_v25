#!/usr/bin/env python3
"""
Force a v31 production-plane DEMO breakout order through the local API intake.

  PYTHONPATH=src .venv/bin/python3 scripts/force_production_demo_trade.py

Requires the agent API on :8080 (or IG_API_PORT) and IG DEMO credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.runtime_context import RuntimeContext  # noqa: E402


def _unique_deal_reference() -> str:
    seed = f"{secrets.token_hex(16)}-{os.getpid()}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _post_fulfill(api_base: str, payload: dict, *, timeout_sec: float = 45.0) -> dict:
    url = f"{api_base.rstrip('/')}/api/v31/orders/fulfill"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body)
        except Exception:
            detail = {"error": body}
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="v31 production DEMO trade injector")
    parser.add_argument("--size", type=float, default=0.5, help="Micro-lot size")
    parser.add_argument("--epic", default="", help="Override epic (auto-select when empty)")
    parser.add_argument("--direction", default="BUY", choices=("BUY", "SELL"))
    parser.add_argument(
        "--api-base",
        default="",
        help="API base URL (default from RuntimeContext / IG_API_PORT)",
    )
    args = parser.parse_args()

    ctx = RuntimeContext(api_base=args.api_base or "http://127.0.0.1:8080").initialize()
    health = ctx.connect_api()
    print(f"API health: {json.dumps(health, default=str)}")

    epic = str(args.epic or "").strip()
    if not epic:
        # Index CFDs accept 0.5 on IG DEMO; FX pairs often require ≥1.0.
        prefer = (
            "IX.D.DOW.IFM.IP",
            "CS.D.CFPGOLD.CFP.IP",
            "IX.D.NIKKEI.IFM.IP",
            "CS.D.EURUSD.CFD.IP",
        )
        epic = ctx.select_open_epic(prefer)
    print(f"Selected epic: {epic}")

    deal_ref = _unique_deal_reference()
    payload = {
        "signal": "BREAKOUT",
        "epic": epic,
        "direction": args.direction,
        "size": float(args.size),
        "dealReference": deal_ref,
        "order_type": "MARKET",
        "setup_key": "E2E|FORCE|V31",
        "confidence": 88.0,
    }
    print(f"Injecting synthetic breakout dealReference={deal_ref} ...")

    receipt = _post_fulfill(ctx.api_base, payload)
    print("\n=== BROKER RECEIPT ===")
    print(json.dumps(receipt, indent=2, default=str))

    status = str(receipt.get("status") or "").upper()
    deal_id = receipt.get("dealId") or receipt.get("deal_id")
    ok = status in ("EXECUTED", "CONFIRMED") and bool(deal_id or receipt.get("ok"))
    print(
        f"\nReceipt matrix: status={status} dealId={deal_id or '—'} "
        f"dealReference={receipt.get('dealReference', deal_ref)}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
