"""v36 Nightmare Night overnight regression — MUST be green before behaviour flip.

In-memory / mocked; no live broker. Locks overnight failure modes from
``trading_report_2026-07-24_0800.md``.

Run::
  PYTHONPATH=src IG_AGENT_CONFIG=config/config_v31_demo_throughput.json \\
    python3 -m pytest tests/test_v36_overnight_regression.py -q
"""

from __future__ import annotations

import math
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

DOW = "IX.D.DOW.IFM.IP"
GOLD = "CS.D.CFPGOLD.CFP.IP"
EURUSD = "CS.D.EURUSD.CFD.IP"
FTSE = "IX.D.FTSE.IFM.IP"
ACCT_CFD = "Z6BAH4"
ACCT_SB = "Z6BAH3"
_LONDON = ZoneInfo("Europe/London")

# Flags ON for regression — live config flip is Phase 3.
LOCKDOWN_CFG = {
    "overnight_entry_lockdown": {
        "enabled": True,
        "start": "21:00",
        "end": "07:00",
        "timezone": "Europe/London",
    },
    "ml_unblind": {"enabled": True},
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
    },
    "long_trade_runner": {"enabled": True},
    "micro_risk": {"dow_broker_stop_floor_pts": 12.0},
}


def _at_0200() -> datetime:
    return datetime(2026, 7, 24, 2, 0, tzinfo=_LONDON)


# ---------------------------------------------------------------------------
# T1 — ML attribution (pre-submit)
# ---------------------------------------------------------------------------


class TestT1MlAttributionPreSubmit:
    def test_entry_candidate_invokes_scorer_finite_stamp(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from runtime.overnight_entry_policy import score_entry_candidate_ml

        calls: list[tuple[str, str]] = []

        def _fake_gate(epic, direction, *, cfg=None, quote=None):
            calls.append((epic, direction))
            return True, "sniper_ok", 0.81

        monkeypatch.setattr(
            "execution.entry_gate_hardening.evaluate_sniper_ml_gate",
            _fake_gate,
        )
        res = score_entry_candidate_ml(
            epic=DOW,
            direction="BUY",
            cfg=LOCKDOWN_CFG,
            quote=SimpleNamespace(bid=45000.0, offer=45001.0),
            invoke_scorer=True,
        )
        assert calls, "interim/ML sniper scorer must be invoked on entry-candidate"
        assert res.allow_submit is True
        assert res.p_success is not None
        assert math.isfinite(res.p_success)
        assert 0.0 <= float(res.p_success) <= 1.0
        assert res.ml_score_at_entry == pytest.approx(0.81)
        assert res.ml_score_at_entry is not None

    @pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), "missing", ""])
    def test_null_nan_missing_hard_abort(self, bad) -> None:
        from runtime.overnight_entry_policy import score_entry_candidate_ml

        res = score_entry_candidate_ml(
            epic=DOW,
            direction="BUY",
            cfg=LOCKDOWN_CFG,
            p_success=bad if bad != "missing" else None,
            invoke_scorer=False,
        )
        assert res.allow_submit is False
        assert res.ml_score_at_entry is None
        assert "null" in res.reason or "abort" in res.reason or "fail" in res.reason

    def test_current_behaviour_does_not_abort_when_flag_off(self) -> None:
        from runtime.overnight_entry_policy import score_entry_candidate_ml

        res = score_entry_candidate_ml(
            epic=DOW,
            direction="BUY",
            cfg={"ml_unblind": {"enabled": False}},
            p_success=None,
            invoke_scorer=False,
        )
        assert res.allow_submit is True
        assert res.ml_score_at_entry is None


# ---------------------------------------------------------------------------
# T2 — Overnight lockdown 02:00 Europe/London
# ---------------------------------------------------------------------------


class TestT2OvernightLockdown0200:
    def test_cfd_all_new_entries_blocked(self) -> None:
        from runtime.overnight_entry_policy import evaluate_overnight_entry_policy

        for path in ("instant", "micro", "long_trade_runner", "core_b"):
            d = evaluate_overnight_entry_policy(
                epic=DOW,
                path=path,
                account_id=ACCT_CFD,
                cfg=LOCKDOWN_CFG,
                now=_at_0200(),
                long_runner_gates_ok=True,
            )
            assert d.allow is False, path
            assert "cfd" in d.reason
            assert d.in_window is True

    def test_sb_instant_micro_rejected(self) -> None:
        from runtime.overnight_entry_policy import evaluate_overnight_entry_policy

        for path in ("instant", "micro"):
            d = evaluate_overnight_entry_policy(
                epic=DOW,
                path=path,
                account_id=ACCT_SB,
                cfg=LOCKDOWN_CFG,
                now=_at_0200(),
            )
            assert d.allow is False, path
            assert "instant_micro" in d.reason or "rejected" in d.reason

    def test_sb_long_runner_only_when_gates_ok(self) -> None:
        from runtime.overnight_entry_policy import evaluate_overnight_entry_policy

        ok = evaluate_overnight_entry_policy(
            epic=DOW,
            path="long_trade_runner",
            account_id=ACCT_SB,
            cfg=LOCKDOWN_CFG,
            now=_at_0200(),
            long_runner_gates_ok=True,
        )
        assert ok.allow is True
        assert "long_runner_ok" in ok.reason

        bad = evaluate_overnight_entry_policy(
            epic=DOW,
            path="long_trade_runner",
            account_id=ACCT_SB,
            cfg=LOCKDOWN_CFG,
            now=_at_0200(),
            long_runner_gates_ok=False,
        )
        assert bad.allow is False

    def test_sb_non_dow_rejected(self) -> None:
        from runtime.overnight_entry_policy import evaluate_overnight_entry_policy

        for epic in (GOLD, EURUSD, FTSE):
            d = evaluate_overnight_entry_policy(
                epic=epic,
                path="long_trade_runner",
                account_id=ACCT_SB,
                cfg=LOCKDOWN_CFG,
                now=_at_0200(),
                long_runner_gates_ok=True,
            )
            assert d.allow is False, epic
            assert "non_dow" in d.reason

    def test_management_exits_not_gated_by_policy_helper(self) -> None:
        """Policy only covers NEW entries — exits call different modules."""
        from runtime.long_trade_runner import is_long_runner_active
        from runtime.profit_run_policy import evaluate_profit_run

        # Exits / management remain callable at 02:00.
        assert is_long_runner_active(
            armed_at=1.0,
            peak_profit_gbp=5.0,
            trail_trigger_gbp=2.5,
            cfg=LOCKDOWN_CFG,
        ) in (True, False)
        pr = evaluate_profit_run(unrealized_pnl_gbp=20.0, cfg={"profit_run": {"enabled": True}})
        assert pr.keep_hard_stop is True

    def test_flag_off_allows_overnight_instant(self) -> None:
        from runtime.overnight_entry_policy import evaluate_overnight_entry_policy

        d = evaluate_overnight_entry_policy(
            epic=DOW,
            path="instant",
            account_id=ACCT_CFD,
            cfg={"overnight_entry_lockdown": {"enabled": False}},
            now=_at_0200(),
        )
        assert d.allow is True


# ---------------------------------------------------------------------------
# T3 — HTTP 429 flood
# ---------------------------------------------------------------------------


class TestT3Http429Flood:
    def test_finnhub_429_burst_backoff_ge_10s(self) -> None:
        from system.feeds.multi_feed_hub import compute_feed_reject_backoff

        class _RateLimit(Exception):
            status_code = 429

        wait = 0.0
        nxt = 2.0
        for _ in range(55):
            wait, nxt, rejected = compute_feed_reject_backoff(_RateLimit("HTTP 429"), nxt)
            assert rejected is True
            assert wait >= 10.0
        assert wait >= 10.0

    def test_429_no_flatten_and_no_micro_gbp_panic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from runtime import feed_health_watchdog as fhw

        flattened: list[str] = []
        micro_calls: list[str] = []

        monkeypatch.setattr(fhw, "_resolve_quote_age_sec", lambda: 1.0)
        monkeypatch.setattr(fhw, "_mark_unhealthy", lambda *_a, **_k: None)
        monkeypatch.setattr(fhw, "_hard_reset_streams", lambda *_a, **_k: None)
        monkeypatch.setattr(
            fhw,
            "_catastrophic_flatten",
            lambda reason: flattened.append(str(reason)),
        )

        # micro_gbp must not be invoked by 429 alone.
        import runtime.micro_gbp_exit as mge

        monkeypatch.setattr(
            mge,
            "on_watchdog_tick",
            lambda: micro_calls.append("tick"),
        )

        class _RateLimit(Exception):
            status_code = 429

        for _ in range(50):
            fhw.note_api_error(_RateLimit("HTTP 429 Too Many Requests"), flatten=True)
        assert flattened == []
        assert micro_calls == []

    def test_safe_degrade_primary_rest_poll_unaffected(self) -> None:
        from system.feeds.multi_feed_hub import compute_feed_reject_backoff

        wait, nxt, rejected = compute_feed_reject_backoff(
            "HTTP 429 finnhub reconnect", 2.0
        )
        assert rejected is True
        assert wait >= 10.0
        # Non-reject path stays short (IG/Yahoo primary unaffected).
        w2, _n2, rej2 = compute_feed_reject_backoff("transient disconnect", 2.0)
        assert rej2 is False
        assert w2 < 10.0 or w2 == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# T4 — Residuals
# ---------------------------------------------------------------------------


class TestT4Residuals:
    def test_dow_broker_stop_floor_12_never_6_or_4(self) -> None:
        from execution.live_broker_order_router import (
            desk_entry_stop_floor_pts,
            floor_stop_distance_points,
            normalize_placement_distances,
        )

        class _FakeRest:
            account_id = ACCT_SB

            def fetch_market_constraints(self, epic: str, **_k):  # noqa: ANN001
                return {"min_stop_distance": 6.0}

        assert desk_entry_stop_floor_pts(DOW) >= 12.0
        res = floor_stop_distance_points(_FakeRest(), DOW, 6.0)
        assert res.effective_points >= 12.0
        assert res.effective_points not in (4.0, 6.0)
        stop, _limit, _ = normalize_placement_distances(
            _FakeRest(), DOW, stop_distance=4.0, limit_distance=6.0
        )
        assert stop >= 12.0
        assert stop not in (4.0, 6.0)

    def test_hard_cap_1_and_in_flight_reject(self) -> None:
        from execution.order_in_flight_mutex import (
            hard_cap_blocks_entry,
            note_account_flat,
            note_account_open,
            reset_order_mutex_for_tests,
            resolve_account_hard_open_cap,
        )

        reset_order_mutex_for_tests()
        assert resolve_account_hard_open_cap(ACCT_SB) == 1
        note_account_flat(ACCT_SB)
        blocked0, _ = hard_cap_blocks_entry(ACCT_SB, open_count=0)
        assert blocked0 is False
        note_account_open(ACCT_SB, delta=1)
        blocked1, reason = hard_cap_blocks_entry(ACCT_SB, open_count=1)
        assert blocked1 is True
        assert "hard_cap" in reason
        # Opposite / second entry while open≥1 rejected.
        blocked_opp, _ = hard_cap_blocks_entry(ACCT_SB, open_count=1)
        assert blocked_opp is True
        note_account_flat(ACCT_SB)
        reset_order_mutex_for_tests()

    def test_halt_sot_active_false_or_missing_does_not_block(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from runtime.halt_sot import any_entry_halt_active, flag_file_active

        monkeypatch.setattr("runtime.halt_sot.data_dir", lambda: tmp_path, raising=False)
        monkeypatch.setattr(
            "runtime.halt_sot._lane_state_roots",
            lambda: [tmp_path],
            raising=False,
        )
        # Missing file.
        assert flag_file_active(tmp_path / "entry_halt.json") is False
        assert any_entry_halt_active() is False
        # active:false
        (tmp_path / "entry_halt.json").write_text(
            '{"active": false, "reason": "cleared"}\n', encoding="utf-8"
        )
        assert flag_file_active(tmp_path / "entry_halt.json") is False
        assert any_entry_halt_active() is False
