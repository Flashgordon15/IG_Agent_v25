#!/usr/bin/env python3
"""
Live open-position assess + manage (works with or without full agent).

When agent is down: one-shot IG REST assessment and immediate exits for
positions breaching GBP risk rules.

When agent is up: reconciles in-process risk stack then reports.

Usage:
  PYTHONPATH=src python3 scripts/manage_live_positions.py
  PYTHONPATH=src python3 scripts/manage_live_positions.py --manage
  PYTHONPATH=src python3 scripts/manage_live_positions.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from execution.open_position_actions import execute_actions_bulk
from execution.open_position_rules import (
    ManageReport,
    assess_open_positions,
    rows_from_ig_items,
)


def _agent_up(port: int = 8080) -> bool:
    try:
        import urllib.request

        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=3
        ) as resp:
            import json as _json

            body = _json.loads(resp.read().decode())
            return bool(body.get("ok"))
    except Exception:
        return False


def _load_rest() -> Any:
    from system.credentials_loader import load_credentials
    from system.ig_rest_session import ensure_shared_authenticated

    return ensure_shared_authenticated(load_credentials())


def assess_positions(rest: Any, cfg: Any, *, arm_stack: bool = True) -> ManageReport:
    from execution.open_position_rules import rows_from_snapshot_positions
    from execution.position_risk_stack import reconcile_open_positions_risk_stack
    from runtime import broker_snapshot

    report_agent_up = _agent_up()
    gbp_tracks: dict[str, Any] = {}

    if arm_stack or report_agent_up:
        try:
            from runtime.micro_gbp_exit import snapshot as gbp_snap
            from runtime.micro_gbp_exit import start_micro_gbp_exit_engine

            start_micro_gbp_exit_engine(rest)
            counts = reconcile_open_positions_risk_stack(rest, cfg=cfg, force=True)
            gbp_tracks = gbp_snap().get("tracks") or {}
            arm_issues = [
                f"risk_stack armed={counts.get('armed', 0)} "
                f"gbp={counts.get('gbp', 0)} pruned={counts.get('pruned', 0)}"
            ]
        except Exception as exc:
            arm_issues = [f"risk_stack: {type(exc).__name__}: {exc}"]
        else:
            arm_issues = arm_issues if arm_stack else []
    else:
        arm_issues = []

    rows = []
    source = "rest"
    # Prefer fresh snapshot; under coalesce pressure accept last-good (any age).
    shared = broker_snapshot.read_snapshot(max_age_sec=45.0)
    if shared is None:
        try:
            from system.rest_api_budget import positions_poll_deferred

            if positions_poll_deferred():
                shared = broker_snapshot.read_snapshot(max_age_sec=None)
        except Exception:
            shared = broker_snapshot.read_snapshot(max_age_sec=None)
    if shared and shared.get("positions") is not None:
        rows = rows_from_snapshot_positions(
            list(shared.get("positions") or []),
            cfg,
            gbp_tracks=gbp_tracks,
        )
        source = f"broker_snapshot({shared.get('source')})"
    if not rows and not (shared and int(shared.get("count") or 0) == 0):
        try:
            items = list(rest.open_positions(budget_priority=True) or [])
            rows = rows_from_ig_items(
                items,
                cfg,
                gbp_tracks=gbp_tracks,
            )
            try:
                # Only rewrite snapshot from live REST items (not snapshot echo).
                if items and not any(
                    isinstance(it, dict) and it.get("_from_snapshot") for it in items
                ):
                    broker_snapshot.write_snapshot(
                        source="manage_live_positions", items=items
                    )
            except Exception:
                pass
            source = "rest"
        except Exception as exc:
            fallback = broker_snapshot.read_snapshot(max_age_sec=None)
            if fallback is not None:
                rows = rows_from_snapshot_positions(
                    list(fallback.get("positions") or []),
                    cfg,
                    gbp_tracks=gbp_tracks,
                )
                source = f"last_good_snapshot({fallback.get('source')})"
                arm_issues.append(
                    f"open_positions deferred ({type(exc).__name__}) — using last-good"
                )
            else:
                raise
    report = assess_open_positions(
        rows,
        cfg,
        gbp_tracks=gbp_tracks,
        agent_up=report_agent_up,
        source=source,
    )
    broker_ids = {r.deal_id for r in rows}
    unmonitored = [did for did in broker_ids if did not in gbp_tracks]
    if unmonitored:
        report.issues.append(
            f"{len(unmonitored)} position(s) missing GBP exit track"
        )
    report.issues = arm_issues + report.issues
    return report


def _print_report(report: ManageReport, *, managed: bool) -> None:
    print("=" * 60)
    print("LIVE POSITION MANAGEMENT")
    print("=" * 60)
    print(f"Agent up:       {report.agent_up}")
    print(f"Broker open:    {report.broker_open}")
    print(f"Assessed:       {report.assessed}")
    print(f"Actions queued: {len(report.actions)}")
    if managed:
        done = sum(1 for a in report.actions if a.ok)
        print(f"Actions done:   {done}")
    print()
    for r in report.positions[:20]:
        pnl = r.get("pnl_gbp")
        flag = ""
        if pnl is not None:
            if pnl >= r.get("target_gbp", 999):
                flag = " [TARGET]"
            elif pnl <= -r.get("soft_loss_gbp", 0):
                flag = " [SOFT LOSS]"
            elif pnl <= -r.get("loss_cap_gbp", 0):
                flag = " [LOSS CAP]"
        print(
            f"  {r['deal_id'][:12]} {r['epic'][:24]:24} "
            f"pnl={pnl} tgt={r.get('target_gbp')} cap=-{r.get('loss_cap_gbp')}{flag}"
        )
    if report.issues:
        print()
        print("Issues:")
        for i in report.issues[:10]:
            print(f"  - {i}")
    if report.actions:
        print()
        print("Actions:")
        for a in report.actions:
            status = "OK" if a.ok else ("PENDING" if not managed else f"FAIL {a.error}")
            print(f"  {a.deal_id[:12]} {a.action} {a.reason} [{status}]")
    if not report.agent_up:
        print()
        print(
            "Note: Agent is DOWN — run supervise then restart for continuous desk management:\n"
            "  IG_AGENT_CONFIG=config/config_v31_demo_throughput.json \\\n"
            "    PYTHONPATH=src python3 scripts/manage_live_positions.py --supervise\n"
            "  bash scripts/start_agent_background.sh"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Assess and manage open IG positions")
    parser.add_argument(
        "--supervise",
        action="store_true",
        help="Arm risk stack for every broker open, assess, and manage breaches",
    )
    parser.add_argument(
        "--manage",
        action="store_true",
        help="Execute flatten actions for loss-cap / target breaches",
    )
    parser.add_argument(
        "--reconcile-db",
        action="store_true",
        help="Also reconcile learning-db phantom opens against broker",
    )
    parser.add_argument(
        "--supervise-loop",
        action="store_true",
        help="Continuous supervise loop (offline daemon when agent REST-starved)",
    )
    parser.add_argument(
        "--poll-sec",
        type=float,
        default=30.0,
        help="Poll interval for --supervise-loop (default 30)",
    )
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()
    if args.supervise:
        args.manage = True

    from system.config_loader import get_config

    cfg = get_config()
    rest = _load_rest()

    if args.supervise_loop:
        import time as _time

        poll = max(10.0, float(args.poll_sec))
        print(f"Supervise loop every {poll:.0f}s — Ctrl+C to stop")
        while True:
            try:
                report = assess_positions(rest, cfg, arm_stack=True)
                if args.manage and report.actions:
                    execute_actions_bulk(rest, report, cfg)
                if args.json:
                    print(
                        json.dumps(
                            {
                                "broker_open": report.broker_open,
                                "actions": len(report.actions),
                                "issues": report.issues[:5],
                            }
                        )
                    )
                else:
                    _print_report(report, managed=args.manage)
            except KeyboardInterrupt:
                return 0
            except Exception as exc:
                print(f"supervise-loop error: {type(exc).__name__}: {exc}")
            _time.sleep(poll)

    try:
        from runtime.active_lifecycle_trades import boot_reconcile_active_trades
        from data.learning_store import LearningStore
        from system.paths import data_dir

        store = LearningStore(str(data_dir() / "learning_db.sqlite3"))
        boot_reconcile_active_trades(rest, store)
        if args.reconcile_db:
            sys.path.insert(0, str(ROOT / "scripts"))
            from reconcile_phantom_opens import reconcile_phantom_opens

            reconcile_phantom_opens(dry_run=False)
    except Exception:
        pass

    report = assess_positions(rest, cfg, arm_stack=True)

    if args.manage and report.actions:
        execute_actions_bulk(rest, report, cfg)

    if args.json:
        out = {
            "agent_up": report.agent_up,
            "broker_open": report.broker_open,
            "assessed": report.assessed,
            "positions": report.positions,
            "issues": report.issues,
            "actions": [
                {
                    "deal_id": a.deal_id,
                    "epic": a.epic,
                    "pnl_gbp": a.pnl_gbp,
                    "action": a.action,
                    "reason": a.reason,
                    "ok": a.ok,
                    "error": a.error,
                }
                for a in report.actions
            ],
        }
        print(json.dumps(out, indent=2))
    else:
        _print_report(report, managed=args.manage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
