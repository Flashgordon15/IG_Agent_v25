"""Per-epic feed-freshness entry veto — TradingLoop._feed_freshness_entry_block.

Guards the phantom-loss root cause found during the feed-starvation
investigation: entries priced off a stale reference quote. The veto is
entry-only, per-epic, and reads the authoritative hub age.
"""

from __future__ import annotations

import time
import unittest

from system.market_data_hub import get_market_data_hub
from trading.trading_loop import TradingLoop


class _StubConfig:
    """Minimal Config duck-type exposing only .get for the freshness helper."""

    def __init__(self, feed_quality: dict) -> None:
        self._fq = feed_quality

    def get(self, key, default=None):
        if key == "feed_quality":
            return self._fq
        return default


class _FakeLoop:
    """Duck-typed self for TradingLoop._feed_freshness_entry_block."""

    _feed_freshness_entry_block = TradingLoop._feed_freshness_entry_block

    def __init__(self, epic: str, feed_quality: dict) -> None:
        self._epic = epic
        self._config = _StubConfig(feed_quality)
        self._gate_signal_cache = None


_FQ = {
    "entry_gate_enabled": True,
    "entry_veto_age_sec": 25.0,
    "entry_require_non_ig_source": False,
}


class FeedFreshnessGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = get_market_data_hub()
        self.epic = "IX.D.DOW.IFM.IP"
        self.hub.invalidate(self.epic)

    def test_fresh_quote_allows_entry(self) -> None:
        self.hub.publish(
            self.epic, 42000.0, 42001.0, source="yahoo", quote_time=time.time()
        )
        loop = _FakeLoop(self.epic, dict(_FQ))
        self.assertIsNone(loop._feed_freshness_entry_block())

    def test_stale_quote_blocks_entry(self) -> None:
        self.hub.publish(
            self.epic,
            42000.0,
            42001.0,
            source="yahoo",
            quote_time=time.time() - 60.0,
        )
        loop = _FakeLoop(self.epic, dict(_FQ))
        res = loop._feed_freshness_entry_block()
        self.assertIsNotNone(res)
        self.assertFalse(res.passed)
        self.assertIn("feed_stale", res.detail)
        self.assertTrue(res.value.get("feed_stale"))

    def test_no_quote_blocks_entry(self) -> None:
        loop = _FakeLoop(self.epic, dict(_FQ))
        res = loop._feed_freshness_entry_block()
        self.assertIsNotNone(res)
        self.assertFalse(res.passed)
        self.assertIn("no_quote", res.value.get("reason", ""))

    def test_disabled_gate_never_blocks(self) -> None:
        self.hub.publish(
            self.epic,
            42000.0,
            42001.0,
            source="yahoo",
            quote_time=time.time() - 600.0,
        )
        fq = dict(_FQ)
        fq["entry_gate_enabled"] = False
        loop = _FakeLoop(self.epic, fq)
        self.assertIsNone(loop._feed_freshness_entry_block())

    def test_ig_source_blocked_when_required(self) -> None:
        self.hub.publish(
            self.epic, 42000.0, 42001.0, source="ig_rest", quote_time=time.time()
        )
        fq = dict(_FQ)
        fq["entry_require_non_ig_source"] = True
        loop = _FakeLoop(self.epic, fq)
        res = loop._feed_freshness_entry_block()
        self.assertIsNotNone(res)
        self.assertFalse(res.passed)
        self.assertEqual(res.value.get("reason"), "ig_source")

    def test_ig_source_allowed_by_default(self) -> None:
        self.hub.publish(
            self.epic, 42000.0, 42001.0, source="ig_rest", quote_time=time.time()
        )
        loop = _FakeLoop(self.epic, dict(_FQ))
        self.assertIsNone(loop._feed_freshness_entry_block())


if __name__ == "__main__":
    unittest.main()
