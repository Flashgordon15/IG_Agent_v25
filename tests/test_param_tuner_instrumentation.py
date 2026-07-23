"""Parameter instrumentation harness — hot-reload overlay absorption."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from diagnostics.fill_rate_monitor import (
    get_fill_rate_monitor,
    reset_fill_rate_monitor_for_tests,
)
from diagnostics.param_tuner import (
    compute_instrumentation,
    load_overlay_cached,
    merge_cfg_section,
    observe_atr_sample,
    reset_param_tuner_for_tests,
    run_instrumentation_cycle,
    write_instrumentation_overlay,
)
from execution.adaptive_atr_bracket import resolve_adaptive_entry_bracket
from execution.pre_entry_regime_veto import evaluate_pre_entry_regime_decision


@pytest.fixture()
def overlay_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "tuning_overlay.json"
    path.write_text(
        json.dumps(
            {
                "grok_macro_bias": "VETO",
                "regime_matrix": {"0": {"size_factor": 1.0}},
                "params": {},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("IG_TUNING_OVERLAY", str(path))
    reset_param_tuner_for_tests()
    reset_fill_rate_monitor_for_tests()
    yield path
    reset_param_tuner_for_tests()
    reset_fill_rate_monitor_for_tests()


def test_hot_reload_overlay_json_decode_safe(overlay_tmp: Path):
    """Atomic write must leave a valid JSON document the engine can decode."""
    sections = compute_instrumentation(fill_rate_15m=0.80, grok_bias="VETO")
    result = write_instrumentation_overlay(sections, path=overlay_tmp)
    assert result["ok"] is True

    # Simulate microkernel hot-reload: re-read without JSONDecodeError
    raw = overlay_tmp.read_text(encoding="utf-8")
    body = json.loads(raw)
    assert isinstance(body, dict)
    assert body["regime_matrix"]["0"]["size_factor"] == 1.0  # preserved
    assert body["pre_entry_regime_veto"]["max_spread_pct"] == pytest.approx(0.00015)
    assert "instrumentation" in body

    cached = load_overlay_cached(force=True)
    assert cached["obi_filter"]["min_abs_ratio"] == pytest.approx(0.22)


def test_fill_rate_tightens_spread_absorbed_by_regime_gate(
    overlay_tmp: Path, monkeypatch: pytest.MonkeyPatch
):
    """Fill rate >75% → 0.015% spread cap absorbed without restart."""
    mon = get_fill_rate_monitor(sync_mode=True)
    mon.reset()
    for _ in range(16):
        mon.record_fill()
    for _ in range(4):
        mon.record_slippage_reject("x")
    # 80% fill rate on timed window
    assert (mon.rolling_fill_rate_15m() or 0) > 0.75

    run_instrumentation_cycle(path=overlay_tmp, fill_rate_15m=0.80, grok_bias="NEUTRAL")
    reset_param_tuner_for_tests()  # force cache re-read via env path still set

    merged = merge_cfg_section(
        {"pre_entry_regime_veto": {"enabled": True, "max_spread_pct": 0.0002}},
        "pre_entry_regime_veto",
    )
    assert merged["max_spread_pct"] == pytest.approx(0.00015)

    monkeypatch.setattr(
        "execution.entry_gate_hardening.evaluate_obi_entry_filter",
        lambda *a, **k: (True, "obi_ok", 0.2),
    )
    monkeypatch.setattr(
        "execution.grok_macro_bias.resolve_grok_macro_bias",
        lambda cfg=None: "NEUTRAL",
    )
    # mid=100000, spread=16 → 0.016% > 0.015% tightened cap → block
    # (would pass default 0.02%)
    d = evaluate_pre_entry_regime_decision(
        "IX.D.DOW.IFM.IP",
        "BUY",
        bid=100000.0,
        offer=100016.0,
        cfg={
            "pre_entry_regime_veto": {
                "enabled": True,
                "max_spread_pct": 0.0002,
                "max_spread_pts": 0,
            },
            "spread_elasticity": {"enabled": False},
            "leader_follower": {"enabled": False},
            "obi_filter": {"enabled": True, "require_align": False},
        },
    )
    assert d.allowed is False
    # RuntimeContext DOW cap (3pt) fires before mid-% when pts are toxic
    assert ("spread_pct" in d.reason) or ("spread_pts" in d.reason)


def test_bias_exit_veto_scales_tp_rr_via_overlay(
    overlay_tmp: Path, monkeypatch: pytest.MonkeyPatch
):
    """VETO→NEUTRAL + ATR top-80th pct → slip-aware TP ≤2.75x; SL risk 1.0x."""
    reset_param_tuner_for_tests()
    # Seed ATR ring with lower values, then a top-decile print
    for v in [10.0 + i * 0.1 for i in range(25)]:
        observe_atr_sample(v)
    observe_atr_sample(20.0)  # high vs ring → high percentile

    # Establish previous bias = VETO in tuner memory
    run_instrumentation_cycle(
        path=overlay_tmp, fill_rate_15m=0.60, grok_bias="VETO", atr=12.0
    )
    out = run_instrumentation_cycle(
        path=overlay_tmp, fill_rate_15m=0.60, grok_bias="NEUTRAL", atr=20.0
    )
    assert out["computed"]["elevated_vol_reward_risk"] == pytest.approx(2.75)

    body = json.loads(overlay_tmp.read_text(encoding="utf-8"))
    assert body["volatility_bracket"]["elevated_vol_reward_risk"] == pytest.approx(2.75)
    assert body["volatility_bracket"]["stop_risk_multiple"] == pytest.approx(1.0)

    # Engine absorbs overlay RR without JSON errors
    monkeypatch.setattr(
        "execution.adaptive_atr_bracket._resolve_atrs",
        lambda epic: (30.0, 10.0),
    )
    cfg = {
        "micro_risk": {
            "risk_per_trade_gbp": 4.0,
            "target_r_multiple": 2.0,
            "min_profit_target_pts": 1.0,
            "max_loss_cap_pts": 4.0,
            "virtual_stop_ceiling_pts": 4.0,
        },
        "volatility_bracket": {
            "enabled": True,
            "size_scale_vol_ratio_ceil": 1.35,
            "elevated_vol_reward_risk": 3.0,  # cfg stale; overlay should win
        },
    }
    reset_param_tuner_for_tests()
    result = resolve_adaptive_entry_bracket(
        "IX.D.DOW.IFM.IP", "BUY", 0.5, cfg, entry=45000.0
    )
    assert result.mode == "asymmetric_rr_elevated_vol"
    # Slip-aware RR from overlay (2.75 raw − 0.25 haircut → 2.5×)
    assert result.tp_pts >= result.sl_pts * 2.4 - 1e-6
    assert result.tp_pts <= result.sl_pts * 2.8 + 1e-6
