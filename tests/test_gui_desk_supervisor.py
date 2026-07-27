"""Unit/integration — GUI desk supervisor findings, handoff, heal allowlist, silence."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from runtime import gui_desk_supervisor as gds
from runtime import gui_desk_supervisor_heal as heal


def _health(
    *,
    paused: bool = False,
    accepting: bool = True,
    ready: bool = True,
    pid: int = 1,
    trade_ready: bool = True,
) -> dict:
    return {
        "ok": not paused,
        "trading_paused": paused,
        "trade_ready": trade_ready,
        "agent_pid": pid,
        "issues": ["trading_paused"] if paused else [],
        "boot_metrics": {
            "ready": ready,
            "percent": 100 if ready else 50,
            "system_state": {
                "ready": ready,
                "loops": {"built": 7, "running": True, "accepting_ticks": accepting},
            },
        },
        "iron_cage": {"trade_ready": trade_ready, "execution": {"loop_active": True}},
    }


def _bundle_from_health(health: dict, *, listen: bool = True, hung: bool = False) -> dict:
    return {
        "base": "http://127.0.0.1:x",
        "reachable": (not hung) and health is not None,
        "listen": listen,
        "hung_api": hung,
        "health": None if hung else health,
        "health_err": "TimeoutError: timed out" if hung else None,
        "positions": {"ok": True, "count": 0, "verdict": "FLAT", "critical": False},
        "positions_err": None,
        "trade_support": {"running": True, "ok": True, "broker_open": 0},
        "trade_support_err": None,
        "liveness": {"ok": True},
        "liveness_err": None,
        "stability": {},
        "stability_err": None,
        "ops_strip": {"rest_pressure_level": "OK", "rest_calls_last_minute": 0},
        "ops_strip_err": None,
        "accounting": {"today_net_realized_pnl_gbp": -10.0, "source": "journal_csv", "last_10_closed_trades": []},
        "accounting_err": None,
        "rotation": {
            "rotation": {
                "ranked_rotator": {
                    "active": True,
                    "mode": "ranked",
                    "promoted": ["IX.D.DOW.IFM.IP"],
                    "dominant": "IX.D.DOW.IFM.IP",
                },
                "active_instruments": [],
            }
        },
        "rotation_err": None,
        "state": {
            "ml_confidence": 0.4,
            "signal_strength": 0.2,
            "routing": [
                {
                    "epic": "IX.D.DOW.IFM.IP",
                    "execution_path": "PATH_A",
                    "contributing_factors": {
                        "enforcement": {"hard_allow": ["PATH_A"], "hard_block": ["MICRO"]},
                        "controller_ownership": "SCALP",
                    },
                    "route_flags": ["SB_MACRO_PATH_A_CARVE"],
                }
            ],
        },
        "state_err": None,
    }


class GuiDeskSupervisorTests(unittest.TestCase):
    def test_cash_merge_shared_once(self) -> None:
        cash = gds._cash_merge_sanity(
            {"today_net_realized_pnl_gbp": -100.0},
            {"today_net_realized_pnl_gbp": -100.5},
        )
        self.assertEqual(cash["mode"], "shared_journal_once")
        self.assertFalse(cash["double_count_risk"])

    def test_cursor_handoff_shape_when_needs_code(self) -> None:
        findings = [
            gds._finding(
                rank=1,
                severity="fail",
                klass="code",
                title="SB TradingLoops not accepting ticks (STUCK)",
                detail="accepting_ticks=false",
                needs_code=True,
            )
        ]
        handoff = gds._build_cursor_handoff(score="FAIL", findings=findings)
        self.assertIsNotNone(handoff)
        assert handoff is not None
        self.assertTrue(handoff["preserve_a2"])
        self.assertIn("suspected_files", handoff)
        self.assertIn("forbidden_actions", handoff)
        self.assertTrue(any("kill -9" in a.lower() or "SIGKILL" in a for a in handoff["forbidden_actions"]))
        self.assertIn("blurb", handoff)
        self.assertEqual(handoff["top_finding"]["needs_code"], True)

    def test_silence_tracker_arms_and_expires(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            t0 = 1_000_000.0
            s1 = gds._update_silence_tracker(data_root=root, sb_armed=True, activity=False, now=t0)
            self.assertTrue(s1["sb_armed"])
            self.assertEqual(s1["silence_sec"], 0.0)
            s2 = gds._update_silence_tracker(
                data_root=root, sb_armed=True, activity=False, now=t0 + 31 * 60
            )
            self.assertGreaterEqual(s2["silence_sec"], 30 * 60)

    def test_assess_stuck_loops_fail_and_handoff(self) -> None:
        cfd = _bundle_from_health(_health(paused=True, accepting=False, pid=10))
        sb = _bundle_from_health(_health(paused=False, accepting=False, ready=True, pid=11))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "state_cfd").mkdir(parents=True)
            (root / "state_cfd" / "a2_entries_paused.json").write_text(
                json.dumps({"active": True, "mode": "A2_SB_ONLY"}), encoding="utf-8"
            )
            with patch.object(gds, "_port_bundle", side_effect=[cfd, sb]), patch.object(
                gds, "_tcp_reachable", return_value=True
            ), patch.object(
                gds,
                "_read_dual_core_posture",
                return_value={
                    "exclude_from_hot_path": ["IX.D.NIKKEI.IFM.IP"],
                    "ranked_candidate_epics": ["IX.D.DOW.IFM.IP", "CS.D.CFPGOLD.CFP.IP"],
                    "sb_hot_path_allowlist": ["IX.D.DOW.IFM.IP"],
                    "ranked_rotator_mode": True,
                    "sb_macro_ltr_entries_only": True,
                    "sb_disable_instant_micro": True,
                    "sb_disable_core_b_micro": True,
                    "sb_path_a_carve_expected": True,
                    "sources": [],
                },
            ), patch.object(gds, "_cheap_log_tick_smell", return_value={"ok": True, "dormant_hits": 0, "entering_tick_hits": 1, "bytes_read": 10}):
                payload = gds.assess(data_root=root)
        self.assertEqual(payload["score"], "FAIL")
        self.assertTrue(payload["needs_code"])
        self.assertIsNotNone(payload["cursor_handoff"])
        self.assertTrue(payload["dashboard_chip"]["visible"])
        titles = [f["title"] for f in payload["findings"]]
        self.assertTrue(any("not accepting ticks" in t for t in titles))

    def test_heal_allowlist_and_no_forbidden(self) -> None:
        payload = {
            "ports": {
                "cfd": {"port": 8080, "reachable": False, "listen": True, "hung_api": True, "broker_open": 0, "positions_verdict": "FLAT"},
                "sb": {"port": 8081, "reachable": True, "listen": True, "hung_api": False, "broker_open": 0, "positions_verdict": "FLAT", "trading_paused": False},
                "ui": {"port": 3000, "tcp": False, "http": False},
            },
            "a2": {"marker_active": True, "cfd_trading_paused": False, "sb_trading_paused": False},
            "stuck_plane": {
                "cfd_loops": {"accepting_ticks": False, "boot_ready": True, "trade_ready": True},
                "sb_loops": {"accepting_ticks": True, "boot_ready": True, "trade_ready": True},
            },
            "findings": [
                {
                    "severity": "fail",
                    "title": ":8081 ARMED but silent (zero-attempt silence timer)",
                    "needs_code": True,
                }
            ],
        }
        plans = heal.plan_heals(payload)
        actions = {p["action"] for p in plans}
        self.assertIn(heal.HEAL_PORT_HUNG, actions)
        self.assertIn(heal.HEAL_UI_DOWN, actions)
        self.assertIn(heal.HEAL_REAPPLY_A2, actions)
        self.assertIn(heal.HEAL_SILENCE_SOFT_PAUSE, actions)
        heal.assert_no_forbidden_in_plan(plans)
        for p in plans:
            self.assertIn(p["action"], heal.ALLOWED_HEALS)

    def test_heal_dry_run_no_mutation_and_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans = [
                {
                    "action": heal.HEAL_UI_DOWN,
                    "requires_flat": False,
                    "ports": [3000],
                    "books_flat": True,
                    "blocked_by_open_book": False,
                }
            ]
            res = heal.execute_heal_plan(plans, dry_run=True, root=root, cap=2)
            self.assertTrue(res["ok"])
            self.assertTrue(res["dry_run"])
            # Simulate cap exhausted
            budget = {"heals": [{"ts": time.time(), "action": "x"}, {"ts": time.time(), "action": "y"}]}
            heal.save_heal_budget(budget, root=root)
            res2 = heal.execute_heal_plan(plans, dry_run=False, root=root, cap=2)
            self.assertTrue(res2.get("hard_fail"))
            self.assertFalse(res2["ok"])

    def test_soft_term_never_sigkill_flag(self) -> None:
        # API contract: soft_term_pid always reports used_sigkill False
        out = heal.soft_term_pid(0)
        self.assertFalse(out.get("used_sigkill"))

    def test_open_book_blocks_recycle(self) -> None:
        payload = {
            "ports": {
                "cfd": {"port": 8080, "reachable": False, "listen": True, "hung_api": True, "broker_open": 2, "positions_verdict": "HEALTHY"},
                "sb": {"port": 8081, "reachable": True, "broker_open": 0, "positions_verdict": "FLAT"},
                "ui": {"port": 3000, "tcp": True, "http": True},
            },
            "a2": {"marker_active": True, "cfd_trading_paused": True, "sb_trading_paused": False},
            "stuck_plane": {"cfd_loops": {}, "sb_loops": {}},
            "findings": [],
        }
        self.assertFalse(heal.books_flat(payload))
        plans = heal.plan_heals(payload)
        hung = next(p for p in plans if p["action"] == heal.HEAL_PORT_HUNG)
        self.assertTrue(hung["blocked_by_open_book"])

    def test_operator_bleed_lock_blocks_unpause_and_loops_heal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_dir = root / "state_sb"
            lock_dir.mkdir(parents=True)
            (lock_dir / "operator_bleed_lock_2026-07-24.json").write_text(
                json.dumps(
                    {
                        "active": True,
                        "reason": "operator_halt_unacceptable_bleed",
                        "do_not_auto_resume": True,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            blocked, lock = heal.operator_bleed_lock_blocks_resume(root=root)
            self.assertTrue(blocked)
            self.assertEqual(lock.get("reason"), "operator_halt_unacceptable_bleed")

            res = heal.unpause_port(port=8081, dry_run=True, root=root)
            self.assertFalse(res.get("ok"))
            self.assertTrue(res.get("blocked_by_operator_bleed_lock"))
            self.assertIn("SKIP POST /api/start", str(res.get("planned") or ""))

            plans = [
                {
                    "action": heal.HEAL_LOOPS_NOT_ARMING,
                    "ports": [8081],
                    "books_flat": True,
                    "blocked_by_open_book": False,
                    "try_unpause_first": True,
                }
            ]
            out = heal.execute_heal_plan(plans, dry_run=True, root=root, cap=5)
            self.assertTrue(any(s.get("skip_reason") == "operator_bleed_lock" for s in out["skipped"]))
            self.assertTrue(any("operator bleed lock" in e for e in out["escalations"]))
            self.assertEqual(out["executed"], [])

            # Silence soft-pause (stop) remains allowlisted even with lock.
            pause_plan = [
                {
                    "action": heal.HEAL_SILENCE_SOFT_PAUSE,
                    "ports": [8081],
                    "books_flat": True,
                    "blocked_by_open_book": False,
                }
            ]
            out2 = heal.execute_heal_plan(pause_plan, dry_run=True, root=root, cap=5)
            self.assertTrue(out2["executed"])
            self.assertIn("/api/stop", str(out2["executed"][0].get("planned") or ""))

    def test_halted_bleed_lock_never_pass(self) -> None:
        cfd = _bundle_from_health(_health(paused=True, accepting=False, pid=10))
        sb = _bundle_from_health(_health(paused=True, accepting=False, pid=11))
        # Prefer/SETUP while paused → GUI_LIE as well
        sb["rotation"] = {
            "prefer_epic": "IX.D.DOW.IFM.IP",
            "rotation": {
                "prefer_epic": "IX.D.DOW.IFM.IP",
                "ranked_rotator": {
                    "active": True,
                    "mode": "ranked",
                    "promoted": ["IX.D.DOW.IFM.IP"],
                    "prefer_epic": "IX.D.DOW.IFM.IP",
                    "rows": [
                        {
                            "epic": "IX.D.DOW.IFM.IP",
                            "mode": "SETUP",
                            "p_success": 0.8,
                            "approved": True,
                            "threshold": 0.68,
                        }
                    ],
                    "per_epic_confidence": {
                        "IX.D.DOW.IFM.IP": {
                            "mode": "SETUP",
                            "p_success": 0.8,
                            "approved": True,
                            "threshold": 0.68,
                        }
                    },
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "state_sb").mkdir(parents=True)
            (root / "state_cfd").mkdir(parents=True)
            (root / "metrics").mkdir(parents=True)
            (root / "state_sb" / "operator_bleed_lock_2026-07-24.json").write_text(
                json.dumps(
                    {
                        "active": True,
                        "reason": "operator_halt_unacceptable_bleed",
                        "do_not_auto_resume": True,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with patch.object(gds, "_port_bundle", side_effect=[cfd, sb]), patch.object(
                gds, "_tcp_reachable", return_value=True
            ), patch.object(
                gds,
                "_read_dual_core_posture",
                return_value={
                    "exclude_from_hot_path": ["IX.D.NIKKEI.IFM.IP"],
                    "ranked_candidate_epics": ["IX.D.DOW.IFM.IP", "CS.D.CFPGOLD.CFP.IP"],
                    "sb_hot_path_allowlist": ["IX.D.DOW.IFM.IP"],
                    "ranked_rotator_mode": True,
                    "sb_macro_ltr_entries_only": True,
                    "sb_disable_instant_micro": True,
                    "sb_disable_core_b_micro": True,
                    "sb_path_a_carve_expected": True,
                    "sources": [],
                },
            ), patch.object(
                gds,
                "_cheap_log_tick_smell",
                return_value={"ok": True, "dormant_hits": 0, "entering_tick_hits": 1, "bytes_read": 10},
            ):
                payload = gds.assess(data_root=root)
        self.assertEqual(payload["score"], "FAIL")
        self.assertTrue(payload["halted"])
        self.assertIn("HALTED", payload["alerts"])
        self.assertIn("BLEED", payload["alerts"])
        self.assertIn("GUI_LIE", payload["alerts"])
        self.assertTrue(payload["needs_ops"])
        self.assertTrue(payload["needs_code"])
        self.assertTrue(payload["dashboard_chip"]["visible"])
        self.assertIn("HALTED", payload["dashboard_chip"]["label"])
        self.assertEqual(payload["dashboard_chip"]["tone"], "red")
        self.assertIsNotNone(payload["cursor_handoff"])
        titles = [f["title"] for f in payload["findings"]]
        self.assertTrue(any(t.startswith("HALTED:") for t in titles))
        self.assertTrue(any("GUI_LIE" in t for t in titles))

    def test_bleed_journal_thresholds_and_ensure_flag(self) -> None:
        cfd = _bundle_from_health(_health(paused=False, accepting=True, pid=10))
        sb = _bundle_from_health(_health(paused=False, accepting=True, pid=11))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "metrics").mkdir(parents=True)
            (root / "state").mkdir(parents=True)
            # 6 losing closes in window → BLEED
            now = datetime.now(timezone.utc)
            lines = [
                "Timestamp,DealID,Direction,EntryPrice,ExitPrice,RealizedPnL_GBP,"
                "ClosingFillRate,ActiveSlipMultiplier,AccountID,ProductType,"
                "EngineOrigin,ExitReason,HoldSec,Style,MlScoreAtEntry,MarketRegime\n"
            ]
            for i in range(6):
                ts = (now - timedelta(minutes=10 + i)).strftime("%Y-%m-%dT%H:%M:%SZ")
                lines.append(
                    f"{ts},DIAAAATEST{i:04d},BUY,1,1,-12.0,,0.5,Z6BAH3,Spreadbet,"
                    f"path_a,stop,8.0,macro,0.5,\n"
                )
            (root / "metrics" / "daily_journal.csv").write_text("".join(lines), encoding="utf-8")
            with patch.object(gds, "_port_bundle", side_effect=[cfd, sb]), patch.object(
                gds, "_tcp_reachable", return_value=True
            ), patch.object(
                gds,
                "_read_dual_core_posture",
                return_value={
                    "exclude_from_hot_path": ["IX.D.NIKKEI.IFM.IP"],
                    "ranked_candidate_epics": ["IX.D.DOW.IFM.IP"],
                    "sb_hot_path_allowlist": ["IX.D.DOW.IFM.IP"],
                    "ranked_rotator_mode": True,
                    "sb_macro_ltr_entries_only": True,
                    "sb_disable_instant_micro": True,
                    "sb_disable_core_b_micro": True,
                    "sb_path_a_carve_expected": True,
                    "sources": [],
                },
            ), patch.object(
                gds,
                "_cheap_log_tick_smell",
                return_value={"ok": True, "dormant_hits": 0, "entering_tick_hits": 1, "bytes_read": 10},
            ):
                payload = gds.assess(data_root=root)
        self.assertEqual(payload["score"], "FAIL")
        self.assertTrue(payload["ensure_bleed_halt"])
        self.assertIn("BLEED", payload["alerts"])
        self.assertIn("MICRO_HOLD", payload["alerts"])
        self.assertTrue(payload["needs_ops"])
        titles = " | ".join(f["title"] for f in payload["findings"])
        self.assertIn("BLEED:", titles)
        self.assertIn("MICRO_HOLD:", titles)

    def test_session_kill_day_net(self) -> None:
        stats = {
            "n": 20,
            "wins": 2,
            "losses": 18,
            "wr": 0.1,
            "net_gbp": -200.0,
            "holds": [],
            "median_hold_sec": None,
            "avg_hold_sec": None,
            "hold_samples": 0,
            "day": "2026-07-24",
        }
        cfd = _bundle_from_health(_health(paused=True, accepting=False, pid=10))
        sb = _bundle_from_health(_health(paused=True, accepting=False, pid=11))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "metrics").mkdir(parents=True)
            (root / "state").mkdir(parents=True)
            with patch.object(gds, "_port_bundle", side_effect=[cfd, sb]), patch.object(
                gds, "_tcp_reachable", return_value=True
            ), patch.object(gds, "_calendar_day_net_gbp", return_value=stats), patch.object(
                gds, "_read_recent_closes", return_value=[]
            ), patch.object(
                gds,
                "_read_dual_core_posture",
                return_value={
                    "exclude_from_hot_path": ["IX.D.NIKKEI.IFM.IP"],
                    "ranked_candidate_epics": ["IX.D.DOW.IFM.IP"],
                    "sb_hot_path_allowlist": ["IX.D.DOW.IFM.IP"],
                    "ranked_rotator_mode": False,
                    "sb_macro_ltr_entries_only": True,
                    "sb_path_a_carve_expected": True,
                    "sources": [],
                },
            ), patch.object(
                gds,
                "_cheap_log_tick_smell",
                return_value={"ok": True, "dormant_hits": 0, "entering_tick_hits": 0, "bytes_read": 0},
            ):
                payload = gds.assess(data_root=root)
        self.assertEqual(payload["score"], "FAIL")
        self.assertIn("SESSION_KILL", payload["alerts"])
        self.assertTrue(payload["ensure_bleed_halt"])

    def test_epic_policy_prefer_excluded(self) -> None:
        cfd = _bundle_from_health(_health(paused=False, accepting=True, pid=10))
        sb = _bundle_from_health(_health(paused=False, accepting=True, pid=11))
        sb["rotation"] = {
            "prefer_epic": "IX.D.NIKKEI.IFM.IP",
            "rotation": {
                "prefer_epic": "IX.D.NIKKEI.IFM.IP",
                "ranked_rotator": {
                    "active": True,
                    "promoted": ["IX.D.NIKKEI.IFM.IP"],
                    "prefer_epic": "IX.D.NIKKEI.IFM.IP",
                    "rows": [],
                    "per_epic_confidence": {},
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "metrics").mkdir(parents=True)
            (root / "state").mkdir(parents=True)
            with patch.object(gds, "_port_bundle", side_effect=[cfd, sb]), patch.object(
                gds, "_tcp_reachable", return_value=True
            ), patch.object(
                gds,
                "_read_dual_core_posture",
                return_value={
                    "exclude_from_hot_path": ["IX.D.NIKKEI.IFM.IP"],
                    "ranked_candidate_epics": ["IX.D.DOW.IFM.IP"],
                    "sb_hot_path_allowlist": ["IX.D.DOW.IFM.IP"],
                    "ranked_rotator_mode": True,
                    "sb_macro_ltr_entries_only": True,
                    "sb_path_a_carve_expected": True,
                    "sources": [],
                },
            ), patch.object(
                gds,
                "_cheap_log_tick_smell",
                return_value={"ok": True, "dormant_hits": 0, "entering_tick_hits": 1, "bytes_read": 10},
            ):
                payload = gds.assess(data_root=root)
        self.assertEqual(payload["score"], "FAIL")
        self.assertIn("EPIC_POLICY", payload["alerts"])
        self.assertTrue(any("EPIC_POLICY" in f["title"] for f in payload["findings"]))

    def test_ensure_bleed_halt_writes_lock_and_never_starts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(heal, "_post_api", return_value={"ok": True, "data": {"ok": True}, "error": None}) as post:
                res = heal.ensure_operator_bleed_halt(
                    ports=[8080, 8081],
                    root=root,
                    dry_run=False,
                    reason="operator_halt_unacceptable_bleed",
                )
            self.assertTrue(res["ok"])
            self.assertFalse(res.get("posted_start"))
            self.assertEqual(post.call_count, 2)
            for call in post.call_args_list:
                self.assertEqual(call.args[1], "/api/stop")
            locks = list((root / "state_cfd").glob("operator_bleed_lock_*.json")) + list(
                (root / "state_sb").glob("operator_bleed_lock_*.json")
            )
            self.assertEqual(len(locks), 2)
            body = json.loads(locks[0].read_text(encoding="utf-8"))
            self.assertTrue(body["do_not_auto_resume"])
            self.assertTrue(body["active"])

    def test_flicker_tracker_counts_prefer_flips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            t0 = 2_000_000.0
            s1 = gds._update_flicker_tracker(
                data_root=root, prefer_epic="A", setup_epics=["A"], now=t0
            )
            self.assertEqual(s1["flip_count_window"], 0)
            s2 = gds._update_flicker_tracker(
                data_root=root, prefer_epic="B", setup_epics=["B"], now=t0 + 60
            )
            self.assertGreaterEqual(s2["flip_count_window"], 1)

    def test_reopen_witness_holds_auto_lock_on_prior_damage(self) -> None:
        """Pre-halt journal must not instantly re-lock after operator reopen."""
        cfd = _bundle_from_health(_health(paused=False, accepting=True, pid=10))
        sb = _bundle_from_health(_health(paused=False, accepting=True, pid=11))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "metrics").mkdir(parents=True)
            (root / "state").mkdir(parents=True)
            now = datetime.now(timezone.utc)
            # Closes BEFORE reopen watermark (prior damage)
            lines = [
                "Timestamp,DealID,Direction,EntryPrice,ExitPrice,RealizedPnL_GBP,"
                "ClosingFillRate,ActiveSlipMultiplier,AccountID,ProductType,"
                "EngineOrigin,ExitReason,HoldSec,Style,MlScoreAtEntry,MarketRegime\n"
            ]
            for i in range(8):
                ts = (now - timedelta(minutes=40 + i)).strftime("%Y-%m-%dT%H:%M:%SZ")
                lines.append(
                    f"{ts},DIAAAAPRIOR{i:04d},BUY,1,1,-20.0,,0.5,Z6BAH3,Spreadbet,"
                    f"path_a,stop,5.0,macro,0.5,\n"
                )
            (root / "metrics" / "daily_journal.csv").write_text("".join(lines), encoding="utf-8")
            gds.write_reopen_witness(
                root,
                day_net_at_reopen=-160.0,
                reason="test_witness",
            )
            # Force witness epoch to "now" so prior closes are excluded from fresh
            wit_path = root / "state" / "operator_reopen_witness.json"
            wit = json.loads(wit_path.read_text(encoding="utf-8"))
            wit["reopened_at_epoch"] = now.timestamp()
            wit_path.write_text(json.dumps(wit) + "\n", encoding="utf-8")

            with patch.object(gds, "_port_bundle", side_effect=[cfd, sb]), patch.object(
                gds, "_tcp_reachable", return_value=True
            ), patch.object(
                gds,
                "_read_dual_core_posture",
                return_value={
                    "exclude_from_hot_path": ["IX.D.NIKKEI.IFM.IP"],
                    "ranked_candidate_epics": ["IX.D.DOW.IFM.IP"],
                    "sb_hot_path_allowlist": ["IX.D.DOW.IFM.IP"],
                    "ranked_rotator_mode": True,
                    "sb_macro_ltr_entries_only": True,
                    "sb_path_a_carve_expected": True,
                    "sources": [],
                },
            ), patch.object(
                gds,
                "_cheap_log_tick_smell",
                return_value={"ok": True, "dormant_hits": 0, "entering_tick_hits": 1, "bytes_read": 10},
            ):
                payload = gds.assess(data_root=root)
        self.assertFalse(payload.get("ensure_bleed_halt"))
        self.assertFalse(payload.get("halted"))
        # Prior damage still visible, but not auto-lock
        self.assertTrue(
            any("BLEED" in a or "SESSION_KILL" in a for a in (payload.get("alerts") or []))
            or any("BLEED" in f["title"] or "SESSION_KILL" in f["title"] for f in payload["findings"])
        )


if __name__ == "__main__":
    unittest.main()
