"""Focused P0 profitability barrage — broker stop 12, SB hard-cap, min-hold, fantasy peak, SB DOW-only."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

DOW = "IX.D.DOW.IFM.IP"
GOLD = "CS.D.CFPGOLD.CFP.IP"
EURUSD = "CS.D.EURUSD.CFD.IP"
ACCT_SB = "Z6BAH3"
ACCT_CFD = "Z6BAH4"


class _FakeRestMin6:
    """SB-like DOW dealing rules — IG min stop is 6, not 12."""

    account_id = ACCT_SB

    def fetch_market_constraints(self, epic: str, **_k):  # noqa: ANN001
        return {"min_stop_distance": 6.0}


# ---------------------------------------------------------------------------
# 1. Broker stop = 12 on DOW submit payload
# ---------------------------------------------------------------------------


def test_dow_broker_stop_floored_to_12_despite_ig_min_6():
    from execution.live_broker_order_router import (
        desk_entry_stop_floor_pts,
        floor_stop_distance_points,
        normalize_placement_distances,
    )
    from runtime.virtual_stop_loss import stretch_broker_stop_distance

    assert desk_entry_stop_floor_pts(DOW) >= 12.0
    res = floor_stop_distance_points(_FakeRestMin6(), DOW, 6.0)
    assert res.effective_points >= 12.0
    assert res.effective_points != pytest.approx(6.0)
    stop, limit, _ = normalize_placement_distances(
        _FakeRestMin6(), DOW, stop_distance=4.0, limit_distance=6.0
    )
    assert stop >= 12.0
    assert limit >= 12.0
    stretched = stretch_broker_stop_distance(_FakeRestMin6(), DOW, 6.0)
    assert stretched >= 12.0


def test_asymmetric_payload_stop_distance_is_12():
    from execution.asymmetric_ioc_router import build_ig_otc_market_payload
    from execution.live_broker_order_router import normalize_placement_distances

    stop_n, limit_n, _ = normalize_placement_distances(
        _FakeRestMin6(), DOW, stop_distance=6.0, limit_distance=None
    )
    payload = build_ig_otc_market_payload(
        epic=DOW,
        direction="BUY",
        size=0.5,
        stop_distance=float(stop_n),
        max_slippage=2,
        limit_distance=limit_n,
        currency_code="GBP",
    )
    assert float(payload["stopDistance"]) >= 12.0


# ---------------------------------------------------------------------------
# 2. SB second opposite order rejected when open≥1
# ---------------------------------------------------------------------------


def test_sb_hard_cap_blocks_second_and_opposite():
    from execution.order_in_flight_mutex import (
        HARD_OPEN_CAP_BY_ACCOUNT,
        hard_cap_blocks_entry,
        note_account_flat,
        note_account_open,
        reset_order_mutex_for_tests,
        resolve_account_hard_open_cap,
    )

    reset_order_mutex_for_tests()
    assert resolve_account_hard_open_cap(ACCT_SB) == 1
    assert HARD_OPEN_CAP_BY_ACCOUNT.get(ACCT_SB) == 1
    note_account_flat(ACCT_SB)
    blocked0, _ = hard_cap_blocks_entry(ACCT_SB, open_count=0)
    assert blocked0 is False
    note_account_open(ACCT_SB, delta=1)
    blocked1, reason = hard_cap_blocks_entry(ACCT_SB, open_count=1)
    assert blocked1 is True
    assert "hard_cap" in reason
    # Opposite side is still a new entry — same hard-cap rejects.
    blocked_opp, _ = hard_cap_blocks_entry(ACCT_SB, open_count=1)
    assert blocked_opp is True
    note_account_flat(ACCT_SB)
    reset_order_mutex_for_tests()


# ---------------------------------------------------------------------------
# 3. Min-hold blocks early trail
# ---------------------------------------------------------------------------


def test_min_hold_blocks_early_dynamic_limit_trail(monkeypatch: pytest.MonkeyPatch):
    import runtime.dynamic_limit_engine as dle

    dle.reset_dynamic_limit_for_tests()
    dle.start_dynamic_limit_engine()
    monkeypatch.setattr(dle, "_min_hold_before_trail_sec", lambda cfg=None: 150.0)
    dle.register_dynamic_limit(
        deal_id="DI_MINHOLD1",
        epic=DOW,
        direction="BUY",
        entry_level=45000.0,
        limit_pts=6.0,
        size=0.5,
        trail_trigger_ig_pts=2.0,
    )
    # Favorable move that would hit initial TP — still inside min-hold.
    hits = dle.check_limit_hit(DOW, 45000.0 + 8.0)
    assert hits == []
    # After aging past min-hold, TP can fire.
    with dle._lock:
        dle._tracks["DI_MINHOLD1"].armed_at = time.time() - 200.0
    hits2 = dle.check_limit_hit(DOW, 45000.0 + 8.0)
    assert "DI_MINHOLD1" in hits2
    dle.reset_dynamic_limit_for_tests()


# ---------------------------------------------------------------------------
# 4. SB non-DOW entry blocked
# ---------------------------------------------------------------------------


def test_sb_hot_path_dow_only(monkeypatch: pytest.MonkeyPatch):
    from runtime.dual_core_execution import epic_allowed_on_hot_path

    monkeypatch.setenv("IG_ACCOUNT_ID", ACCT_SB)
    monkeypatch.setenv("IG_ENGINE_ORIGIN", "MACRO_SENTINEL")
    cfg = {
        "dual_core": {
            "sb_hot_path_allowlist": [DOW],
            "exclude_from_hot_path": [],
        }
    }
    assert epic_allowed_on_hot_path(DOW, cfg) is True
    assert epic_allowed_on_hot_path(GOLD, cfg) is False
    assert epic_allowed_on_hot_path(EURUSD, cfg) is False
    assert epic_allowed_on_hot_path("IX.D.NIKKEI.IFM.IP", cfg) is False
    assert epic_allowed_on_hot_path("IX.D.FTSE.IFM.IP", cfg) is False


def test_cfd_lane_not_bound_by_sb_allowlist(monkeypatch: pytest.MonkeyPatch):
    from runtime.dual_core_execution import epic_allowed_on_hot_path

    monkeypatch.setenv("IG_ACCOUNT_ID", ACCT_CFD)
    monkeypatch.setenv("IG_ENGINE_ORIGIN", "QUANT_SNIPER")
    cfg = {
        "dual_core": {
            "sb_hot_path_allowlist": [DOW],
            "exclude_from_hot_path": ["IX.D.NIKKEI.IFM.IP"],
        }
    }
    # CFD still allows DOW; Gold not excluded globally so may pass DOW-authority path only.
    assert epic_allowed_on_hot_path(DOW, cfg) is True


# ---------------------------------------------------------------------------
# 5. DynamicLimit fantasy peak rejected (SB too)
# ---------------------------------------------------------------------------


def test_dynamic_limit_fantasy_peak_rejected_on_sb():
    import runtime.dynamic_limit_engine as dle

    dle.reset_dynamic_limit_for_tests()
    dle.start_dynamic_limit_engine()
    dle.register_dynamic_limit(
        deal_id="DI_FANTASY_SB",
        epic=DOW,
        direction="BUY",
        entry_level=45000.0,
        limit_pts=12.0,
        size=0.5,
        trail_trigger_ig_pts=2.0,
    )
    # 19.5pt one-tick jump from 0 peak — must reject (was false flatten vector).
    dle.update_from_mid(DOW, 45000.0 + 19.5)
    assert dle._tracks["DI_FANTASY_SB"].peak_profit_ig_pts == 0.0
    # Gradual real move ≤25pt jump is accepted.
    dle.update_from_mid(DOW, 45000.0 + 4.0)
    assert dle._tracks["DI_FANTASY_SB"].peak_profit_ig_pts == pytest.approx(4.0)
    dle.update_from_mid(DOW, 45000.0 + 10.0)
    assert dle._tracks["DI_FANTASY_SB"].peak_profit_ig_pts == pytest.approx(10.0)
    # Jump from 10 → 40 (>25) rejected.
    dle.update_from_mid(DOW, 45000.0 + 40.0)
    assert dle._tracks["DI_FANTASY_SB"].peak_profit_ig_pts == pytest.approx(10.0)
    dle.reset_dynamic_limit_for_tests()


def test_config_flags_p0_present():
    import json
    from pathlib import Path

    cfg = json.loads(
        Path("config/config_v31_demo_throughput.json").read_text(encoding="utf-8")
    )
    mr = cfg["micro_risk"]
    assert float(mr["virtual_stop_ceiling_pts"]) == 12.0
    assert float(mr["dow_broker_stop_floor_pts"]) == 12.0
    assert float(mr["min_hold_before_trail_sec"]) >= 120.0
    assert cfg["dual_core"]["sb_hot_path_allowlist"] == [DOW]
    assert cfg["dual_core"]["cfd_require_15m_trend_ml_obi"] is True
    assert cfg["micro_scalp_instant"]["require_15m_trend_ml_obi"] is True
    assert cfg["long_trade_runner"]["skip_dynamic_limit_until_armed"] is True
