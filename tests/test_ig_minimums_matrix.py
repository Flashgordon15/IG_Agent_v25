"""IG minimums matrix — mimics broker mins and time-of-day tradeability."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from execution.ig_minimums_matrix import (
    MinimumsVerdict,
    evaluate_epic_minimums,
    session_note_for_status,
    trade_possible,
)


@pytest.mark.parametrize(
    "status,note_fragment",
    [
        ("TRADEABLE", "session_open"),
        ("EDITS_ONLY", "edits_only"),
        ("CLOSED", "session_closed"),
        ("", "unknown"),
    ],
)
def test_session_notes(status: str, note_fragment: str) -> None:
    assert note_fragment in session_note_for_status(status)


def test_trade_possible_requires_tradeable_status() -> None:
    ok, reason = trade_possible(
        market_status="EDITS_ONLY",
        transmit_allowed=True,
        ig_min_deal=0.5,
        guard_size=0.5,
        constraints_ok=True,
    )
    assert ok is False
    assert "EDITS_ONLY" in reason


def test_trade_possible_blocks_below_ig_min() -> None:
    ok, reason = trade_possible(
        market_status="TRADEABLE",
        transmit_allowed=True,
        ig_min_deal=10.0,
        guard_size=2.0,
        constraints_ok=True,
    )
    assert ok is False
    assert "below_ig_min" in reason


def test_trade_possible_passes_valid_dow() -> None:
    ok, reason = trade_possible(
        market_status="TRADEABLE",
        transmit_allowed=True,
        ig_min_deal=0.5,
        guard_size=0.5,
        constraints_ok=True,
    )
    assert ok is True
    assert reason == ""


def test_evaluate_mimics_gold_edits_only_block(monkeypatch) -> None:
    """Gold at night rollover: EDITS_ONLY — no new deals regardless of size."""
    rest = MagicMock()
    rest.fetch_market_constraints.return_value = {
        "market_status": "EDITS_ONLY",
        "min_deal_size": 10.0,
    }
    monkeypatch.setattr(
        "execution.ig_size_validator.fractional_lot_execution_enabled",
        lambda cfg=None: True,
    )
    with patch(
        "execution.broker_tradeability.broker_new_deal_allowed",
        return_value=(False, "market_not_tradeable:EDITS_ONLY"),
    ), patch(
        "execution.broker_epic_resolver.resolve_account_product",
        return_value="SPREADBET",
    ), patch(
        "execution.broker_epic_resolver.resolve_order_epic",
        side_effect=lambda e, **kw: e,
    ):
        v = evaluate_epic_minimums(
            "CS.D.CFPGOLD.CFP.IP",
            cfg=MagicMock(),
            rest_client=rest,
        )
    assert isinstance(v, MinimumsVerdict)
    assert v.trade_possible is False
    assert v.hard_min_deal == 10.0
    assert "EDITS_ONLY" in v.block_reason or "transmit" in v.block_reason


def test_evaluate_mimics_dow_tradeable_at_min(monkeypatch) -> None:
    rest = MagicMock()
    rest.fetch_market_constraints.return_value = {
        "market_status": "TRADEABLE",
        "min_deal_size": 0.5,
    }
    lot = MagicMock(ok=True, size=0.5, rejection_reason="")
    monkeypatch.setattr(
        "execution.ig_size_validator.fractional_lot_execution_enabled",
        lambda cfg=None: True,
    )
    with patch(
        "execution.broker_tradeability.broker_new_deal_allowed",
        return_value=(True, ""),
    ), patch(
        "execution.ig_size_validator.resolve_executable_lot_size",
        return_value=lot,
    ), patch(
        "execution.broker_epic_resolver.resolve_account_product",
        return_value="SPREADBET",
    ), patch(
        "execution.broker_epic_resolver.resolve_order_epic",
        side_effect=lambda e, **kw: e,
    ):
        v = evaluate_epic_minimums(
            "IX.D.DOW.IFM.IP",
            cfg=MagicMock(),
            rest_client=rest,
            probe_size=0.1,
        )
    assert v.trade_possible is True
    assert v.effective_min_deal == 0.5
    assert v.guard_size == 0.5


def test_hard_min_never_below_ig_for_gold_spreadbet(monkeypatch) -> None:
    """Mimic: even if IG reports 2.0, hard floor 10.0 wins for spreadbet gold."""
    from execution.size_floors import effective_min_deal_size

    monkeypatch.setattr(
        "execution.ig_size_validator.fractional_lot_execution_enabled",
        lambda cfg=None: True,
    )
    assert effective_min_deal_size("CS.D.CFPGOLD.CFP.IP", rest_min=2.0) == 10.0


def test_epic_post_block_prevents_trade(monkeypatch) -> None:
    from runtime.broker_reject_guard import epic_post_blocked, record_epic_post_block, reset_broker_reject_guard_for_tests

    reset_broker_reject_guard_for_tests()
    record_epic_post_block("CS.D.EURUSD.CFD.IP", reason="403")
    assert epic_post_blocked("CS.D.EURUSD.CFD.IP") is True
    rest = MagicMock()
    with patch(
        "execution.broker_tradeability.broker_market_status",
        return_value="TRADEABLE",
    ):
        from execution.broker_tradeability import broker_new_deal_allowed

        ok, reason = broker_new_deal_allowed(rest, "CS.D.EURUSD.CFD.IP")
    assert ok is False
    assert "post_blocked" in reason
