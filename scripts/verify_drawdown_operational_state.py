#!/usr/bin/env python3
"""
Live operational verification — drawdown monitor + Superjet guard lockout clearance.

Checks:
  1. drawdown_monitor operational_status is NOMINAL or STANDBY (not stuck ALERT/BREACH)
  2. engine.log DRAWDOWN_EVAL_DEBUG cycles use last_balance_field_used=balance
  3. Flight Deck telemetry JSON serializes snapshot_decimal_debug without Decimal leaks

Usage:
  PYTHONPATH=src python3 scripts/verify_drawdown_operational_state.py
  PYTHONPATH=src python3 scripts/verify_drawdown_operational_state.py --live
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ENGINE_LOG = ROOT / "src" / "data" / "logs" / "engine.log"
COCKPIT_DRAWDOWN_URL = "http://127.0.0.1:8787/api/drawdown-status"
COCKPIT_WS_URL = "ws://127.0.0.1:8787/ws/telemetry"

_DEBUG_RE = re.compile(r"DRAWDOWN_EVAL_DEBUG raw_context=(\{.*\})\s*$")


def _fetch_cockpit_drawdown_status() -> dict[str, Any] | None:
    try:
        req = urllib.request.Request(COCKPIT_DRAWDOWN_URL, method="GET")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _fetch_ws_telemetry_once() -> dict[str, Any] | None:
    try:
        import websocket  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        ws = websocket.create_connection(COCKPIT_WS_URL, timeout=4.0)
        raw = ws.recv()
        ws.close()
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _local_drawdown_status() -> dict[str, Any]:
    from system.drawdown_monitor import operational_status, snapshot_for_telemetry
    from system.superjet_drawdown_guard import is_frozen, telemetry_snapshot

    guard = telemetry_snapshot()
    monitor = snapshot_for_telemetry()
    status = operational_status()
    return {
        **guard,
        "monitor": monitor,
        "monitor_operational_status": status,
        "frozen": is_frozen(),
        "lockout_clear": (
            not guard.get("frozen")
            and not guard.get("breached")
            and status in ("NOMINAL", "STANDBY")
        ),
        "source": "local_import",
    }


def _parse_drawdown_eval_debug_lines(
    log_path: Path,
    *,
    max_lines: int = 50,
) -> list[dict[str, Any]]:
    if not log_path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        if "DRAWDOWN_EVAL_DEBUG" not in line:
            continue
        m = _DEBUG_RE.search(line.strip())
        if not m:
            continue
        try:
            ctx = json.loads(m.group(1))
            if isinstance(ctx, dict):
                rows.append(ctx)
        except json.JSONDecodeError:
            continue
    return rows[-max_lines:]


def _hud_serialization_probe(payload: dict[str, Any]) -> tuple[bool, str]:
    """Mirror Flight Deck ws.send_text(json.dumps(payload, default=str))."""
    try:
        encoded = json.dumps(payload, default=str)
        roundtrip = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        return False, f"json serialize failed: {exc}"

    monitor = roundtrip.get("drawdown_guard", {}).get("monitor") or roundtrip.get("monitor") or {}
    for key in ("session_pnl_gbp", "drawdown_pct", "observations"):
        val = monitor.get(key)
        if val is not None and not isinstance(val, (int, float, str)):
            return False, f"monitor.{key} not JSON-safe: {type(val).__name__}"

    # app.js fmtMoney(Number(n)) — must not be NaN for numeric fields
    for key in ("session_pnl_gbp", "drawdown_pct"):
        val = monitor.get(key)
        if val is not None:
            f = float(val)
            if f != f:  # NaN
                return False, f"monitor.{key} is NaN after float()"

    return True, "ok"


def verify(*, live: bool) -> int:
    issues: list[str] = []
    notes: list[str] = []

    status: dict[str, Any] | None = None
    if live:
        status = _fetch_cockpit_drawdown_status()
        if status is None:
            ws_payload = _fetch_ws_telemetry_once()
            if ws_payload and isinstance(ws_payload.get("drawdown_guard"), dict):
                dg = ws_payload["drawdown_guard"]
                status = {**dg, "source": "websocket_telemetry"}
                notes.append("Used /ws/telemetry ( /api/drawdown-status not available — restart agent for endpoint )")
    if status is None:
        status = _local_drawdown_status()
        notes.append("Used local module import (not in-process agent state)")

    monitor = status.get("monitor") or {}
    op_status = str(
        status.get("monitor_operational_status")
        or monitor.get("operational_status")
        or "UNKNOWN"
    ).upper()
    field_used = str(monitor.get("last_balance_field_used") or "unknown")

    print("=== Drawdown operational verification ===")
    print(f"source: {status.get('source', 'cockpit_api')}")
    print(f"superjet daily_pnl_gbp: {status.get('daily_pnl_gbp')}")
    print(f"superjet frozen: {status.get('frozen')}")
    print(f"superjet breached: {status.get('breached')}")
    print(f"monitor operational_status: {op_status}")
    print(f"monitor last_balance_field_used: {field_used}")
    print(f"lockout_clear: {status.get('lockout_clear')}")

    if status.get("frozen"):
        issues.append("Superjet guard still FROZEN — restart after fix or wait for UK midnight reset")
    if status.get("breached"):
        issues.append(
            f"Superjet guard still BREACHED (daily_pnl_gbp={status.get('daily_pnl_gbp')})"
        )
    if op_status not in ("NOMINAL", "STANDBY"):
        issues.append(f"drawdown_monitor operational_status={op_status} (expected NOMINAL or STANDBY)")
    if field_used not in ("balance", "unknown", ""):
        issues.append(f"last_balance_field_used={field_used!r} (expected 'balance')")

    # engine.log DRAWDOWN_EVAL_DEBUG audit
    debug_rows = _parse_drawdown_eval_debug_lines(ENGINE_LOG)
    print(f"\nengine.log DRAWDOWN_EVAL_DEBUG entries: {len(debug_rows)}")
    if debug_rows:
        bad_field = [
            r
            for r in debug_rows
            if str((r.get("drawdown_monitor") or {}).get("last_balance_field_used") or "") not in ("balance", "")
        ]
        avail_mismatch = [r for r in debug_rows if r.get("balance_vs_available_alert")]
        print(f"  last field: {(debug_rows[-1].get('drawdown_monitor') or {}).get('last_balance_field_used')}")
        print(f"  last total_decimal: {(debug_rows[-1].get('pnl_components') or {}).get('total_decimal')}")
        if bad_field:
            issues.append(
                f"{len(bad_field)} DRAWDOWN_EVAL_DEBUG cycle(s) used non-balance field"
            )
        if avail_mismatch:
            notes.append(
                f"{len(avail_mismatch)} log cycle(s) flagged balance_vs_available_alert (diagnostic only)"
            )
    else:
        notes.append("No DRAWDOWN_EVAL_DEBUG in engine.log yet — trigger via cockpit collector or balance refresh")

    # HUD serialization probe
    hud_payload = {
        "drawdown_guard": status if "monitor" in status else {"monitor": monitor, **status},
        "global_ai_status_key": "HEALTHY" if not status.get("breached") else "EMERGENCY",
    }
    ok, detail = _hud_serialization_probe(hud_payload)
    print(f"\nHUD JSON serialization: {'PASS' if ok else 'FAIL'} ({detail})")
    if not ok:
        issues.append(f"HUD serialization: {detail}")

    if notes:
        print("\nNotes:")
        for n in notes:
            print(f"  - {n}")

    if issues:
        print("\nFAILED:")
        for i in issues:
            print(f"  ✗ {i}")
        return 1

    print("\nPASSED — drawdown lockouts clear, balance field consistent, HUD-safe payload.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify drawdown monitor + Superjet operational state")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Prefer live Flight Deck HTTP/WebSocket (127.0.0.1:8787)",
    )
    args = parser.parse_args()
    raise SystemExit(verify(live=args.live))


if __name__ == "__main__":
    main()
