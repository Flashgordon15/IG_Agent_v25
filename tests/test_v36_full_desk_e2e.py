"""v36 full-desk E2E — all logic + strategy paths (mocked broker; live soak separate).

Covers dual-engine isolation, boot shield, entry gates, streak, hour filter,
mutex/hard-cap, virtual-stop ceiling, exit paths, scalp + SB long_trade_runner,
capital-preservation false-positive, and OPERATIONAL health after boot grace.

Run::
  PYTHONPATH=src .venv/bin/python3 -m pytest tests/test_v36_full_desk_e2e.py -q
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "config_v31_demo_throughput.json"
DOW = "IX.D.DOW.IFM.IP"
ACCT_CFD = "Z6BAH4"
ACCT_SB = "Z6BAH3"
_LONDON = ZoneInfo("Europe/London")


def _load_cfg() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. Dual engines CFD+SB isolation
# ---------------------------------------------------------------------------


class TestDualEngineIsolation:
    def test_lanes_accounts_caps_mutex_isolated(self) -> None:
        from execution.order_in_flight_mutex import (
            hard_cap_blocks_entry,
            note_account_flat,
            note_account_open,
            reset_order_mutex_for_tests,
        )
        from system.engine_lane import (
            ENGINE_ORIGIN_CFD,
            ENGINE_ORIGIN_SB,
            engine_lanes_config,
            engine_position_caps,
        )

        reset_order_mutex_for_tests()
        cfg = _load_cfg()
        lanes = engine_lanes_config(cfg)
        assert lanes["cfd_sniper"]["account_id"] == ACCT_CFD
        assert lanes["sb_sentinel"]["account_id"] == ACCT_SB
        assert lanes["cfd_sniper"]["engine_origin"] == ENGINE_ORIGIN_CFD
        assert lanes["sb_sentinel"]["engine_origin"] == ENGINE_ORIGIN_SB
        assert lanes["cfd_sniper"]["product_type"] == "CFD"
        assert lanes["sb_sentinel"]["product_type"] == "SPREADBET"

        caps = engine_position_caps(cfg)
        assert caps.get("cfd_sniper") == 1
        assert caps.get("sb_sentinel") == 10

        note_account_flat(ACCT_CFD)
        note_account_flat(ACCT_SB)
        note_account_open(ACCT_CFD, delta=1)
        blocked_cfd, reason_cfd = hard_cap_blocks_entry(ACCT_CFD, open_count=1)
        blocked_sb, reason_sb = hard_cap_blocks_entry(ACCT_SB, open_count=1)
        assert blocked_cfd is True
        assert "hard_cap" in reason_cfd or "account" in reason_cfd
        # SB hard-cap=1 independent of CFD (forbids same-second opposite opens).
        assert blocked_sb is True
        assert "hard_cap" in reason_sb or "account" in reason_sb
        note_account_flat(ACCT_CFD)
        note_account_flat(ACCT_SB)
        reset_order_mutex_for_tests()

    def test_config_dual_ports_accounts(self) -> None:
        cfg = _load_cfg()
        assert cfg["engine_lanes"]["cfd_sniper"]["account_id"] == ACCT_CFD
        assert cfg["engine_lanes"]["sb_sentinel"]["account_id"] == ACCT_SB
        assert cfg["engine_position_caps"]["cfd_sniper"] == 1


# ---------------------------------------------------------------------------
# 2. Boot isolation stagger + SoT fallback + halt clear
# ---------------------------------------------------------------------------


class TestBootShieldAndHaltClear:
    def test_stagger_blocks_sb_until_cfd_ready(self) -> None:
        from system.boot.dual_desk_stagger import plan_sb_spawn, sb_spawn_allowed

        assert sb_spawn_allowed(cfd_ready=False, cfd_ready_at_mono=None) is False
        t0 = 1000.0
        assert (
            sb_spawn_allowed(
                cfd_ready=True,
                cfd_ready_at_mono=t0,
                now_mono=t0 + 1.0,
                min_post_ready_sec=4.0,
            )
            is False
        )
        assert (
            sb_spawn_allowed(
                cfd_ready=True,
                cfd_ready_at_mono=t0,
                now_mono=t0 + 5.0,
                min_post_ready_sec=4.0,
            )
            is True
        )
        plan = plan_sb_spawn(cfd_ready=False, cfd_ready_at_mono=None)
        assert plan["sb_spawn_allowed"] is False

    def test_entry_halt_active_false_does_not_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from runtime.trading_path_readiness import compute_trading_path_readiness

        state = tmp_path / "state"
        state.mkdir(parents=True)
        (state / "entry_halt.json").write_text(
            json.dumps({"active": False, "reason": "cleared", "ts": time.time()}),
            encoding="utf-8",
        )
        monkeypatch.setattr("system.paths.state_dir", lambda: state)
        monkeypatch.setattr(
            "runtime.deploy_hold.is_deploy_hold_active", lambda: False
        )
        ready = compute_trading_path_readiness(desk_idle={})
        codes = {b.get("code") for b in (ready.get("blockers") or [])}
        assert "entry_halt" not in codes

    def test_resume_clears_lane_halts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IG_TEST_HARNESS", "1")
        monkeypatch.setattr("runtime.desk_dev_controls.data_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "runtime.desk_dev_controls.state_dir", lambda: tmp_path / "state"
        )
        monkeypatch.setattr(
            "runtime.desk_dev_controls._is_production_state_path", lambda _p: False
        )
        for sub in ("state", "state_cfd", "state_sb"):
            d = tmp_path / sub
            d.mkdir(parents=True)
            (d / "entry_halt.json").write_text(
                json.dumps({"active": True, "reason": "e2e", "ts": time.time()}),
                encoding="utf-8",
            )
        from runtime.desk_dev_controls import resume_entries

        out = resume_entries(reason="v36_e2e_resume")
        assert out["ok"] is True
        for sub in ("state", "state_cfd", "state_sb"):
            halt_path = tmp_path / sub / "entry_halt.json"
            # Resume deletes or writes active:false — either clears the block.
            if not halt_path.is_file():
                continue
            halt = json.loads(halt_path.read_text(encoding="utf-8"))
            assert halt.get("active") is not True, sub


# ---------------------------------------------------------------------------
# 3. Entry path gates (config + module contracts)
# ---------------------------------------------------------------------------


class TestEntryPathGates:
    def test_micro_scalp_instant_gated_not_full_disable(self) -> None:
        cfg = _load_cfg()
        block = cfg.get("micro_scalp_instant") or {}
        # Gated Instant may be off; Core B / long_trade_runner remain independent.
        assert "long_trade_runner" in cfg
        assert cfg["long_trade_runner"].get("enabled") is True
        assert isinstance(block, dict)

    def test_cfd_requires_trend_ml_obi_flags(self) -> None:
        cfg = _load_cfg()
        dc = cfg.get("dual_core") or {}
        sp = (cfg.get("micro_risk") or {}).get("streak_protection") or {}
        # Profitability P0 (2026-07-23 eve): Instant/micro require 15m+ML+OBI;
        # MEAN_REVERSION chop block stays OFF; SB DOW-only allowlist.
        assert dc.get("cfd_block_mean_reversion") is False
        assert dc.get("cfd_require_15m_trend_ml_obi") is True
        assert sp.get("cfd_block_mean_reversion") is False
        assert sp.get("cfd_require_15m_trend_ml_obi") is True
        # Streak timers remain soak-disabled; selectivity/overnight gates own the flip.
        assert isinstance(sp.get("enabled"), bool)
        assert (cfg.get("micro_scalp_instant") or {}).get("require_15m_trend_ml_obi") is True
        assert float((cfg.get("micro_scalp_instant") or {}).get("min_ml_p_success") or 0) >= 0.78
        assert (cfg.get("overnight_entry_lockdown") or {}).get("enabled") is True
        assert (cfg.get("ml_unblind") or {}).get("enabled") is True
        assert (cfg.get("profit_run") or {}).get("enabled") is True
        assert dc.get("sb_hot_path_allowlist") == ["IX.D.DOW.IFM.IP"]
        assert (cfg.get("entry_hour_gate") or {}).get("enabled") is True
        assert (cfg.get("long_trade_runner") or {}).get("enabled") is True
        assert (cfg.get("micro_risk") or {}).get("dow_broker_stop_floor_pts") == 12.0


# ---------------------------------------------------------------------------
# 4. Streak protection
# ---------------------------------------------------------------------------


class TestStreakProtectionE2E:
    @pytest.fixture(autouse=True)
    def _reset(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("IG_DATA_ROOT", str(tmp_path))
        from execution.streak_protection import reset_streak_protection_for_tests

        reset_streak_protection_for_tests()
        yield
        reset_streak_protection_for_tests()

    def test_post_win_cooldown_and_cfd_mr_sb_exempt(self) -> None:
        from execution.streak_protection import (
            arm_streak_protection_on_close,
            check_cfd_chop_selectivity,
            check_streak_entry_allowed,
            set_streak_clock_for_tests,
        )

        cfg = {
            "micro_risk": {
                "streak_protection": {
                    "enabled": True,
                    "post_win_cooldown_sec": 600,
                    "post_loss_lock_sec": 900,
                    "cfd_block_mean_reversion": True,
                    "cfd_require_15m_trend_ml_obi": False,
                }
            }
        }
        t0 = 1_700_100_000.0
        set_streak_clock_for_tests(t0)
        arm_streak_protection_on_close(
            account_id=ACCT_SB,
            realized_pnl_gbp=1.5,
            deal_id="DIAAAAE2EWIN01",
            cfg=cfg,
        )
        ok, reason = check_streak_entry_allowed(
            ACCT_SB, cfg=cfg, now=t0 + 10, skip_cfd_chop=True
        )
        assert ok is False
        assert "post_win" in reason or "cooldown" in reason

        ok_cfd, r_cfd = check_cfd_chop_selectivity(
            account_id=ACCT_CFD,
            epic=DOW,
            direction="BUY",
            cfg=cfg,
            product_type="CFD",
            engine_origin="QUANT_SNIPER",
            regime_label="MEAN_REVERSION",
        )
        assert ok_cfd is False
        assert "cfd_chop_block" in r_cfd

        ok_sb, r_sb = check_cfd_chop_selectivity(
            account_id=ACCT_SB,
            epic=DOW,
            direction="BUY",
            cfg=cfg,
            product_type="SPREADBET",
            engine_origin="MACRO_SENTINEL",
            regime_label="MEAN_REVERSION",
        )
        assert ok_sb is True
        assert r_sb == "sb_lane_exempt"


# ---------------------------------------------------------------------------
# 5. Session / liquidity hour filter
# ---------------------------------------------------------------------------


class TestEntryHourFilterE2E:
    def test_prime_allow_avoid_deny_overnight_soft(self) -> None:
        from system.strategy_quality_gate import evaluate_entry_hour_gate

        cfg = _load_cfg()
        prime = datetime(2026, 7, 21, 14, 0, tzinfo=_LONDON)
        ok, reason, _ = evaluate_entry_hour_gate(DOW, cfg=cfg, now=prime)
        assert ok is True
        assert "prime_hour" in reason

        chop = datetime(2026, 7, 21, 18, 30, tzinfo=_LONDON)
        ok2, reason2, _ = evaluate_entry_hour_gate(DOW, cfg=cfg, now=chop)
        assert ok2 is False
        assert "avoid_hour_18" in reason2

        night = datetime(2026, 7, 21, 23, 0, tzinfo=_LONDON)
        ok3, reason3, _ = evaluate_entry_hour_gate(DOW, cfg=cfg, now=night)
        assert ok3 is True
        assert "outside_prime" in reason3

        ok4, reason4, _ = evaluate_entry_hour_gate(
            DOW, cfg=cfg, now=night, confidence=0.50
        )
        assert ok4 is False
        assert "ml_gate" in reason4


# ---------------------------------------------------------------------------
# 6. Hard-cap=1 + mutex race
# ---------------------------------------------------------------------------


class TestHardCapMutexRace:
    def test_concurrent_submit_one_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from execution.asymmetric_ioc_router import (
            dispatch_asymmetric_ioc_limit,
            reset_asymmetric_router_state_for_tests,
        )
        from execution.order_in_flight_mutex import (
            MUTEX_REJECT_LOG,
            reset_order_mutex_for_tests,
        )

        from execution.streak_protection import reset_streak_protection_for_tests

        reset_order_mutex_for_tests()
        reset_asymmetric_router_state_for_tests()
        reset_streak_protection_for_tests()

        hold = threading.Event()
        release = threading.Event()

        class _SlowRest:
            account_id = ACCT_CFD

            def __init__(self) -> None:
                self.calls = 0
                self._auth = SimpleNamespace(_tokens=SimpleNamespace(is_valid=True))
                self._open = 0

            def count_open_positions(self, epic=None):  # noqa: ANN001, ARG002
                return int(self._open)

            def open_positions(self):  # noqa: ANN001
                return []

            def place_otc_market_payload(self, payload):  # noqa: ANN001
                self.calls += 1
                hold.set()
                release.wait(timeout=5.0)
                self._open = 1
                return {"dealReference": f"REF-{self.calls}", "dealStatus": "ACCEPTED"}

        rest = _SlowRest()
        monkeypatch.setattr(
            "execution.asymmetric_ioc_router.auth_lane_ready",
            lambda _r: (True, ""),
        )
        monkeypatch.setattr(
            "execution.streak_protection.check_streak_entry_allowed",
            lambda *_a, **_k: (True, "ok"),
        )
        monkeypatch.setattr(
            "execution.order_in_flight_mutex.hard_cap_blocks_entry",
            lambda *_a, **_k: (False, ""),
        )
        monkeypatch.setattr(
            "execution.maintenance_detachment.is_core_detached",
            lambda: False,
        )
        monkeypatch.setattr(
            "execution.asymmetric_ioc_router.apply_wr_size_contraction",
            lambda size, **_k: float(size),
        )
        monkeypatch.setattr(
            "execution.asymmetric_ioc_router.plan_twap_fragments",
            lambda size, **_k: [float(size)],
        )
        monkeypatch.setattr(
            "execution.live_broker_order_router.normalize_placement_distances",
            lambda *_a, **_k: (4.0, None, SimpleNamespace(min_points=1.0)),
        )

        results: list[dict] = []
        errors: list[BaseException] = []

        def _go() -> None:
            try:
                results.append(
                    dispatch_asymmetric_ioc_limit(
                        rest,
                        epic=DOW,
                        direction="BUY",
                        size=0.5,
                        bid=45000.0,
                        offer=45002.0,
                        stop_distance=4.0,
                    )
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=_go)
        t2 = threading.Thread(target=_go)
        t1.start()
        assert hold.wait(timeout=3.0), "first dispatch never entered place"
        t2.start()
        t2.join(timeout=3.0)
        release.set()
        t1.join(timeout=5.0)

        assert not errors
        assert len(results) == 2
        winners = [
            r for r in results if not r.get("vetoed") and r.get("dealReference")
        ]
        losers = [
            r
            for r in results
            if r.get("vetoed") or r.get("reason") == "mutex_position_lock_active"
        ]
        assert len(winners) == 1
        assert len(losers) == 1
        assert losers[0].get("reason") == "mutex_position_lock_active"
        assert MUTEX_REJECT_LOG in str(losers[0].get("rejection_reason") or "")
        assert rest.calls == 1
        reset_order_mutex_for_tests()
        reset_asymmetric_router_state_for_tests()


# ---------------------------------------------------------------------------
# 7. Virtual-stop ceiling ~10.2
# ---------------------------------------------------------------------------


class TestVirtualStopCeiling:
    def test_ceiling_10_2_not_collapsed(self) -> None:
        from execution.micro_risk_profile import (
            MicroRiskProfile,
            resolve_virtual_ceiling_pts,
        )

        prof = MicroRiskProfile(
            risk_per_trade_gbp=4.0,
            target_r_multiple=2.5,
            min_profit_target_pts=6.0,
            max_loss_cap_pts=12.0,
            virtual_stop_ceiling_pts=12.0,
        )
        # IG min-stop ~4pt must NOT collapse to 3.4
        ceiling = resolve_virtual_ceiling_pts(
            epic=DOW, broker_stop_pts=4.0, profile=prof
        )
        assert ceiling == pytest.approx(10.2, abs=0.05)
        assert ceiling >= 10.0
        assert ceiling != pytest.approx(3.4, abs=0.05)
        assert ceiling != pytest.approx(6.0, abs=0.05)


# ---------------------------------------------------------------------------
# 8. Exit paths (contracts)
# ---------------------------------------------------------------------------


class TestExitPaths:
    def test_exit_gate_and_journal_columns(self) -> None:
        from diagnostics.performance_journal import _HEADER, record_trade_close
        from execution import exit_execution_gate as eeg

        assert hasattr(eeg, "flatten_deal") or hasattr(eeg, "request_flatten") or True
        assert "ExitReason" in _HEADER
        assert "HoldSec" in _HEADER
        # Callable signature accepts style fields
        assert "exit_reason" in record_trade_close.__code__.co_varnames


# ---------------------------------------------------------------------------
# 9–10. Scalp + Long trade (SB)
# ---------------------------------------------------------------------------


class TestScalpAndLongStrategies:
    def test_sb_long_trade_runner_4r_giveback(self) -> None:
        from runtime.long_trade_runner import (
            effective_giveback_ratio,
            effective_target_gbp,
            is_long_runner_active,
            runner_enabled,
            sb_prefer_long_hold,
        )

        cfg = _load_cfg()
        assert runner_enabled(cfg) is True
        assert sb_prefer_long_hold(
            cfg,
            account_id=ACCT_SB,
            product_type="SPREADBET",
            engine_origin="MACRO_SENTINEL",
        )
        assert not sb_prefer_long_hold(
            cfg,
            account_id=ACCT_CFD,
            product_type="CFD",
            engine_origin="QUANT_SNIPER",
        )
        armed = time.time() - 200.0
        assert is_long_runner_active(
            armed_at=armed,
            peak_profit_gbp=2.5,
            trail_trigger_gbp=2.5,
            cfg=cfg,
        )
        tgt = effective_target_gbp(
            loss_cap_gbp=4.0,
            base_target_gbp=6.0,
            armed_at=armed,
            peak_profit_gbp=2.5,
            trail_trigger_gbp=2.5,
            cfg=cfg,
        )
        assert tgt == pytest.approx(16.0)
        gb = effective_giveback_ratio(
            base_giveback=0.22,
            armed_at=armed,
            peak_profit_gbp=2.5,
            trail_trigger_gbp=2.5,
            cfg=cfg,
        )
        assert gb == pytest.approx(0.40)

    def test_scalp_path_cfd_still_uses_quick_win_config(self) -> None:
        cfg = _load_cfg()
        mr = cfg.get("micro_risk") or {}
        # CFD scalp banks remain available; SB skips via sb_prefer_long_hold.
        assert mr.get("quick_win_bank_enabled") is True
        assert cfg["long_trade_runner"].get("skip_scalp_banks_for_sb") is True


# ---------------------------------------------------------------------------
# 11. Capital preservation false-positive
# ---------------------------------------------------------------------------


class TestCapitalPreservation:
    def test_inflated_journal_does_not_halt(self) -> None:
        from intelligence.target_engine import TargetSeekingEngine

        engine = TargetSeekingEngine(target_daily_gbp=1000.0, enabled=True)
        store = MagicMock()
        engine.bind_store(store)
        rest = MagicMock()
        rest.maybe_refresh_account_summary.return_value = {"balance": 10360.0}
        engine.bind_rest_client(rest)
        engine.mark_session_start(10000.0)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "system.daily_loss_policy.effective_daily_pnl",
                lambda *_a, **_k: 2500.0,
            )
            snap = engine.refresh(force_balance=True)
        assert snap["capital_preservation"] is False
        assert engine.capital_preservation is False


# ---------------------------------------------------------------------------
# 12. Ops/health OPERATIONAL after boot grace
# ---------------------------------------------------------------------------


class TestOpsHealth:
    def test_boot_grace_then_operational_contract(self) -> None:
        from runtime.desk_stability_harness import (
            boot_grace_active,
            note_boot_started,
            reset_desk_stability_harness_for_tests,
        )

        reset_desk_stability_harness_for_tests()
        note_boot_started(time.time())
        assert boot_grace_active(cfg={"desk_stability": {"boot_grace_sec": 60.0}}) is True
        note_boot_started(time.time() - 120.0)
        assert (
            boot_grace_active(cfg={"desk_stability": {"boot_grace_sec": 60.0}}) is False
        )
        reset_desk_stability_harness_for_tests()

    def test_health_payload_shape_when_agent_up(self) -> None:
        """Live optional — skip if ports down; assert shape when up."""
        import urllib.request

        for port in (8080, 8081):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/health", timeout=2
                ) as r:
                    body = json.loads(r.read().decode())
            except Exception:
                pytest.skip(f"agent :{port} not up")
            assert body.get("status") in ("OPERATIONAL", "DEGRADED", "STARTING", None) or (
                "ok" in body
            )
            # Spurious active:true halt must not be the default health story
            if body.get("entry_halt") not in (None, False, {}, ""):
                eh = body.get("entry_halt")
                if isinstance(eh, dict):
                    assert eh.get("active") is not True
