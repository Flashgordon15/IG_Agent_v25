"""Regime-aware loss patience — execution.loss_patience.

Verifies the desk DEFERS a soft-loss cut only when the feed is fresh, the loss
is inside the soft->cap band, and the regime has not flipped adverse; and CUTS
in every other case. Default (disabled) must never change behaviour.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass

import execution.loss_patience as lp


class _Cfg:
    def __init__(self, block):
        self._b = block

    def get(self, key, default=None):
        return self._b if key == "loss_patience" else default


@dataclass
class _Verdict:
    regime: str
    confidence: float
    momentum_5m: float = 0.0


_ENABLED = {
    "enabled": True,
    "hold_band_ratio": 0.85,
    "max_hold_minutes": 20.0,
    "max_quote_age_sec": 15.0,
    "regime_shift_confidence": 0.55,
    "adverse_momentum_5m": 0.0,
}


class LossPatienceTests(unittest.TestCase):
    def setUp(self):
        self._orig_age = lp._quote_age_sec
        self._orig_micro = lp._microstructure
        # Default: fresh feed, neutral regime.
        lp._quote_age_sec = lambda epic: 3.0
        lp._microstructure = lambda epic: _Verdict("NEUTRAL", 0.4)

    def tearDown(self):
        lp._quote_age_sec = self._orig_age
        lp._microstructure = self._orig_micro

    def _call(self, **kw):
        base = dict(
            epic="IX.D.DOW.IFM.IP",
            direction="BUY",
            pnl_gbp=-2.5,
            soft_loss_gbp=1.68,
            loss_cap_gbp=4.0,
            open_mins=5.0,
            cfg=_Cfg(dict(_ENABLED)),
        )
        base.update(kw)
        return lp.should_hold_losing_position(**base)

    def test_disabled_never_holds(self):
        d = self._call(cfg=_Cfg({"enabled": False}))
        self.assertFalse(d.hold)
        self.assertEqual(d.reason, "disabled")

    def test_neutral_regime_fresh_feed_holds(self):
        d = self._call()
        self.assertTrue(d.hold)
        self.assertIn("mean_reversion_hold", d.reason)

    def test_not_in_loss_band(self):
        d = self._call(pnl_gbp=-1.0)  # above soft
        self.assertFalse(d.hold)
        self.assertEqual(d.reason, "not_in_loss_band")

    def test_near_hard_cap_cuts(self):
        d = self._call(pnl_gbp=-3.6)  # <= -4.0*0.85 = -3.4
        self.assertFalse(d.hold)
        self.assertIn("near_hard_cap", d.reason)

    def test_stale_feed_cuts(self):
        lp._quote_age_sec = lambda epic: 60.0
        d = self._call()
        self.assertFalse(d.hold)
        self.assertIn("stale_feed", d.reason)

    def test_no_quote_cuts(self):
        lp._quote_age_sec = lambda epic: None
        d = self._call()
        self.assertFalse(d.hold)
        self.assertEqual(d.reason, "no_quote")

    def test_too_old_cuts(self):
        d = self._call(open_mins=45.0)
        self.assertFalse(d.hold)
        self.assertIn("max_hold", d.reason)

    def test_max_soft_loss_sec_cuts(self):
        cfg = dict(_ENABLED)
        cfg["max_hold_soft_loss_sec"] = 45
        d = self._call(open_mins=0.8, cfg=_Cfg(cfg))  # 48s
        self.assertFalse(d.hold)
        self.assertIn("max_soft_loss_hold", d.reason)

    def test_max_soft_loss_sec_holds_under_cap(self):
        cfg = dict(_ENABLED)
        cfg["max_hold_soft_loss_sec"] = 45
        d = self._call(open_mins=0.5, cfg=_Cfg(cfg))  # 30s
        self.assertTrue(d.hold)

    def test_adverse_regime_long_cuts(self):
        lp._microstructure = lambda epic: _Verdict("MOMENTUM_DOWN", 0.8)
        d = self._call(direction="BUY")
        self.assertFalse(d.hold)
        self.assertIn("regime_shift", d.reason)

    def test_adverse_regime_short_cuts(self):
        lp._microstructure = lambda epic: _Verdict("MOMENTUM_UP", 0.8)
        d = self._call(direction="SELL")
        self.assertFalse(d.hold)
        self.assertIn("regime_shift", d.reason)

    def test_adverse_regime_low_confidence_holds(self):
        # Momentum down but weak conviction — treat as drift, hold for reversion.
        lp._microstructure = lambda epic: _Verdict("MOMENTUM_DOWN", 0.40)
        d = self._call(direction="BUY")
        self.assertTrue(d.hold)

    def test_momentum_secondary_confirmation_cuts(self):
        cfg = dict(_ENABLED)
        cfg["adverse_momentum_5m"] = 0.001
        lp._microstructure = lambda epic: _Verdict("NEUTRAL", 0.4, momentum_5m=-0.01)
        d = self._call(direction="BUY", cfg=_Cfg(cfg))
        self.assertFalse(d.hold)
        self.assertIn("momentum_shift", d.reason)

    def test_regime_favourable_to_long_holds(self):
        # Momentum UP while long and underwater — reversion in progress, hold.
        lp._microstructure = lambda epic: _Verdict("MOMENTUM_UP", 0.8)
        d = self._call(direction="BUY")
        self.assertTrue(d.hold)


if __name__ == "__main__":
    unittest.main()
