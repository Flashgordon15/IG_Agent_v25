"""Loss classifier must not launder APP defects into LOGIC.

Before provenance existed the 2026-07-24 autopsy reported 21 LOGIC losses. All
but two were broker-closed trades where our own risk stack never fired, or rows
whose hold/ML stamps were artifacts. Those must classify as APP or UNKNOWN.
"""

from __future__ import annotations

import unittest

from diagnostics.trade_lifecycle_witness import (
    PolicyContext,
    TradeLifecycle,
    classify_loss,
    classify_policy_breaches,
)
from diagnostics.stamp_provenance import (
    EXIT_AUTHORITY_AGENT,
    EXIT_AUTHORITY_BROKER,
    classify_exit_authority,
)

DOW = "IX.D.DOW.IFM.IP"


def _lc(**kw) -> TradeLifecycle:
    lc = TradeLifecycle(deal_id=kw.pop("deal_id", "DIAAAATEST"))
    lc.epic = kw.pop("epic", DOW)
    lc.pnl_gbp = kw.pop("pnl_gbp", -5.0)
    lc.account_id = kw.pop("account_id", "Z6BAH3")
    lc.product_type = kw.pop("product_type", "SPREADBET")
    for k, v in kw.items():
        setattr(lc, k, v)
    lc.exit_authority = classify_exit_authority(
        exit_reason=lc.exit_reason or "", engine_origin=lc.engine_origin or ""
    )
    return lc


class BrokerExitIsAppTests(unittest.TestCase):
    def setUp(self) -> None:
        # A2 pause off so it cannot mask the result we are asserting.
        self.ctx = PolicyContext(a2_cfd_paused=False)

    def test_broker_closed_trade_is_app_not_logic(self) -> None:
        lc = _lc(
            exit_reason="broker_attached",
            engine_origin="broker_attached",
            hold_sec=0.0,
            ml_score_at_entry=0.7773,
        )
        cls, reason = classify_loss(lc, self.ctx)
        self.assertEqual(cls, "APP")
        self.assertIn("SUPERVISION_GAP", reason)

    def test_agent_soft_loss_exit_is_logic(self) -> None:
        lc = _lc(
            exit_reason="open_position_actions:soft_loss breach -4.85 <= -2.95",
            engine_origin="MACRO_SENTINEL",
            hold_sec=210.0,
            ml_score_at_entry=0.4356,
        )
        cls, _reason = classify_loss(lc, self.ctx)
        self.assertEqual(cls, "LOGIC")


class UnmeasuredHoldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ctx = PolicyContext(a2_cfd_paused=False, path_a_claimed=True)

    def test_sync_artifact_zero_hold_is_not_micro_masquerade(self) -> None:
        """A collapsed timestamp must not be read as a zero-second scalp."""
        lc = _lc(
            exit_reason="soft_loss",
            engine_origin="broker_attached",
            hold_sec=0.0,
            ml_score_at_entry=0.7773,
        )
        # Agent token wins, so authority is agent — the question is the hold.
        self.assertEqual(lc.exit_authority, EXIT_AUTHORITY_AGENT)
        _cls, reason = classify_loss(lc, self.ctx)
        self.assertNotIn("micro masquerade", reason)

    def test_measured_short_hold_still_flags_micro_masquerade(self) -> None:
        lc = _lc(
            exit_reason="micro_gbp_exit:soft_loss",
            engine_origin="QUANT_SNIPER",
            hold_sec=3.0,
            ml_score_at_entry=0.7773,
        )
        cls, reason = classify_loss(lc, self.ctx)
        self.assertEqual(cls, "APP")
        self.assertIn("micro masquerade", reason)


class UntrustedStampTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ctx = PolicyContext(a2_cfd_paused=False)

    def test_threshold_ml_stamp_alone_cannot_prove_logic(self) -> None:
        """0.68 is the gate threshold, not a prediction — no LOGIC verdict."""
        lc = _lc(
            exit_reason="",
            engine_origin="broker_attached",
            hold_sec=None,
            ml_score_at_entry=0.68,
            evidence_gaps=["missing_hold_sec"],
        )
        cls, _reason = classify_loss(lc, self.ctx)
        self.assertIn(cls, {"APP", "UNKNOWN"})
        self.assertNotEqual(cls, "LOGIC")


class RiskStackDidNotCutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ctx = PolicyContext(
            a2_cfd_paused=False, soft_loss_gbp=2.95, soft_loss_overrun_ratio=1.5
        )

    def _codes(self, lc: TradeLifecycle) -> set[str]:
        return {b.code for b in classify_policy_breaches(lc, self.ctx)}

    def test_broker_close_far_beyond_soft_loss_is_flagged(self) -> None:
        lc = _lc(
            pnl_gbp=-9.09,
            exit_reason="broker_attached",
            engine_origin="broker_attached",
        )
        self.assertIn("RISK_STACK_DID_NOT_CUT", self._codes(lc))

    def test_broker_close_within_tolerance_is_not_flagged(self) -> None:
        lc = _lc(
            pnl_gbp=-3.10,
            exit_reason="broker_attached",
            engine_origin="broker_attached",
        )
        self.assertNotIn("RISK_STACK_DID_NOT_CUT", self._codes(lc))

    def test_agent_closed_trade_is_never_flagged(self) -> None:
        """Our stack cutting deep is a sizing question, not a supervision gap."""
        lc = _lc(
            pnl_gbp=-9.09,
            exit_reason="open_position_actions:soft_loss breach -9.09 <= -2.95",
            engine_origin="MACRO_SENTINEL",
        )
        self.assertEqual(lc.exit_authority, EXIT_AUTHORITY_AGENT)
        self.assertNotIn("RISK_STACK_DID_NOT_CUT", self._codes(lc))

    def test_winner_is_never_flagged(self) -> None:
        lc = _lc(
            pnl_gbp=12.0,
            exit_reason="broker_attached",
            engine_origin="broker_attached",
        )
        self.assertEqual(lc.exit_authority, EXIT_AUTHORITY_BROKER)
        self.assertNotIn("RISK_STACK_DID_NOT_CUT", self._codes(lc))


if __name__ == "__main__":
    unittest.main()
