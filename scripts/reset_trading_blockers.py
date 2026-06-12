#!/usr/bin/env python3
"""Clear test-session blockers so demo trading can resume without deleting audit rows."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.learning_store import LearningStore
from execution.order_validator import OrderValidator
from system.config_loader import ConfigLoader
from system.engine_log import log_engine
from system.paths import config_dir, data_dir
from system.v291_upgrade import refresh_today_daily_loss_baseline


def _load_points_path() -> Path:
    return data_dir() / "state" / "points_state.json"


def reset_points_state(*, keep_cumulative: float | None = None) -> None:
    path = _load_points_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    cumulative = (
        float(keep_cumulative)
        if keep_cumulative is not None
        else float(existing.get("cumulative") or 6.0)
    )
    payload = {
        "version": 1,
        "cumulative": cumulative,
        "cumulative_points": cumulative,
        "state": "HEALTHY",
        "session_score": 0.0,
        "last_trade_score": 0.0,
        "consecutive_losses": 0,
        "signals_to_skip": 0,
        "recovery_wins": int(existing.get("recovery_wins") or 0),
        "bootstrap_wins": int(existing.get("bootstrap_wins") or 0),
        "day_stopped": False,
        "stop_latched": False,
        "last_nominal": "HEALTHY",
        "rapid_cooldown_until": 0.0,
        "equity_lock_active": False,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def reset_runtime_json_daily_risk() -> None:
    path = data_dir() / "runtime_state.json"
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    if isinstance(data, dict):
        data["daily_risk"] = {}
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reset circuit breaker, daily loss baseline, cooldowns, and points pauses"
    )
    parser.add_argument(
        "--reason",
        default="operator_reset",
        help="Audit label stored in runtime_state",
    )
    parser.add_argument(
        "--skip-points",
        action="store_true",
        help="Do not rewrite points_state.json",
    )
    args = parser.parse_args()

    cfg = ConfigLoader(config_dir() / "config_v29.json").load_config()
    store = LearningStore(data_dir() / "learning_db.sqlite3")

    streak_info = store.reset_consecutive_loss_streak(reason=args.reason)
    loss_info = refresh_today_daily_loss_baseline(store, cfg=cfg, reason=args.reason)
    cooldowns = store.clear_all_cooldowns()
    reset_runtime_json_daily_risk()

    if not args.skip_points:
        reset_points_state()

    ov = OrderValidator(cfg)
    ov.attach_store(store)
    circuit_ok, circuit_msg = ov.check_circuit_breaker()

    log_engine(
        f"reset_trading_blockers: streak_archived={streak_info['archived_streak']} "
        f"effective_loss={loss_info.get('effective_loss_gbp')} "
        f"cooldowns_cleared={cooldowns} circuit_ok={circuit_ok}"
    )

    print("Trading blockers reset")
    print(f"  archived consecutive loss streak: {streak_info['archived_streak']}")
    print(f"  effective daily loss (GBP):       {loss_info.get('effective_loss_gbp')}")
    print(f"  cooldown rows cleared:            {cooldowns}")
    print(f"  circuit breaker:                  {'OPEN' if circuit_ok else 'BLOCKED'}")
    if not circuit_ok:
        print(f"    {circuit_msg}")
    print(
        "  points session pause cleared (file); in-memory points reload on next agent restart"
    )
    return 0 if circuit_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
