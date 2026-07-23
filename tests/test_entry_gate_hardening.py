"""Fail-closed spread + OBI entry hardening."""

from __future__ import annotations

from types import SimpleNamespace

from execution.entry_gate_hardening import (
    evaluate_entry_hardening,
    evaluate_obi_entry_filter,
    evaluate_spread_hard_veto,
)


CFG = {
    "feed_quality": {
        "enabled": True,
        "max_spread_pts": 3.0,
        "spread_hard_veto": True,
    },
    "obi_filter": {
        "enabled": True,
        "min_abs_ratio": 0.15,
        "require_align": True,
        "fail_closed_on_neutral": False,
    },
}


def test_spread_hard_veto_blocks_wide_book(monkeypatch):
    monkeypatch.setattr(
        "execution.entry_gate_hardening._hub_quote",
        lambda epic: SimpleNamespace(bid=100.0, offer=104.0),
    )
    ok, reason, spread = evaluate_spread_hard_veto("IX.D.DOW.IFM.IP", cfg=CFG)
    assert ok is False
    assert spread == 4.0
    assert "spread_hard_veto" in reason


def test_spread_fail_closed_without_quote(monkeypatch):
    monkeypatch.setattr(
        "execution.entry_gate_hardening._hub_quote",
        lambda epic: None,
    )
    ok, reason, _ = evaluate_spread_hard_veto("IX.D.DOW.IFM.IP", cfg=CFG)
    assert ok is False
    assert "fail_closed" in reason


def test_obi_blocks_buy_into_crash(monkeypatch):
    monkeypatch.setattr(
        "execution.entry_gate_hardening._obi_from_microkernel",
        lambda epic: (-0.42, False),
    )
    ok, reason, ratio = evaluate_obi_entry_filter(
        "IX.D.DOW.IFM.IP", "BUY", cfg=CFG
    )
    assert ok is False
    assert ratio == -0.42
    assert "crash" in reason or "not_aligned" in reason


def test_obi_allows_buy_with_support(monkeypatch):
    monkeypatch.setattr(
        "execution.entry_gate_hardening._obi_from_microkernel",
        lambda epic: (0.35, True),
    )
    ok, reason, ratio = evaluate_obi_entry_filter(
        "IX.D.DOW.IFM.IP", "BUY", cfg=CFG
    )
    assert ok is True
    assert ratio == 0.35


def test_obi_depthless_neutral_does_not_permanent_block(monkeypatch):
    """Yahoo/rest_poll OBI≈0 + aligned=False must fall through to quote proxy."""
    monkeypatch.setattr(
        "execution.entry_gate_hardening._obi_from_microkernel",
        lambda epic: (0.0, False),
    )
    monkeypatch.setattr(
        "execution.entry_gate_hardening._obi_proxy_from_quote",
        lambda epic, quote: 0.0,
    )
    ok, reason, ratio = evaluate_obi_entry_filter(
        "IX.D.DOW.IFM.IP", "BUY", cfg=CFG
    )
    assert ok is True
    assert "not_aligned" not in reason
    assert ratio == 0.0


def test_obi_fail_closed_neutral_starves_depthless_proxy_zero(monkeypatch):
    """fail_closed_on_neutral=true + proxy ratio=0 blocks every side — soak killer on Mini."""
    cfg = {
        **CFG,
        "obi_filter": {
            **CFG["obi_filter"],
            "fail_closed_on_neutral": True,
            "min_abs_ratio": 0.22,
        },
    }
    monkeypatch.setattr(
        "execution.entry_gate_hardening._obi_from_microkernel",
        lambda epic: (0.0, False),
    )
    monkeypatch.setattr(
        "execution.entry_gate_hardening._obi_proxy_from_quote",
        lambda epic, quote: 0.0,
    )
    ok, reason, ratio = evaluate_obi_entry_filter(
        "IX.D.DOW.IFM.IP", "BUY", cfg=cfg
    )
    assert ok is False
    assert "obi_proxy_not_supportive" in reason
    assert ratio == 0.0


def test_obi_informative_misalign_still_blocks(monkeypatch):
    monkeypatch.setattr(
        "execution.entry_gate_hardening._obi_from_microkernel",
        lambda epic: (0.22, False),
    )
    ok, reason, ratio = evaluate_obi_entry_filter(
        "IX.D.DOW.IFM.IP", "BUY", cfg=CFG
    )
    assert ok is False
    assert "not_aligned" in reason
    assert ratio == 0.22


def test_hardening_exception_fail_closed(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("telemetry_down")

    monkeypatch.setattr(
        "execution.entry_gate_hardening.evaluate_spread_hard_veto",
        _boom,
    )
    ok, reason = evaluate_entry_hardening("IX.D.DOW.IFM.IP", "BUY", cfg=CFG)
    assert ok is False
    assert "fail_closed" in reason
