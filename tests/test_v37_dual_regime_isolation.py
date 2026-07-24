"""V37 dual-regime isolation — CFD scalp must not clobber SB macro; exit matrices."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from diagnostics.ml_trade_outcomes import reset_ml_trade_outcomes_for_tests
from diagnostics.performance_journal import (
    enable_sync_mode_for_tests,
    record_trade_close,
    reset_performance_journal_for_tests,
)
from execution.live_broker_order_router import desk_entry_stop_floor_pts
from runtime.long_trade_runner import effective_giveback_ratio, sb_prefer_long_hold
from runtime.overnight_entry_policy import (
    elastic_gate_enabled,
    evaluate_elastic_gate,
    evaluate_selectivity_gates,
)
from runtime.profit_run_policy import evaluate_profit_run
from system.dual_regime import (
    MACRO_SENTINEL,
    QUANT_SNIPER,
    allow_obi_velocity_scalp_trigger,
    apply_macro_sentiment,
    apply_ml_overrides_for_engine,
    elastic_gate_applies,
    evaluate_exit_matrix,
    get_macro_sentiment,
    get_ml_overrides_for_engine,
    get_sniper_gate_state,
    reset_dual_regime_for_tests,
    simulate_cfd_scalp_fill_sequence,
)

DOW = "IX.D.DOW.IFM.IP"

_CFG = {
    "profit_run": {
        "enabled": True,
        "upl_threshold_gbp": 15.0,
        "breakeven_offset_pts": 1.0,
    },
    "micro_risk": {
        "dow_broker_stop_floor_pts": 12.0,
        "virtual_stop_ceiling_pts": 12.0,
        "max_loss_cap_pts": 12.0,
    },
    "long_trade_runner": {
        "enabled": True,
        "min_age_minutes": 3,
        "widened_giveback_ratio": 0.40,
        "trend_retention_giveback_ratio": 0.20,
        "sb_prefer_long_hold": True,
        "skip_scalp_banks_for_sb": True,
        "sb_accounts": ["Z6BAH3"],
        "sb_engine_origins": ["MACRO_SENTINEL"],
    },
    "selectivity_gates": {
        "min_ml_p_success": 0.78,
        "min_abs_obi": 0.25,
        "require_15m_trend_ml_obi": True,
        "allow_non_dow": False,
        "elastic_gate_enabled": True,
    },
    "elastic_gate": {
        "enabled": True,
        "healthy_p_lo": 0.68,
        "healthy_p_hi": 0.72,
        "stressed_p_lo": 0.78,
        "stressed_p_hi": 0.82,
    },
    "dual_regime": {
        "enabled": True,
        "elastic_gate_owner": "QUANT_SNIPER",
        "sb_forbid_obi_velocity_scalp": True,
        "trend_retention": {
            "upl_threshold_gbp": 15.0,
            "breakeven_offset_pts": 1.0,
            "giveback_ratio": 0.20,
        },
    },
}


@pytest.fixture(autouse=True)
def _reset_stores() -> None:
    reset_dual_regime_for_tests()
    reset_performance_journal_for_tests()
    enable_sync_mode_for_tests(True)
    reset_ml_trade_outcomes_for_tests()
    yield
    reset_dual_regime_for_tests()
    reset_performance_journal_for_tests()
    reset_ml_trade_outcomes_for_tests()


# ---------------------------------------------------------------------------
# 1) CFD scalp cannot overwrite SB macro sentiment / ML overrides
# ---------------------------------------------------------------------------


def test_cfd_scalp_fill_does_not_overwrite_sb_macro_sentiment() -> None:
    apply_macro_sentiment(
        engine_origin=MACRO_SENTINEL,
        epic=DOW,
        sentiment={"bias": "BEARISH", "mtf_align": True, "score": 0.81},
    )
    apply_ml_overrides_for_engine(
        MACRO_SENTINEL, {"macro_hold_minutes": 45.0, "passive_limit": True}
    )

    simulate_cfd_scalp_fill_sequence(
        epic=DOW,
        ml_overrides={"micro_z_threshold": 0.05, "micro_tp_points": 4.0},
        gate_payload={"last_fill": "scalp", "obi_velocity": 0.55},
    )

    sb_sent = get_macro_sentiment(engine_origin=MACRO_SENTINEL, epic=DOW)
    assert sb_sent["bias"] == "BEARISH"
    assert sb_sent["score"] == pytest.approx(0.81)

    sb_ml = get_ml_overrides_for_engine(MACRO_SENTINEL)
    assert sb_ml.get("macro_hold_minutes") == 45.0
    assert sb_ml.get("passive_limit") is True
    assert "micro_z_threshold" not in sb_ml

    cfd_ml = get_ml_overrides_for_engine(QUANT_SNIPER)
    assert cfd_ml.get("micro_z_threshold") == pytest.approx(0.05)
    cfd_gate = get_sniper_gate_state(engine_origin=QUANT_SNIPER, epic=DOW)
    assert cfd_gate.get("last_fill") == "scalp"

    # SB sniper-gate lane stays empty — scalp state is CFD-scoped.
    assert get_sniper_gate_state(engine_origin=MACRO_SENTINEL, epic=DOW) == {}


def test_sb_cannot_fire_obi_velocity_scalp_when_forbidden() -> None:
    assert allow_obi_velocity_scalp_trigger(
        engine_origin=QUANT_SNIPER, cfg=_CFG
    )
    assert not allow_obi_velocity_scalp_trigger(
        engine_origin=MACRO_SENTINEL, cfg=_CFG
    )


def test_elastic_gate_is_cfd_owned_knob() -> None:
    assert elastic_gate_applies(engine_origin=QUANT_SNIPER, cfg=_CFG)
    assert not elastic_gate_applies(engine_origin=MACRO_SENTINEL, cfg=_CFG)
    assert elastic_gate_enabled(_CFG) is True

    # SB selectivity must not require ElasticGate HF OBI path.
    sb = evaluate_selectivity_gates(
        epic=DOW,
        direction="BUY",
        p_success=0.90,
        obi=None,
        obi_available=False,
        trend_15m="BULLISH",
        cfg=_CFG,
        force_require=True,
        engine_origin=MACRO_SENTINEL,
    )
    assert sb.allow is True
    assert "sb_macro_skips_cfd_elastic_gate" in sb.reason

    # CFD still fail-closed on missing OBI via ElasticGate.
    cfd = evaluate_elastic_gate(
        epic=DOW,
        direction="BUY",
        p_success=0.90,
        obi=None,
        obi_available=False,
        trend_15m="BULLISH",
        cfg=_CFG,
        force_require=True,
    )
    assert cfd.allow is False
    assert "obi_unavailable" in cfd.reason


# ---------------------------------------------------------------------------
# 2) Journal close stamps AccountID + EngineOrigin + ml + regime + hold
# ---------------------------------------------------------------------------


def test_journal_close_stamps_account_engine_ml_regime_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = tmp_path / "daily_journal.csv"
    outcomes = tmp_path / "ml_trade_outcomes.jsonl"
    monkeypatch.setattr(
        "diagnostics.performance_journal.journal_path", lambda: journal
    )
    monkeypatch.setattr(
        "diagnostics.ml_trade_outcomes.outcomes_path", lambda: outcomes
    )

    record_trade_close(
        deal_id="DIAAAAXV37CFD01",
        direction="SELL",
        realized_pnl_gbp=-2.5,
        account_id="Z6BAH4",
        engine_origin=QUANT_SNIPER,
        exit_reason="micro_gbp_exit",
        hold_sec=33.0,
        epic=DOW,
        ml_score=0.712,
        regime="CHOP",
    )
    record_trade_close(
        deal_id="DIAAAAXV37SB001",
        direction="BUY",
        realized_pnl_gbp=8.4,
        account_id="Z6BAH3",
        engine_origin=MACRO_SENTINEL,
        exit_reason="long_trade_runner",
        hold_sec=420.0,
        epic=DOW,
        ml_score=0.655,
        regime="TRENDING",
    )

    rows = {r["DealID"]: r for r in csv.DictReader(journal.open(encoding="utf-8"))}
    cfd = rows["DIAAAAXV37CFD01"]
    sb = rows["DIAAAAXV37SB001"]

    assert cfd["AccountID"] == "Z6BAH4"
    assert cfd["EngineOrigin"] == QUANT_SNIPER
    assert float(cfd["MlScoreAtEntry"]) == pytest.approx(0.712)
    assert cfd["MarketRegime"] == "CHOP"
    assert float(cfd["HoldSec"]) == pytest.approx(33.0)

    assert sb["AccountID"] == "Z6BAH3"
    assert sb["EngineOrigin"] == MACRO_SENTINEL
    assert float(sb["MlScoreAtEntry"]) == pytest.approx(0.655)
    assert sb["MarketRegime"] == "TRENDING"
    assert float(sb["HoldSec"]) == pytest.approx(420.0)

    lines = [json.loads(x) for x in outcomes.read_text().splitlines() if x]
    by_deal = {str(x.get("deal_id")): x for x in lines}
    assert by_deal["DIAAAAXV37CFD01"]["ml_score_at_entry"] == pytest.approx(0.712)
    assert by_deal["DIAAAAXV37CFD01"]["market_regime"] == "CHOP"
    assert by_deal["DIAAAAXV37CFD01"]["hold_duration_seconds"] == pytest.approx(33.0)
    assert by_deal["DIAAAAXV37CFD01"]["engine_origin"] == QUANT_SNIPER
    assert by_deal["DIAAAAXV37SB001"]["engine_origin"] == MACRO_SENTINEL
    assert by_deal["DIAAAAXV37SB001"]["hold_duration_seconds"] == pytest.approx(420.0)


# ---------------------------------------------------------------------------
# 3) CFD vs SB exit matrices
# ---------------------------------------------------------------------------


def test_cfd_exit_matrix_keeps_12pt_scalp_floor() -> None:
    d = evaluate_exit_matrix(
        engine_origin=QUANT_SNIPER,
        unrealized_pnl_gbp=20.0,
        cfg=_CFG,
    )
    assert d.mode == "cfd_scalp"
    assert d.stop_floor_pts is not None and d.stop_floor_pts >= 12.0
    assert d.skip_micro_trails is False
    assert d.floor_breakeven_plus is False
    assert desk_entry_stop_floor_pts(DOW, cfg=_CFG) >= 12.0


def test_sb_trend_retention_be_plus_and_20pct_giveback() -> None:
    below = evaluate_exit_matrix(
        engine_origin=MACRO_SENTINEL,
        unrealized_pnl_gbp=10.0,
        cfg=_CFG,
    )
    assert below.mode == "neutral"
    assert below.skip_micro_trails is False

    armed = evaluate_exit_matrix(
        engine_origin=MACRO_SENTINEL,
        unrealized_pnl_gbp=15.0,
        cfg=_CFG,
    )
    assert armed.mode == "sb_trend_retention"
    assert armed.skip_micro_trails is True
    assert armed.floor_breakeven_plus is True
    assert armed.breakeven_offset_pts == pytest.approx(1.0)
    assert armed.giveback_ratio == pytest.approx(0.20)

    pr = evaluate_profit_run(unrealized_pnl_gbp=18.0, cfg=_CFG)
    assert pr.active is True
    assert pr.skip_hyper_trail is True
    assert pr.floor_to_breakeven_plus is True

    # LTR giveback uses Trend-Retention ~20% for SB when UPL armed (not 40% chop).
    gb = effective_giveback_ratio(
        base_giveback=0.22,
        armed_at=0.0,  # force inactive age path — dual_regime helper still owns matrix
        peak_profit_gbp=20.0,
        trail_trigger_gbp=2.5,
        cfg=_CFG,
        engine_origin=MACRO_SENTINEL,
        unrealized_pnl_gbp=18.0,
    )
    assert gb == pytest.approx(0.20)

    assert sb_prefer_long_hold(
        _CFG, account_id="Z6BAH3", engine_origin=MACRO_SENTINEL
    )
    assert not sb_prefer_long_hold(
        _CFG, account_id="Z6BAH4", engine_origin=QUANT_SNIPER
    )


# ---------------------------------------------------------------------------
# 4) SB Instant / Core-B micro hard veto (daytime) — CFD micro still exists
# ---------------------------------------------------------------------------


def test_sb_instant_and_core_b_micro_hard_disabled() -> None:
    from runtime.overnight_entry_policy import evaluate_engine_entry_path_policy
    from system.dual_regime import (
        allow_engine_micro_scalp_path,
        sb_disable_core_b_micro,
        sb_disable_instant_micro,
        sb_macro_ltr_entries_only,
    )

    cfg = {
        **_CFG,
        "dual_regime": {
            **_CFG["dual_regime"],
            "sb_disable_instant_micro": True,
            "sb_disable_core_b_micro": True,
            "sb_macro_ltr_entries_only": True,
        },
        "overnight_entry_lockdown": {
            "enabled": True,
            "start": "21:00",
            "end": "07:00",
            "timezone": "Europe/London",
        },
    }
    assert sb_disable_instant_micro(cfg) is True
    assert sb_disable_core_b_micro(cfg) is True
    assert sb_macro_ltr_entries_only(cfg) is True

    ok_i, reason_i = allow_engine_micro_scalp_path(
        "instant", engine_origin=MACRO_SENTINEL, cfg=cfg
    )
    assert ok_i is False
    assert "sb_instant" in reason_i or "macro_ltr" in reason_i

    ok_b, reason_b = allow_engine_micro_scalp_path(
        "engine_b_micro_scalper", engine_origin=MACRO_SENTINEL, cfg=cfg
    )
    assert ok_b is False
    assert "sb_core_b" in reason_b or "macro_ltr" in reason_b

    ok_ltr, reason_ltr = allow_engine_micro_scalp_path(
        "long_trade_runner", engine_origin=MACRO_SENTINEL, cfg=cfg
    )
    assert ok_ltr is True
    assert "ltr" in reason_ltr or "macro" in reason_ltr

    # Daytime (outside overnight window) still vetoes SB Instant.
    from datetime import datetime
    from zoneinfo import ZoneInfo

    noon = datetime(2026, 7, 24, 12, 0, tzinfo=ZoneInfo("Europe/London"))
    ed = evaluate_engine_entry_path_policy(
        epic=DOW,
        path="instant",
        account_id="Z6BAH3",
        engine_origin=MACRO_SENTINEL,
        cfg=cfg,
        now=noon,
    )
    assert ed.allow is False
    assert "sb_" in ed.reason


def test_cfd_instant_micro_still_allowed_when_sb_disabled() -> None:
    from runtime.overnight_entry_policy import evaluate_engine_entry_path_policy
    from system.dual_regime import allow_engine_micro_scalp_path

    cfg = {
        **_CFG,
        "dual_regime": {
            **_CFG["dual_regime"],
            "sb_disable_instant_micro": True,
            "sb_disable_core_b_micro": True,
            "sb_macro_ltr_entries_only": True,
        },
        "overnight_entry_lockdown": {
            "enabled": True,
            "start": "21:00",
            "end": "07:00",
            "timezone": "Europe/London",
        },
    }
    ok, reason = allow_engine_micro_scalp_path(
        "instant", engine_origin=QUANT_SNIPER, cfg=cfg
    )
    assert ok is True
    assert reason == "cfd_micro_path_allowed"

    from datetime import datetime
    from zoneinfo import ZoneInfo

    noon = datetime(2026, 7, 24, 12, 0, tzinfo=ZoneInfo("Europe/London"))
    ed = evaluate_engine_entry_path_policy(
        epic=DOW,
        path="instant",
        account_id="Z6BAH4",
        engine_origin=QUANT_SNIPER,
        cfg=cfg,
        now=noon,
    )
    assert ed.allow is True
    assert ed.engine_origin == QUANT_SNIPER


def test_sb_cfd_micro_path_isolation() -> None:
    """CFD micro path config remains armed; SB veto does not clear shared Instant flag."""
    from system.dual_regime import (
        allow_engine_micro_scalp_path,
        resolve_engine_regime_config,
    )

    cfg = {
        **_CFG,
        "micro_scalp_instant": {"enabled": True},
        "dual_regime": {
            **_CFG["dual_regime"],
            "sb_disable_instant_micro": True,
            "sb_disable_core_b_micro": True,
            "sb_macro_ltr_entries_only": True,
        },
    }
    cfd_view = resolve_engine_regime_config(cfg, engine_origin=QUANT_SNIPER)
    sb_view = resolve_engine_regime_config(cfg, engine_origin=MACRO_SENTINEL)
    assert (cfd_view.get("micro_scalp_instant") or {}).get("enabled") is True
    assert (sb_view.get("micro_scalp_instant") or {}).get("enabled") is True
    assert allow_engine_micro_scalp_path(
        "instant", engine_origin=QUANT_SNIPER, cfg=cfg
    )[0]
    assert not allow_engine_micro_scalp_path(
        "instant", engine_origin=MACRO_SENTINEL, cfg=cfg
    )[0]
    assert not allow_engine_micro_scalp_path(
        "core_b", engine_origin=MACRO_SENTINEL, cfg=cfg
    )[0]
