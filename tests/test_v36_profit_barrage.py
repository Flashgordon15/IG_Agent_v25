"""v36 Profit / selectivity barrage — MUST be green before behaviour flip.

DOW-centric synthetic cases. No live broker.

Run::
  PYTHONPATH=src IG_AGENT_CONFIG=config/config_v31_demo_throughput.json \\
    python3 -m pytest tests/test_v36_profit_barrage.py -q
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

DOW = "IX.D.DOW.IFM.IP"
GOLD = "CS.D.CFPGOLD.CFP.IP"
EURUSD = "CS.D.EURUSD.CFD.IP"
FTSE = "IX.D.FTSE.IFM.IP"

STRICT_CFG = {
    "selectivity_gates": {
        "min_ml_p_success": 0.78,
        "min_abs_obi": 0.25,
        "require_15m_trend_ml_obi": True,
        "allow_non_dow": False,
    },
    "micro_scalp_instant": {
        "enabled": True,
        "require_15m_trend_ml_obi": True,
        "min_ml_p_success": 0.78,
    },
    "dual_core": {
        "sb_hot_path_allowlist": [DOW],
        "cfd_require_15m_trend_ml_obi": True,
        "exclude_from_hot_path": [],
    },
    "profit_run": {
        "enabled": True,
        "upl_threshold_gbp": 15.0,
        "breakeven_offset_pts": 1.0,
    },
    "long_trade_runner": {
        "enabled": True,
        "min_age_minutes": 3,
        "skip_dynamic_limit_until_armed": True,
    },
    "micro_risk": {
        "dow_broker_stop_floor_pts": 12.0,
        "min_hold_before_trail_sec": 150,
    },
}

LOOSE_CFG = {
    "selectivity_gates": {
        "min_ml_p_success": 0.55,
        "min_abs_obi": 0.0,
        "require_15m_trend_ml_obi": False,
        "allow_non_dow": True,
    },
    "micro_scalp_instant": {
        "require_15m_trend_ml_obi": False,
        "min_ml_p_success": 0.55,
    },
}


# ---------------------------------------------------------------------------
# S1 — Selectivity
# ---------------------------------------------------------------------------


class TestS1Selectivity:
    def _candidates(self) -> list[dict]:
        # Synthetic book: mix of weak / strong DOW + non-DOW noise.
        return [
            {"epic": DOW, "direction": "BUY", "p": 0.90, "obi": 0.40, "trend": "BULLISH"},
            {"epic": DOW, "direction": "BUY", "p": 0.80, "obi": 0.30, "trend": "BULLISH"},
            {"epic": DOW, "direction": "SELL", "p": 0.85, "obi": -0.35, "trend": "BEARISH"},
            {"epic": DOW, "direction": "BUY", "p": 0.70, "obi": 0.40, "trend": "BULLISH"},  # p fail
            {"epic": DOW, "direction": "BUY", "p": 0.90, "obi": 0.10, "trend": "BULLISH"},  # obi fail
            {"epic": DOW, "direction": "BUY", "p": 0.90, "obi": 0.40, "trend": "BEARISH"},  # trend fail
            {"epic": DOW, "direction": "BUY", "p": 0.60, "obi": 0.05, "trend": "NEUTRAL"},
            {"epic": GOLD, "direction": "BUY", "p": 0.95, "obi": 0.50, "trend": "BULLISH"},
            {"epic": EURUSD, "direction": "SELL", "p": 0.92, "obi": -0.40, "trend": "BEARISH"},
            {"epic": FTSE, "direction": "BUY", "p": 0.88, "obi": 0.33, "trend": "BULLISH"},
            {"epic": DOW, "direction": "SELL", "p": 0.55, "obi": -0.05, "trend": "NEUTRAL"},
            {"epic": DOW, "direction": "BUY", "p": 0.79, "obi": 0.26, "trend": "BULLISH"},
        ]

    def test_require_15m_trend_ml_obi_collapses_volume(self) -> None:
        from runtime.overnight_entry_policy import evaluate_selectivity_gates

        cands = self._candidates()
        loose_ok = [
            c
            for c in cands
            if evaluate_selectivity_gates(
                epic=c["epic"],
                direction=c["direction"],
                p_success=c["p"],
                obi=c["obi"],
                trend_15m=c["trend"],
                cfg=LOOSE_CFG,
                force_require=False,
            ).allow
        ]
        strict_ok = [
            c
            for c in cands
            if evaluate_selectivity_gates(
                epic=c["epic"],
                direction=c["direction"],
                p_success=c["p"],
                obi=c["obi"],
                trend_15m=c["trend"],
                cfg=STRICT_CFG,
                force_require=True,
            ).allow
        ]
        assert len(loose_ok) > len(strict_ok)
        assert len(strict_ok) <= 4  # volume collapse
        assert all(c["epic"] == DOW for c in strict_ok)
        for c in strict_ok:
            assert c["p"] >= 0.78
            assert abs(c["obi"]) >= 0.25

    def test_gold_eurusd_ftse_do_not_enter(self) -> None:
        from runtime.overnight_entry_policy import evaluate_selectivity_gates
        from runtime.dual_core_execution import epic_allowed_on_hot_path

        for epic in (GOLD, EURUSD, FTSE):
            sel = evaluate_selectivity_gates(
                epic=epic,
                direction="BUY",
                p_success=0.95,
                obi=0.50,
                trend_15m="BULLISH",
                cfg=STRICT_CFG,
                force_require=True,
            )
            assert sel.allow is False
            assert "non_dow" in sel.reason

        # SB hot-path allowlist also rejects non-DOW.
        cfg = {
            "dual_core": {
                "sb_hot_path_allowlist": [DOW],
                "exclude_from_hot_path": [],
            }
        }
        import os

        os.environ["IG_ACCOUNT_ID"] = "Z6BAH3"
        os.environ["IG_ENGINE_ORIGIN"] = "MACRO_SENTINEL"
        try:
            for epic in (GOLD, EURUSD, FTSE):
                assert epic_allowed_on_hot_path(epic, cfg) is False
            assert epic_allowed_on_hot_path(DOW, cfg) is True
        finally:
            os.environ.pop("IG_ACCOUNT_ID", None)
            os.environ.pop("IG_ENGINE_ORIGIN", None)


# ---------------------------------------------------------------------------
# S2 — Profit-running (UPL>=£15)
# ---------------------------------------------------------------------------


class TestS2ProfitRunning:
    def test_hyper_trail_skipped_dynamic_not_hypersensitive(self) -> None:
        from runtime.profit_run_policy import (
            evaluate_profit_run,
            should_skip_dynamic_limit_hyper,
            should_skip_micro_gbp_hyper_trail,
        )

        below = evaluate_profit_run(unrealized_pnl_gbp=10.0, cfg=STRICT_CFG)
        assert below.active is False
        assert below.skip_hyper_trail is False

        active = evaluate_profit_run(unrealized_pnl_gbp=15.0, cfg=STRICT_CFG)
        assert active.active is True
        assert active.skip_hyper_trail is True
        assert active.skip_dynamic_hyper is True
        assert active.keep_hard_stop is True
        assert active.allow_long_runner_hold is True
        assert should_skip_micro_gbp_hyper_trail(
            unrealized_pnl_gbp=18.0, cfg=STRICT_CFG
        )
        assert should_skip_dynamic_limit_hyper(
            unrealized_pnl_gbp=18.0, cfg=STRICT_CFG
        )

    def test_floor_to_breakeven_plus_1pt_keeps_hard_stop(self) -> None:
        from runtime.profit_run_policy import (
            breakeven_plus_stop_level,
            evaluate_profit_run,
        )

        d = evaluate_profit_run(unrealized_pnl_gbp=22.0, cfg=STRICT_CFG)
        assert d.floor_to_breakeven_plus is True
        assert d.breakeven_offset_pts == pytest.approx(1.0)
        assert d.keep_hard_stop is True
        buy_floor = breakeven_plus_stop_level(
            direction="BUY", entry_level=45000.0, offset_pts=1.0
        )
        sell_floor = breakeven_plus_stop_level(
            direction="SELL", entry_level=45000.0, offset_pts=1.0
        )
        assert buy_floor == pytest.approx(45001.0)
        assert sell_floor == pytest.approx(44999.0)
        # Hard broker/virtual floor (12pt) remains independent of BE floor.
        hard_stop_pts = float(STRICT_CFG["micro_risk"]["dow_broker_stop_floor_pts"])
        assert hard_stop_pts >= 12.0

    def test_long_trade_runner_can_continue_hold(self) -> None:
        from runtime.long_trade_runner import (
            effective_giveback_ratio,
            is_long_runner_active,
            skip_max_age_close_for_runner,
        )
        from runtime.profit_run_policy import evaluate_profit_run
        import time

        pr = evaluate_profit_run(unrealized_pnl_gbp=20.0, cfg=STRICT_CFG)
        assert pr.allow_long_runner_hold is True
        armed = time.time() - 200.0
        assert is_long_runner_active(
            armed_at=armed,
            peak_profit_gbp=20.0,
            trail_trigger_gbp=2.5,
            cfg=STRICT_CFG,
        )
        # Widened giveback keeps runner holding rather than scratch-exit.
        gb = effective_giveback_ratio(
            base_giveback=0.22,
            armed_at=armed,
            peak_profit_gbp=20.0,
            trail_trigger_gbp=2.5,
            cfg=STRICT_CFG,
        )
        assert gb >= 0.22
        assert skip_max_age_close_for_runner(
            side="BUY",
            entry=45000.0,
            px=45020.0,
            cfg=STRICT_CFG,
        ) is True

    def test_flag_off_keeps_current_hyper_trail(self) -> None:
        from runtime.profit_run_policy import evaluate_profit_run

        d = evaluate_profit_run(
            unrealized_pnl_gbp=50.0,
            cfg={"profit_run": {"enabled": False}},
        )
        assert d.active is False
        assert d.skip_hyper_trail is False
