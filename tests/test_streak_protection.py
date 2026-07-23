"""Streak protection — post-win / post-loss entry gates + CFD chop selectivity."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from execution.streak_protection import (
    arm_streak_protection_on_close,
    check_cfd_chop_selectivity,
    check_streak_entry_allowed,
    reset_streak_protection_for_tests,
    set_streak_clock_for_tests,
    streak_cfg,
)


CFG = {
    "entry_protection": {
        "streak_protection_enabled": True,
        "post_win_cooldown_sec": 600,
        "post_loss_lock_sec": 900,
        "post_loss_mode": "lock",
    },
    "dual_core": {
        "cfd_block_mean_reversion": True,
        "cfd_require_15m_trend_ml_obi": False,
    },
    "pre_entry_regime_veto": {
        "cfd_block_mean_reversion": True,
    },
    "micro_risk": {
        "streak_protection": {
            "enabled": True,
            "post_win_cooldown_sec": 600,
            "post_loss_lock_sec": 900,
        }
    },
}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    reset_streak_protection_for_tests()
    data = tmp_path / "data"
    (data / "state_cfd").mkdir(parents=True)
    (data / "state_sb").mkdir(parents=True)
    (data / "state").mkdir(parents=True)
    monkeypatch.setenv("IG_DATA_ROOT", str(data))
    monkeypatch.setenv("IG_AGENT_DATA_DIR", str(data))
    monkeypatch.delenv("IG_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("IG_ENGINE_ORIGIN", raising=False)
    monkeypatch.setattr("system.paths.data_dir", lambda: data)
    monkeypatch.setattr("system.paths.bridge_legacy_data_into", lambda *a, **k: [])
    monkeypatch.setattr("execution.streak_protection.log_engine", lambda *a, **k: None)
    yield
    reset_streak_protection_for_tests()


def test_streak_cfg_defaults():
    sc = streak_cfg(CFG)
    assert sc["enabled"] is True
    assert sc["post_win_cooldown_sec"] == 600
    assert sc["post_loss_lock_sec"] == 900
    assert sc["cfd_block_mean_reversion"] is True


def test_post_win_blocks_entry_then_expires():
    t0 = 1_700_000_000.0
    set_streak_clock_for_tests(t0)

    arm = arm_streak_protection_on_close(
        account_id="Z6BAH3",
        realized_pnl_gbp=2.5,
        deal_id="DIAAAAWIN001",
        cfg=CFG,
    )
    assert arm["armed"] is True
    assert arm["kind"] == "post_win_cooldown"

    ok, reason = check_streak_entry_allowed(
        "Z6BAH3", epic="IX.D.DOW.IFM.IP", cfg=CFG, now=t0 + 10, skip_cfd_chop=True
    )
    assert ok is False
    assert "post_win_cooldown" in reason

    ok_other, _ = check_streak_entry_allowed(
        "Z6BAH4", epic="IX.D.DOW.IFM.IP", cfg=CFG, now=t0 + 10, skip_cfd_chop=True
    )
    assert ok_other is True

    ok_later, reason_later = check_streak_entry_allowed(
        "Z6BAH3", epic="IX.D.DOW.IFM.IP", cfg=CFG, now=t0 + 601, skip_cfd_chop=True
    )
    assert ok_later is True
    assert reason_later == "ok"

    path = Path(os.environ["IG_DATA_ROOT"]) / "state_sb" / "streak_protection.json"
    assert path.is_file()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["last_deal_id"] == "DIAAAAWIN001"
    assert float(raw["post_win_until"]) == pytest.approx(t0 + 600)


def test_post_loss_blocks_entry():
    t0 = 1_700_000_100.0
    set_streak_clock_for_tests(t0)

    arm = arm_streak_protection_on_close(
        account_id="Z6BAH4",
        realized_pnl_gbp=-1.25,
        deal_id="DIAAAALOSS001",
        cfg=CFG,
    )
    assert arm["armed"] is True
    assert arm["kind"] == "post_loss_lock"

    ok, reason = check_streak_entry_allowed(
        "Z6BAH4", epic="IX.D.DOW.IFM.IP", cfg=CFG, now=t0 + 30, skip_cfd_chop=True
    )
    assert ok is False
    assert "post_loss_tilt_lock" in reason

    ok_later, _ = check_streak_entry_allowed(
        "Z6BAH4", epic="IX.D.DOW.IFM.IP", cfg=CFG, now=t0 + 901, skip_cfd_chop=True
    )
    assert ok_later is True

    path = Path(os.environ["IG_DATA_ROOT"]) / "state_cfd" / "streak_protection.json"
    assert path.is_file()


def test_cfd_blocked_in_mean_reversion_sb_allowed():
    ok_cfd, reason = check_cfd_chop_selectivity(
        account_id="Z6BAH4",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        cfg=CFG,
        product_type="CFD",
        engine_origin="QUANT_SNIPER",
        regime_label="MEAN_REVERSION",
    )
    assert ok_cfd is False
    assert "cfd_chop_block" in reason

    ok_sb, reason_sb = check_cfd_chop_selectivity(
        account_id="Z6BAH3",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        cfg=CFG,
        product_type="SPREADBET",
        engine_origin="MACRO_SENTINEL",
        regime_label="MEAN_REVERSION",
    )
    assert ok_sb is True
    assert reason_sb == "sb_lane_exempt"


def test_cfd_entry_helper_blocks_mean_reversion(monkeypatch):
    monkeypatch.setattr(
        "execution.streak_protection._resolve_regime_label",
        lambda epic: "MEAN_REVERSION",
    )
    ok, reason = check_streak_entry_allowed(
        "Z6BAH4",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        cfg=CFG,
        product_type="CFD",
        engine_origin="QUANT_SNIPER",
    )
    assert ok is False
    assert "cfd_chop_block" in reason

    ok_sb, r_sb = check_streak_entry_allowed(
        "Z6BAH3",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        cfg=CFG,
        product_type="SPREADBET",
        engine_origin="MACRO_SENTINEL",
    )
    assert ok_sb is True
    assert r_sb == "ok"


def test_idempotent_arm_same_deal():
    t0 = 1_700_000_200.0
    set_streak_clock_for_tests(t0)
    a1 = arm_streak_protection_on_close(
        account_id="Z6BAH3",
        realized_pnl_gbp=1.0,
        deal_id="DIAAAADUP001",
        cfg=CFG,
    )
    a2 = arm_streak_protection_on_close(
        account_id="Z6BAH3",
        realized_pnl_gbp=1.0,
        deal_id="DIAAAADUP001",
        cfg=CFG,
    )
    assert a1["armed"] is True
    assert a2["armed"] is False
    assert a2["reason"] == "already_armed"


def test_flat_pnl_does_not_arm():
    t0 = 1_700_000_300.0
    set_streak_clock_for_tests(t0)
    arm = arm_streak_protection_on_close(
        account_id="Z6BAH3",
        realized_pnl_gbp=0.0,
        deal_id="DIAAAAFLAT001",
        cfg=CFG,
    )
    assert arm["armed"] is False
    ok, _ = check_streak_entry_allowed(
        "Z6BAH3", cfg=CFG, now=t0 + 1, skip_cfd_chop=True
    )
    assert ok is True


def test_cooldown_expiry_allows_entry():
    t0 = 1_700_000_400.0
    set_streak_clock_for_tests(t0)
    arm_streak_protection_on_close(
        account_id="Z6BAH4",
        realized_pnl_gbp=3.0,
        deal_id="DIAAAAJRNL001",
        cfg=CFG,
    )
    ok, reason = check_streak_entry_allowed(
        "Z6BAH4", cfg=CFG, now=t0 + 5, skip_cfd_chop=True
    )
    assert ok is False
    assert "post_win_cooldown" in reason
    ok2, reason2 = check_streak_entry_allowed(
        "Z6BAH4", cfg=CFG, now=t0 + 600.1, skip_cfd_chop=True
    )
    assert ok2 is True
    assert reason2 == "ok"
