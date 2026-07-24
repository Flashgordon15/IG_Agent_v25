"""v38 ranked rotator — confidence in score + hysteresis prefer/promote."""

from __future__ import annotations

import pytest

DOW = "IX.D.DOW.IFM.IP"
GOLD = "CS.D.CFPGOLD.CFP.IP"
EURUSD = "CS.D.EURUSD.CFD.IP"
FTSE = "IX.D.FTSE.IFM.IP"
NIKKEI = "IX.D.NIKKEI.IFM.IP"


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
    top_n: int = 2,
    min_lead: float = 0.05,
    min_hold: int = 3,
    conf_weight: float = 40.0,
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
            "ranked_confidence_weight": conf_weight,
            "ranked_setup_bonus": 8.0,
            "min_confidence_lead": min_lead,
            "min_hold_scans": min_hold,
            "ranked_candidate_epics": [DOW, GOLD, EURUSD, FTSE],
            "failover_epics": [GOLD, EURUSD, FTSE],
            "rotation_failover_confidence_floor": 0.68,
        },
    }


def _conf(
    p: float,
    *,
    approved: bool | None = None,
    thr: float = 0.68,
) -> dict:
    ap = bool(approved) if approved is not None else p >= thr
    return {"p_success": p, "approved": ap, "threshold": thr}


def test_confidence_tilts_rank_score_above_composite_alone():
    from runtime.rotation_failover import rank_candidate_markets

    cfg = _cfg()
    # Equal composites — GOLD SETUP confidence must outrank DOW WAIT.
    rows = rank_candidate_markets(
        cfg,
        eligible_epics={DOW, GOLD, EURUSD, FTSE},
        score_overrides={DOW: 50.0, GOLD: 50.0, EURUSD: 50.0, FTSE: 50.0},
        expectancy_overrides={e: 0.0 for e in (DOW, GOLD, EURUSD, FTSE)},
        confidence_overrides={
            DOW: _conf(0.44, approved=False),
            GOLD: _conf(0.71, approved=True, thr=0.68),
            EURUSD: _conf(0.50, approved=False, thr=0.70),
            FTSE: _conf(0.40, approved=False),
        },
    )
    assert rows[0]["epic"] == GOLD
    gold = next(r for r in rows if r["epic"] == GOLD)
    dow = next(r for r in rows if r["epic"] == DOW)
    assert gold["confidence"] == pytest.approx(0.71)
    assert dow["mode"] == "WAIT"
    assert gold["mode"] == "SETUP"
    assert gold["score"] > dow["score"]


def test_dow_low_wait_gold_high_setup_promotes_under_hysteresis():
    from runtime.rotation_failover import (
        failover_allows_epic,
        get_rotation_failover_state,
        note_dow_tradeability,
    )

    cfg = _cfg(min_hold=3, min_lead=0.05, top_n=2)
    scores = {DOW: 55.0, GOLD: 52.0, EURUSD: 40.0, FTSE: 35.0}
    conf = {
        DOW: _conf(0.44, approved=False),
        GOLD: _conf(0.71, approved=True, thr=0.68),
        EURUSD: _conf(0.50, approved=False),
        FTSE: _conf(0.35, approved=False),
    }
    zero_exp = {e: 0.0 for e in (DOW, GOLD, EURUSD, FTSE)}
    eligible = {DOW, GOLD, EURUSD, FTSE}

    # Scan 1 — bootstrap accepts current #1 (GOLD via confidence tilt).
    st1 = note_dow_tradeability(
        p_success=0.44,
        approved=False,
        cfg=cfg,
        eligible_epics=eligible,
        score_overrides=scores,
        expectancy_overrides=zero_exp,
        confidence_overrides=conf,
        now=10_000_000.0,
    )
    assert st1["prefer_epic"] == GOLD
    assert st1["ranked_rotator_dominant"] == GOLD
    assert GOLD in st1["rotation_failover_promoted"]
    assert st1["per_epic_confidence"][GOLD]["mode"] == "SETUP"
    assert st1["per_epic_confidence"][DOW]["mode"] == "WAIT"

    # Force DOW dominant first, then prove hysteresis before flipping to GOLD.
    from runtime.rotation_failover import reset_rotation_failover_for_tests

    reset_rotation_failover_for_tests()
    # Bootstrap DOW as dominant with DOW leading confidence briefly.
    note_dow_tradeability(
        p_success=0.80,
        approved=True,
        cfg=cfg,
        eligible_epics=eligible,
        score_overrides={DOW: 90.0, GOLD: 50.0, EURUSD: 40.0, FTSE: 35.0},
        expectancy_overrides=zero_exp,
        confidence_overrides={
            DOW: _conf(0.80, approved=True),
            GOLD: _conf(0.40, approved=False, thr=0.68),
            EURUSD: _conf(0.40, approved=False),
            FTSE: _conf(0.35, approved=False),
        },
        now=10_000_100.0,
    )
    assert get_rotation_failover_state()["ranked_rotator_dominant"] == DOW

    # Flip scores+confidence to GOLD SETUP — must NOT promote on first scan.
    st_hold = note_dow_tradeability(
        p_success=0.44,
        approved=False,
        cfg=cfg,
        eligible_epics=eligible,
        score_overrides=scores,
        expectancy_overrides=zero_exp,
        confidence_overrides=conf,
        now=10_000_200.0,
    )
    assert st_hold["ranked_rotator_dominant"] == DOW
    assert st_hold["prefer_epic"] == DOW
    assert st_hold["hold_challenger"] == GOLD
    assert st_hold["hold_scans"] == 1
    assert "holding" in (st_hold.get("preference_reason") or "").lower()

    # Scans 2–3 complete hysteresis → promote GOLD.
    note_dow_tradeability(
        p_success=0.44,
        approved=False,
        cfg=cfg,
        eligible_epics=eligible,
        score_overrides=scores,
        expectancy_overrides=zero_exp,
        confidence_overrides=conf,
        now=10_000_300.0,
    )
    st_flip = note_dow_tradeability(
        p_success=0.44,
        approved=False,
        cfg=cfg,
        eligible_epics=eligible,
        score_overrides=scores,
        expectancy_overrides=zero_exp,
        confidence_overrides=conf,
        now=10_000_400.0,
    )
    assert st_flip["ranked_rotator_dominant"] == GOLD
    assert st_flip["prefer_epic"] == GOLD
    assert GOLD in st_flip["rotation_failover_promoted"]
    assert failover_allows_epic(GOLD, cfg) is True
    assert "prefer GOLD" in (st_flip.get("preference_reason") or "")


def test_nikkei_still_excluded_with_confidence_overrides():
    from runtime.rotation_failover import (
        failover_allows_epic,
        note_dow_tradeability,
        rank_candidate_markets,
    )

    cfg = _cfg(exclude=[NIKKEI])
    # Inject Nikkei into candidates via dual_core override.
    cfg["dual_core"]["ranked_candidate_epics"] = [DOW, GOLD, EURUSD, FTSE, NIKKEI]
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
        confidence_overrides={
            DOW: _conf(0.4),
            GOLD: _conf(0.5),
            EURUSD: _conf(0.5),
            FTSE: _conf(0.5),
            NIKKEI: _conf(0.99, approved=True),
        },
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
        confidence_overrides={
            DOW: _conf(0.4),
            GOLD: _conf(0.5),
            EURUSD: _conf(0.5),
            FTSE: _conf(0.5),
            NIKKEI: _conf(0.99, approved=True),
        },
        now=11_000_000.0,
    )
    assert NIKKEI not in st["rotation_failover_promoted"]
    assert NIKKEI not in (st.get("per_epic_confidence") or {})
    assert failover_allows_epic(NIKKEI, cfg) is False


def test_sb_instant_micro_still_hard_off_with_confidence_rank():
    from system.dual_regime import (
        sb_disable_core_b_micro,
        sb_disable_instant_micro,
        sb_macro_ltr_entries_only,
    )

    cfg = _cfg()
    assert sb_disable_instant_micro(cfg) is True
    assert sb_disable_core_b_micro(cfg) is True
    assert sb_macro_ltr_entries_only(cfg) is True


def test_rotation_state_shape_includes_prefer_and_per_epic():
    from runtime.dual_core_execution import _ranked_rotator_state_unlocked
    from runtime.rotation_failover import note_dow_tradeability

    cfg = _cfg(min_hold=1)
    note_dow_tradeability(
        p_success=0.44,
        approved=False,
        cfg=cfg,
        eligible_epics={DOW, GOLD, EURUSD, FTSE},
        score_overrides={DOW: 40.0, GOLD: 60.0, EURUSD: 50.0, FTSE: 30.0},
        expectancy_overrides={e: 0.0 for e in (DOW, GOLD, EURUSD, FTSE)},
        confidence_overrides={
            DOW: _conf(0.44, approved=False),
            GOLD: _conf(0.71, approved=True, thr=0.68),
            EURUSD: _conf(0.55, approved=False),
            FTSE: _conf(0.40, approved=False),
        },
        now=12_000_000.0,
    )
    payload = _ranked_rotator_state_unlocked()
    assert payload["prefer_epic"] == GOLD
    assert payload["preference_reason"]
    assert GOLD in payload["per_epic_confidence"]
    rr = payload["ranked_rotator"]
    assert rr["prefer_epic"] == GOLD
    assert rr["per_epic_confidence"][GOLD]["p_success"] == pytest.approx(0.71)
