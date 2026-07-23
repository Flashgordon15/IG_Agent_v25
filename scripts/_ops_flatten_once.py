#!/usr/bin/env python3
"""Operator one-shot: flatten all broker opens via IG REST (demo-safe)."""
from __future__ import annotations

import json
import sys
import time


def main() -> int:
    from system.config_loader import load_active_config
    from system.credentials_loader import try_load_credentials
    from system.ig_rest_session import ensure_shared_authenticated
    from execution.exit_inflight import clear_exit

    status = try_load_credentials()
    if not status.ok or status.credentials is None:
        print(json.dumps({"ok": False, "error": status.error}))
        return 2
    cfg = load_active_config(validate=False)
    rest = ensure_shared_authenticated(status.credentials)

    def list_open():
        items = list(rest.open_positions(budget_priority=True) or [])
        out = []
        for it in items:
            pos = it.get("position") or {}
            mkt = it.get("market") or {}
            deal_id = str(pos.get("dealId") or "")
            epic = str(mkt.get("epic") or "")
            side = str(pos.get("direction") or "BUY").upper()
            size = float(pos.get("size") or 0)
            if deal_id and size > 0:
                out.append((deal_id, epic, side, size))
        return out

    closed: list[str] = []
    errors: list[str] = []
    for round_i in range(1, 5):
        rows = list_open()
        print(f"ROUND {round_i} open={len(rows)}", flush=True)
        if not rows:
            break
        for deal_id, epic, side, size in rows:
            if epic:
                try:
                    clear_exit(epic)
                except Exception:
                    pass
            # Pass OPEN side: skip_lookup path inverts once. Do NOT pass close_dir.
            try:
                # Use epic for IG net-close body; bypass exit_inflight latch via
                # _do_close_position so sibling DOW closes are not skipped.
                rest._do_close_position(
                    deal_id,
                    direction=side,  # OPEN side
                    size=size,
                    epic=epic or None,
                    currency_code="USD",  # index CFD net-close working path
                    verify=False,
                    budget_priority=True,
                    skip_lookup=True,
                    skip_confirm=True,
                )
                closed.append(deal_id)
                print(f"CLOSED {deal_id}", flush=True)
            except Exception as exc:
                errors.append(f"{deal_id}: {type(exc).__name__}: {exc}")
                print(f"ERR {deal_id} {exc}", flush=True)
            time.sleep(0.8)
        time.sleep(2)

    left = list_open()
    try:
        from runtime import broker_snapshot

        items = list(rest.open_positions(budget_priority=True) or [])
        broker_snapshot.write_snapshot(source="operator_flatten", items=items)
    except Exception as exc:
        print(f"snapshot_err {exc}", flush=True)

    print(
        json.dumps(
            {
                "ok": len(left) == 0,
                "closed": closed,
                "errors": errors,
                "final_open": len(left),
                "left_ids": [r[0] for r in left],
            },
            indent=2,
        ),
        flush=True,
    )
    return 0 if not left else 1


if __name__ == "__main__":
    sys.exit(main())
