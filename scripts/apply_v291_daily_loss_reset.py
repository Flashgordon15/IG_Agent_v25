#!/usr/bin/env python3
"""Reset today's daily loss baseline so gate-effective loss is £0 (trading can fire)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.learning_store import LearningStore
from system.config_loader import ConfigLoader
from system.daily_loss_policy import (
    daily_loss_gate_status,
    effective_daily_loss_gbp,
    effective_daily_pnl,
)
from system.v291_upgrade import refresh_today_daily_loss_baseline


def main() -> int:
    cfg = ConfigLoader().load_config()
    store = LearningStore(str(cfg.learning_db))
    raw_before = float(store.sum_daily_pnl())
    loss_before = effective_daily_loss_gbp(store)
    ok_before, detail_before, _ = daily_loss_gate_status(store, cfg)

    result = refresh_today_daily_loss_baseline(
        store, cfg=cfg, reason="manual_script"
    )

    raw_after = effective_daily_pnl(store)
    loss_after = effective_daily_loss_gbp(store)
    ok_after, detail_after, _ = daily_loss_gate_status(store, cfg)

    print("Today's daily loss reset (gate baseline archived)")
    print(f"  raw_daily_pnl (ledger):     £{raw_before:+.2f}")
    print(f"  before effective_loss_gbp:  £{loss_before:.2f}  gate_ok={ok_before}")
    if not ok_before:
        print(f"    blocked: {detail_before}")
    print(f"  reset result:               {result}")
    print(f"  after effective_loss_gbp:   £{loss_after:.2f}  gate_ok={ok_after}")
    print(f"    gate detail: {detail_after}")
    print()
    if ok_after:
        print("✅ Trading gates should allow entries (until new closes add loss).")
    else:
        print("⚠️  Gates still blocked — check detail above.")
    return 0 if ok_after else 1


if __name__ == "__main__":
    raise SystemExit(main())
