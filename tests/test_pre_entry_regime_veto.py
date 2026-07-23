"""Pre-entry regime veto — spread% + OBI before network."""

from __future__ import annotations

import pytest

from execution.pre_entry_regime_veto import evaluate_pre_entry_regime_veto


CFG = {
    "pre_entry_regime_veto": {
        "enabled": True,
        "max_spread_pct": 0.0002,
        "max_spread_pts": 0,
        "enforce_spread_pct": True,
    },
    "obi_filter": {
        "enabled": True,
        "min_abs_ratio": 0.15,
        "require_align": False,
        "fail_closed_on_neutral": False,
    },
    # Isolate classic veto tests from new structural layers
    "spread_elasticity": {"enabled": False},
    "leader_follower": {"enabled": False},
    "grok_macro_bias": "NEUTRAL",
}


@pytest.fixture(autouse=True)
def _isolate_grok_and_overlay(monkeypatch, tmp_path):
    """Disk may hold live VETO / instrumentation — isolate classic unit tests."""
    monkeypatch.setattr(
        "execution.grok_macro_bias.resolve_grok_macro_bias",
        lambda cfg=None: "NEUTRAL",
    )
    overlay = tmp_path / "tuning_overlay.json"
    overlay.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("IG_TUNING_OVERLAY", str(overlay))
    try:
        from diagnostics.param_tuner import reset_param_tuner_for_tests

        reset_param_tuner_for_tests()
    except Exception:
        pass
    yield


def test_spread_pct_kills_wide_book(monkeypatch):
    monkeypatch.setattr(
        "execution.entry_gate_hardening.evaluate_obi_entry_filter",
        lambda *a, **k: (True, "obi_ok", 0.2),
    )
    # mid=100, spread=0.05 → 0.05% > 0.02%
    ok, reason = evaluate_pre_entry_regime_veto(
        "IX.D.DOW.IFM.IP", "BUY", bid=100.0, offer=100.05, cfg=CFG
    )
    assert ok is False
    assert "spread_pct" in reason


def test_tight_spread_passes(monkeypatch):
    monkeypatch.setattr(
        "execution.entry_gate_hardening.evaluate_obi_entry_filter",
        lambda *a, **k: (True, "obi_ok", 0.2),
    )
    # mid=50000, spread=2 → under DOW RuntimeContext cap (3.0) and 0.02% pct
    ok, reason = evaluate_pre_entry_regime_veto(
        "IX.D.DOW.IFM.IP", "BUY", bid=50000.0, offer=50002.0, cfg=CFG
    )
    assert ok is True
    assert reason == "regime_veto_clear"


def test_obi_crash_kills_buy(monkeypatch):
    monkeypatch.setattr(
        "execution.entry_gate_hardening.evaluate_obi_entry_filter",
        lambda *a, **k: (False, "obi_crash_guard ratio=-0.40", -0.40),
    )
    ok, reason = evaluate_pre_entry_regime_veto(
        "IX.D.DOW.IFM.IP", "BUY", bid=50000.0, offer=50002.0, cfg=CFG
    )
    assert ok is False
    assert "obi" in reason


def test_invalid_book_fail_closed():
    ok, reason = evaluate_pre_entry_regime_veto(
        "IX.D.DOW.IFM.IP", "BUY", bid=0.0, offer=0.0, cfg=CFG
    )
    assert ok is False
    assert "invalid_book" in reason


def test_sovereign_instant_block_purged(monkeypatch):
    import execution.pre_entry_regime_veto as prv

    orig = prv._sovereign_regime_label
    prv._sovereign_regime_label = lambda epic: "RANGE_BOUND"  # type: ignore[assignment]
    try:
        ok, reason = prv.evaluate_sovereign_regime_instant_block(
            "IX.D.DOW.IFM.IP", cfg=CFG
        )
        assert ok is True
        assert "purged" in reason
    finally:
        prv._sovereign_regime_label = orig  # type: ignore[assignment]


def test_range_bound_passes_when_ml_obi_qualify(monkeypatch):
    import execution.pre_entry_regime_veto as prv

    orig = prv._resolve_regime_label
    prv._resolve_regime_label = lambda epic: "RANGE_BOUND"  # type: ignore[assignment]
    monkeypatch.setattr(
        "alpha.micro_sniper_ml.evaluate_live_sniper_probability",
        lambda *a, **k: type(
            "R",
            (),
            {
                "p_success": 0.72,
                "approved": True,
                "threshold": 0.68,
                "reason": "ok",
                "features": {},
            },
        )(),
    )
    monkeypatch.setattr(
        "execution.pre_entry_regime_veto._resolve_bypass_obi_signal",
        lambda *a, **k: (0.20, "quote_proxy"),
    )
    try:
        ok, reason = evaluate_pre_entry_regime_veto(
            "IX.D.DOW.IFM.IP",
            "BUY",
            bid=50000.0,
            offer=50002.0,
            cfg={
                **CFG,
                "pre_entry_regime_veto": {
                    **CFG["pre_entry_regime_veto"],
                    "enforce_spread_pct": False,
                },
            },
        )
        assert ok is True
        assert "sovereign_ml_obi_bypass" in reason or reason == "regime_veto_clear"
    finally:
        prv._resolve_regime_label = orig  # type: ignore[assignment]


def test_dow_overnight_allowlist_passes_mean_reversion(monkeypatch):
    """Night-matrix DOW often labels MEAN_REVERSION; allowlist must not starve it."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    import execution.pre_entry_regime_veto as prv

    orig = prv._resolve_regime_label
    prv._resolve_regime_label = lambda epic: "MEAN_REVERSION"  # type: ignore[assignment]
    monkeypatch.setattr(
        prv,
        "sovereign_ml_obi_bypass_qualifies",
        lambda *a, **k: (False, "bypass_off"),
    )
    monkeypatch.setattr(
        prv,
        "datetime",
        type(
            "DT",
            (),
            {
                "now": staticmethod(
                    lambda tz=None: datetime(
                        2026, 7, 23, 2, 15, tzinfo=ZoneInfo("Europe/London")
                    )
                )
            },
        ),
    )
    try:
        ok, reason = prv.evaluate_trending_regime_gate(
            "IX.D.DOW.IFM.IP",
            cfg={
                **CFG,
                "pre_entry_regime_veto": {
                    **CFG["pre_entry_regime_veto"],
                    "require_trending_regime": True,
                    "dow_overnight_allowlist": True,
                },
            },
            direction="BUY",
            bid=50000.0,
            offer=50002.0,
        )
        assert ok is True
        assert "dow_overnight_allow" in reason
        assert "MEAN_REVERSION" in reason
    finally:
        prv._resolve_regime_label = orig  # type: ignore[assignment]


def test_mean_reversion_passes_when_ml_obi_qualify(monkeypatch):
    import execution.pre_entry_regime_veto as prv

    orig = prv._resolve_regime_label
    prv._resolve_regime_label = lambda epic: "MEAN_REVERSION"  # type: ignore[assignment]
    monkeypatch.setattr(
        "alpha.micro_sniper_ml.evaluate_live_sniper_probability",
        lambda *a, **k: type(
            "R",
            (),
            {
                "p_success": 0.70,
                "approved": True,
                "threshold": 0.68,
                "reason": "ok",
                "features": {"obi_velocity": 0.20},
            },
        )(),
    )
    monkeypatch.setattr(
        "execution.entry_gate_hardening.resolve_raw_obi_ratio",
        lambda *a, **k: (0.18, "quote_proxy"),
    )
    try:
        ok, reason = prv.evaluate_trending_regime_gate(
            "IX.D.DOW.IFM.IP",
            cfg=CFG,
            direction="BUY",
            bid=50000.0,
            offer=50002.0,
        )
        assert ok is True
        assert "sovereign_ml_obi_bypass" in reason
        assert "MEAN_REVERSION" in reason
    finally:
        prv._resolve_regime_label = orig  # type: ignore[assignment]


def test_sovereign_bypass_requires_directional_obi(monkeypatch):
    import execution.pre_entry_regime_veto as prv

    monkeypatch.setattr(
        "alpha.micro_sniper_ml.evaluate_live_sniper_probability",
        lambda *a, **k: type(
            "R",
            (),
            {
                "p_success": 0.72,
                "approved": True,
                "threshold": 0.68,
                "reason": "ok",
                "features": {},
            },
        )(),
    )
    monkeypatch.setattr(
        "execution.entry_gate_hardening.resolve_raw_obi_ratio",
        lambda *a, **k: (-0.20, "quote_proxy"),
    )
    monkeypatch.setattr(
        "execution.pre_entry_regime_veto._resolve_bypass_obi_signal",
        lambda *a, **k: (0.20, "quote_proxy"),
    )
    ok, reason = prv.sovereign_ml_obi_bypass_qualifies(
        "IX.D.DOW.IFM.IP",
        "BUY",
        bid=50000.0,
        offer=50002.0,
        cfg=CFG,
    )
    assert ok is False
    assert "crash" in reason


def test_sovereign_bypass_passes_on_abs_obi_with_side_alignment(monkeypatch):
    import execution.pre_entry_regime_veto as prv

    monkeypatch.setattr(
        "alpha.micro_sniper_ml.evaluate_live_sniper_probability",
        lambda *a, **k: type(
            "R",
            (),
            {
                "p_success": 0.72,
                "approved": True,
                "threshold": 0.68,
                "reason": "ok",
                "features": {},
            },
        )(),
    )
    monkeypatch.setattr(
        "execution.entry_gate_hardening.resolve_raw_obi_ratio",
        lambda *a, **k: (0.18, "quote_proxy"),
    )
    monkeypatch.setattr(
        "execution.pre_entry_regime_veto._resolve_bypass_obi_signal",
        lambda *a, **k: (0.18, "quote_proxy"),
    )
    ok, reason = prv.sovereign_ml_obi_bypass_qualifies(
        "IX.D.DOW.IFM.IP",
        "BUY",
        bid=50000.0,
        offer=50002.0,
        cfg=CFG,
    )
    assert ok is True
    assert "sovereign_ml_obi_bypass" in reason


def test_sovereign_bypass_uses_sniper_obi_velocity(monkeypatch):
    import execution.pre_entry_regime_veto as prv

    monkeypatch.setattr(
        "alpha.micro_sniper_ml.evaluate_live_sniper_probability",
        lambda *a, **k: type(
            "R",
            (),
            {
                "p_success": 0.74,
                "approved": True,
                "threshold": 0.68,
                "reason": "ok",
                "features": {"obi_velocity": 0.22},
            },
        )(),
    )
    monkeypatch.setattr(
        "execution.entry_gate_hardening.resolve_raw_obi_ratio",
        lambda *a, **k: (0.02, "quote_proxy"),
    )
    monkeypatch.setattr(
        "execution.pre_entry_regime_veto._resolve_bypass_obi_signal",
        lambda *a, **k: (0.02, "quote_proxy"),
    )
    ok, reason = prv.sovereign_ml_obi_bypass_qualifies(
        "IX.D.DOW.IFM.IP",
        "BUY",
        bid=50000.0,
        offer=50002.0,
        cfg=CFG,
    )
    assert ok is True
    assert "sniper_obi_velocity" in reason


def test_us_close_slot_passes_when_ml_obi_qualify(monkeypatch):
    import time

    from system.strategy_quality_gate import evaluate_entry_slot_gate

    class _Cfg:
        def get(self, key, default=None):
            if key == "intraday_slots":
                return {
                    "enabled": True,
                    "timezone": "Europe/London",
                    "entry_allowed_slots": ["us_cash"],
                    "slots": [
                        {"id": "us_close", "start": "17:00", "end": "21:00"},
                    ],
                }
            return default

    monkeypatch.setattr(
        "runtime.intraday_slot_tracker.slot_id_for_timestamp",
        lambda ts, cfg: "us_close",
    )
    monkeypatch.setattr(
        "execution.pre_entry_regime_veto.sovereign_ml_obi_bypass_qualifies",
        lambda *a, **k: (True, "sovereign_ml_obi_bypass p=0.70 obi=0.18"),
    )
    ok, reason = evaluate_entry_slot_gate(
        _Cfg(),
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        bid=50000.0,
        offer=50002.0,
    )
    assert ok is True
    assert "sovereign_ml_obi_bypass" in reason
    assert "us_close" in reason


def test_range_bound_still_blocks_without_ml_obi(monkeypatch):
    import execution.pre_entry_regime_veto as prv

    orig = prv._resolve_regime_label
    prv._resolve_regime_label = lambda epic: "RANGE_BOUND"  # type: ignore[assignment]
    monkeypatch.setattr(
        "execution.entry_gate_hardening.evaluate_sniper_ml_gate",
        lambda *a, **k: (False, "sniper_low", 0.40),
    )
    try:
        ok, reason = evaluate_pre_entry_regime_veto(
            "IX.D.DOW.IFM.IP",
            "BUY",
            bid=50000.0,
            offer=50002.0,
            cfg={
                **CFG,
                "pre_entry_regime_veto": {
                    **CFG["pre_entry_regime_veto"],
                    "enforce_spread_pct": False,
                },
            },
        )
        assert ok is False
        assert "not_trending" in reason or "RANGE_BOUND" in reason
    finally:
        prv._resolve_regime_label = orig  # type: ignore[assignment]
