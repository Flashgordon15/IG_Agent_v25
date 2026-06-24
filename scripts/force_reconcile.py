#!/usr/bin/env python3
"""
Administrative reconciliation — clear stale pending orders and rebuild alpha matrix SHM.

1. Query IG REST open positions (OTC book).
2. Force-resolve overdue pending confirms for night-matrix epics.
3. Sync fulfillment performance rows from broker truth.
4. Run fast_bootstrap_alpha_matrix_if_empty() with secure_fill_matrix_update.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

NIGHT_MATRIX_EPICS = (
    "CS.D.CFPGOLD.CFP.IP",
    "IX.D.DOW.IFM.IP",
    "IX.D.NIKKEI.IFM.IP",
    "CS.D.EURUSD.CFD.IP",
)

OVERDUE_CLEAR_EPICS = (
    "IX.D.DOW.IFM.IP",
    "CS.D.EURUSD.CFD.IP",
)


def _open_count_by_epic(positions: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in positions or []:
        if not isinstance(item, dict):
            continue
        market = item.get("market") or {}
        epic = str(market.get("epic") or "").strip()
        if not epic:
            continue
        counts[epic] = counts.get(epic, 0) + 1
    return counts


def _clear_inflight_via_api(epic: str, *, port: int = 8080) -> bool:
    url = f"http://127.0.0.1:{port}/api/clear_inflight/{epic}"
    try:
        req = urllib.request.Request(url, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        print(f"FORCE-RECONCILE: api clear_inflight {epic} -> {body}")
        return True
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"FORCE-RECONCILE: api clear_inflight {epic} failed: {exc}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Admin reconcile + alpha matrix bootstrap")
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="Skip IG REST (use when live agent holds rate budget); clears via :8080 API",
    )
    parser.add_argument("--api-port", type=int, default=8080)
    args = parser.parse_args()

    from execution.entry_inflight import clear_entry
    from execution.pending_order_reconcile import (
        list_pending_orders,
        reconcile_all_pending_from_broker,
        resolve_pending,
    )
    from system.engine_log import log_engine

    cleared = 0
    perf_rows = 0
    by_epic: dict[str, int] = {}

    if args.local_only:
        print("FORCE-RECONCILE: local-only — skipping IG REST, using agent API")
        for epic in NIGHT_MATRIX_EPICS:
            _clear_inflight_via_api(epic, port=args.api_port)
            clear_entry(epic)
    else:
        from system.credentials_loader import load_credentials
        from system.ig_rest_session import ensure_shared_authenticated
        from system.unified_fulfillment_cache import sync_performance_rows_from_ig_rest

        print("FORCE-RECONCILE: connecting IG REST session…")
        rest = ensure_shared_authenticated(load_credentials())
        positions = rest.open_positions() if hasattr(rest, "open_positions") else []
        by_epic = _open_count_by_epic(list(positions or []))
        print(f"FORCE-RECONCILE: broker open positions by epic: {by_epic or '{}'}")

        pending_before = list_pending_orders()
        if pending_before:
            print(
                "FORCE-RECONCILE: pending before:",
                [(p.epic, p.order_type, p.broker_deal_reference) for p in pending_before],
            )

        for epic in OVERDUE_CLEAR_EPICS:
            if resolve_pending(epic, reason="force_reconcile:admin_cleared_overdue"):
                print(f"FORCE-RECONCILE: cleared pending_order_reconcile epic={epic}")
            clear_entry(epic)
            print(f"FORCE-RECONCILE: cleared entry_inflight epic={epic}")

        cleared = reconcile_all_pending_from_broker(by_epic, stale_entry_grace_sec=1.0)
        print(f"FORCE-RECONCILE: reconcile_all_pending_from_broker cleared={cleared}")

        for epic in NIGHT_MATRIX_EPICS:
            resolve_pending(epic, reason="force_reconcile:night_matrix_sweep")
            clear_entry(epic)

        perf_rows = sync_performance_rows_from_ig_rest(force=True)
        print(f"FORCE-RECONCILE: fulfillment cache synced perf_rows={perf_rows}")

    from intelligence.matrix_prebaker import (
        alpha_matrix_mapped,
        fast_bootstrap_alpha_matrix_if_empty,
        matrix_compiler_telemetry,
    )

    booted = fast_bootstrap_alpha_matrix_if_empty(stride=48)
    tel = matrix_compiler_telemetry()
    print(
        f"FORCE-RECONCILE: alpha_matrix booted={booted} "
        f"mapped={alpha_matrix_mapped()} "
        f"cells={tel.get('cells_populated')} "
        f"status={tel.get('status')}"
    )
    log_engine(
        f"force_reconcile complete cleared_pending={cleared} "
        f"perf_rows={perf_rows} alpha_boot={booted}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
