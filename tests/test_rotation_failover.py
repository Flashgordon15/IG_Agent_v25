"""Ranked multi-market rotator + legacy DOW-stale failover."""

from __future__ import annotations

import pytest

DOW = "IX.D.DOW.IFM.IP"
GOLD = "CS.D.CFPGOLD.CFP.IP"
EURUSD = "CS.D.EURUSD.CFD.IP"
FTSE = "IX.D.FTSE.IFM.IP"
NIKKEI = "IX.D.NIKKEI.IFM.IP"
ACCT_SB = "Z6BAH3"


@pytest.fixture(autouse=True)
def _reset_failover():
    from runtime.rotation_failover import reset_rotation_failover_for_tests

    reset_rotation_failover_for_tests()
    yield
    reset_rotation_failover_for_tests()


def _cfg(
    *,
    enabled: bool = True,
    ranked: bool = True,
    stale: float = 8.0,
    recover: float = 3.0,
    top_n: int = 2,
    candidates: list[str] | None = None,
    failover: list[str] | None = None,
    exclude: list[str] | None = None,
):
    return {
        "dual_regime": {
            "enabled": True,
            "sb_disable_instant_micro": True,
            "sb_disable_core_b_micro": True,
            "sb_macro_ltr_entries_only": True,
        },
        "dual_core": {
            "sb_hot_path_allowlist": [DOW],
            "exclude_from_hot_path": exclude
            if exclude is not None
            else [NIKKEI, "CS.D.GBPUSD.CFD.IP", "IX.D.DAX.IFM.IP", "CS.D.CRUDE.CFD.IP"],
            "rotation_failover_enabled": enabled,
            "ranked_rotator_mode": ranked,
            "ranked_promote_top_n": top_n,
            "ranked_rerank_min_sec": 0.0,
            "ranked_use_journal_expectancy": False,
            "ranked_candidate_epics": candidates
            or [DOW, GOLD, EURUSD, FTSE],
            "rotation_failover_stale_minutes": stale,
            "rotation_failover_recover_minutes": recover,
            "rotation_failover_confidence_floor": 0.68,
            "failover_epics": failover or [GOLD, EURUSD, FTSE],
        },
        "selectivity_gates": {
            "allow_non_dow": False,
            "min_ml_p_success": 0.78,
            "min_abs_obi": 0.25,
            "require_15m_trend_ml_obi": True,
        },
        "elastic_gate": {"enabled": False},
        "long_trade_runner": {"enabled": True},
    }


def test_disabled_never_promotes():
    from runtime.rotation_failover import (
        effective_sb_allowlist,
        failover_allows_epic,
        note_dow_tradeability,
    )

    cfg = _cfg(enabled=False)
    note_dow_tradeability(
        p_success=0.40,
        approved=False,
        cfg=cfg,
        score_overrides={DOW: 10, GOLD: 99, EURUSD: 80, FTSE: 70},
        now=1_000_000.0,
    )
    assert failover_allows_epic(GOLD, cfg) is False
    assert effective_sb_allowlist({DOW}, cfg) == {DOW}


def test_ranked_promotes_strongest_demotes_weak_dow():
    from runtime.rotation_failover import (
        effective_sb_allowlist,
        failover_allows_epic,
        get_rotation_failover_state,
        note_dow_tradeability,
    )

    cfg = _cfg(enabled=True, ranked=True, top_n=2)
    st = note_dow_tradeability(
        p_success=0.40,
        approved=False,
        cfg=cfg,
        eligible_epics={DOW, GOLD, EURUSD, FTSE},
        score_overrides={DOW: 40.0, GOLD: 95.0, EURUSD: 88.0, FTSE: 50.0},
        expectancy_overrides={DOW: 0.0, GOLD: 0.0, EURUSD: 0.0, FTSE: 0.0},
        now=2_000_000.0,
    )
    assert st["ranked_rotator_mode"] is True
    assert st["rotation_failover_active"] is True
    assert st["ranked_rotator_dominant"] == GOLD
    assert st["rotation_failover_promoted"] == [GOLD, EURUSD]
    assert failover_allows_epic(GOLD, cfg) is True
    assert failover_allows_epic(EURUSD, cfg) is True
    assert failover_allows_epic(FTSE, cfg) is False
    # DOW demoted — not in top-2
    assert failover_allows_epic(DOW, cfg) is False
    allow = effective_sb_allowlist({DOW}, cfg)
    assert allow == {GOLD, EURUSD, "CS.D.EURUSD.TODAY.IP"}
    assert DOW not in allow


def test_ranked_dow_can_remain_dominant_when_strongest():
    from runtime.rotation_failover import (
        effective_sb_allowlist,
        note_dow_tradeability,
    )

    cfg = _cfg(top_n=2)
    st = note_dow_tradeability(
        p_success=0.85,
        approved=True,
        cfg=cfg,
        eligible_epics={DOW, GOLD, EURUSD, FTSE},
        score_overrides={DOW: 99.0, GOLD: 70.0, EURUSD: 60.0, FTSE: 55.0},
        expectancy_overrides={e: 0.0 for e in (DOW, GOLD, EURUSD, FTSE)},
        now=2_100_000.0,
    )
    assert st["ranked_rotator_dominant"] == DOW
    assert st["rotation_failover_promoted"][0] == DOW
    assert DOW in effective_sb_allowlist({DOW}, cfg)


def test_nikkei_never_promoted_even_if_scored_high():
    from runtime.rotation_failover import (
        failover_allows_epic,
        note_dow_tradeability,
        rank_candidate_markets,
    )

    cfg = _cfg(
        candidates=[DOW, GOLD, EURUSD, FTSE, NIKKEI],
        exclude=[NIKKEI],
    )
    rows = rank_candidate_markets(
        cfg,
        eligible_epics={DOW, GOLD, EURUSD, FTSE, NIKKEI},
        score_overrides={
            DOW: 10.0,
            GOLD: 20.0,
            EURUSD: 30.0,
            FTSE: 40.0,
            NIKKEI: 999.0,
        },
        expectancy_overrides={e: 0.0 for e in (DOW, GOLD, EURUSD, FTSE, NIKKEI)},
    )
    assert all(r["epic"] != NIKKEI for r in rows)
    st = note_dow_tradeability(
        p_success=0.4,
        approved=False,
        cfg=cfg,
        eligible_epics={DOW, GOLD, EURUSD, FTSE, NIKKEI},
        score_overrides={
            DOW: 10.0,
            GOLD: 20.0,
            EURUSD: 30.0,
            FTSE: 40.0,
            NIKKEI: 999.0,
        },
        expectancy_overrides={e: 0.0 for e in (DOW, GOLD, EURUSD, FTSE, NIKKEI)},
        now=2_200_000.0,
    )
    assert NIKKEI not in st["rotation_failover_promoted"]
    assert failover_allows_epic(NIKKEI, cfg) is False


def test_sb_hot_path_allows_ranked_promoted(
    monkeypatch: pytest.MonkeyPatch,
):
    from runtime.dual_core_execution import epic_allowed_on_hot_path
    from runtime.rotation_failover import note_dow_tradeability

    monkeypatch.setenv("IG_ACCOUNT_ID", ACCT_SB)
    monkeypatch.setenv("IG_ENGINE_ORIGIN", "MACRO_SENTINEL")
    cfg = _cfg(top_n=2)
    assert epic_allowed_on_hot_path(GOLD, cfg) is False

    note_dow_tradeability(
        p_success=0.4,
        approved=False,
        cfg=cfg,
        eligible_epics={DOW, GOLD, EURUSD, FTSE},
        score_overrides={DOW: 30.0, GOLD: 90.0, EURUSD: 85.0, FTSE: 40.0},
        expectancy_overrides={e: 0.0 for e in (DOW, GOLD, EURUSD, FTSE)},
        now=5_000_000.0,
    )
    assert epic_allowed_on_hot_path(GOLD, cfg) is True
    assert epic_allowed_on_hot_path(EURUSD, cfg) is True
    assert epic_allowed_on_hot_path(NIKKEI, cfg) is False


def test_selectivity_non_dow_carved_out_for_ranked_promoted():
    from runtime.overnight_entry_policy import evaluate_selectivity_gates
    from runtime.rotation_failover import note_dow_tradeability

    cfg = _cfg(top_n=2)
    before = evaluate_selectivity_gates(
        epic=GOLD,
        direction="BUY",
        p_success=0.85,
        obi=0.30,
        trend_15m="BULLISH",
        cfg=cfg,
        force_require=True,
        engine_origin="QUANT_SNIPER",
        account_id="Z6BAH4",
    )
    assert before.allow is False
    assert "non_dow" in before.reason

    note_dow_tradeability(
        p_success=0.4,
        approved=False,
        cfg=cfg,
        eligible_epics={DOW, GOLD, EURUSD, FTSE},
        score_overrides={DOW: 20.0, GOLD: 95.0, EURUSD: 50.0, FTSE: 40.0},
        expectancy_overrides={e: 0.0 for e in (DOW, GOLD, EURUSD, FTSE)},
        now=6_000_000.0,
    )
    after = evaluate_selectivity_gates(
        epic=GOLD,
        direction="BUY",
        p_success=0.85,
        obi=0.30,
        trend_15m="BULLISH",
        cfg=cfg,
        force_require=True,
        engine_origin="QUANT_SNIPER",
        account_id="Z6BAH4",
    )
    assert after.allow is True


def test_sb_micro_still_hard_off_with_ranked_enabled():
    from system.dual_regime import (
        sb_disable_core_b_micro,
        sb_disable_instant_micro,
        sb_macro_ltr_entries_only,
    )

    cfg = _cfg(enabled=True, ranked=True)
    assert sb_disable_instant_micro(cfg) is True
    assert sb_disable_core_b_micro(cfg) is True
    assert sb_macro_ltr_entries_only(cfg) is True


def test_legacy_stale_window_promotes_failover_epics():
    """Gold-only-style path still works when ranked_rotator_mode=false."""
    from runtime.rotation_failover import (
        effective_sb_allowlist,
        failover_allows_epic,
        note_dow_tradeability,
    )

    cfg = _cfg(enabled=True, ranked=False, stale=8.0, failover=[GOLD])
    t0 = 3_000_000.0
    note_dow_tradeability(
        p_success=0.44,
        approved=False,
        threshold=0.68,
        cfg=cfg,
        eligible_epics={DOW, GOLD},
        now=t0,
    )
    assert failover_allows_epic(GOLD, cfg) is False

    st = note_dow_tradeability(
        p_success=0.44,
        approved=False,
        threshold=0.68,
        cfg=cfg,
        eligible_epics={DOW, GOLD},
        now=t0 + 8 * 60,
    )
    assert st["rotation_failover_active"] is True
    assert GOLD in st["rotation_failover_promoted"]
    assert failover_allows_epic(GOLD, cfg) is True
    assert effective_sb_allowlist({DOW}, cfg) == {DOW, GOLD}


def test_legacy_recover_clears_promotion():
    from runtime.rotation_failover import (
        failover_allows_epic,
        note_dow_tradeability,
    )

    cfg = _cfg(enabled=True, ranked=False, stale=1.0, recover=2.0, failover=[GOLD])
    t0 = 4_000_000.0
    note_dow_tradeability(
        p_success=0.40,
        approved=False,
        threshold=0.68,
        cfg=cfg,
        eligible_epics={DOW, GOLD},
        now=t0,
    )
    note_dow_tradeability(
        p_success=0.40,
        approved=False,
        threshold=0.68,
        cfg=cfg,
        eligible_epics={DOW, GOLD},
        now=t0 + 60.0,
    )
    assert failover_allows_epic(GOLD, cfg) is True

    note_dow_tradeability(
        p_success=0.80,
        approved=True,
        threshold=0.68,
        cfg=cfg,
        now=t0 + 60 + 30,
    )
    assert failover_allows_epic(GOLD, cfg) is True

    note_dow_tradeability(
        p_success=0.80,
        approved=True,
        threshold=0.68,
        cfg=cfg,
        now=t0 + 60 + 30 + 120,
    )
    assert failover_allows_epic(GOLD, cfg) is False


def test_config_v31_ranked_candidates_include_core_markets():
    from system.config_loader import ConfigLoader

    cfg = ConfigLoader("config/config_v31_demo_throughput.json").load_config(
        validate=False
    )
    dual = cfg.get("dual_core") or {}
    assert dual.get("rotation_failover_enabled") is True
    assert dual.get("ranked_rotator_mode") is True
    cands = set(dual.get("ranked_candidate_epics") or [])
    assert {DOW, GOLD, EURUSD, FTSE} <= cands
    exclude = set(dual.get("exclude_from_hot_path") or [])
    assert NIKKEI in exclude
    assert GOLD not in exclude
    assert EURUSD not in exclude
    assert FTSE not in exclude
