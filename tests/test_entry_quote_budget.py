"""Transport-aware entry quote budget — rest_poll must not clamp to 500ms."""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from system.market_data_hub import get_market_data_hub
from system.market_integrity import (
    LIVE_QUOTE_MAX_AGE_SEC,
    effective_entry_quote_budget_sec,
    streaming_transport_is_rest_poll,
)
from trading.trading_loop import TradingLoop


class _StubConfig:
    def __init__(self, data: dict) -> None:
        self._data = data

    def get(self, key, default=None):
        return self._data.get(key, default)


class _FakeLoop:
    _feed_freshness_entry_block = TradingLoop._feed_freshness_entry_block

    def __init__(self, epic: str, cfg: dict) -> None:
        self._epic = epic
        self._config = _StubConfig(cfg)
        self._gate_signal_cache = None


class EntryQuoteBudgetTests(unittest.TestCase):
    def test_rest_poll_uses_config_veto_not_500ms(self) -> None:
        cfg = {
            "streaming_transport": "rest_poll",
            "pricing": {"reference_transport": "yahoo"},
            "feed_quality": {"entry_veto_age_sec": 10.0},
        }
        with patch(
            "feeder.pricing_transport.reference_transport_is_yahoo",
            return_value=True,
        ):
            self.assertTrue(streaming_transport_is_rest_poll(cfg))
            budget = effective_entry_quote_budget_sec(cfg)
        self.assertGreaterEqual(budget, 9.0)
        self.assertLessEqual(budget, 45.0)
        self.assertNotEqual(budget, LIVE_QUOTE_MAX_AGE_SEC)

    def test_lightstreamer_keeps_500ms(self) -> None:
        cfg = {
            "streaming_transport": "lightstreamer",
            "feed_quality": {"entry_veto_age_sec": 25.0},
        }
        with patch(
            "feeder.pricing_transport.reference_transport_is_yahoo",
            return_value=False,
        ):
            self.assertFalse(streaming_transport_is_rest_poll(cfg))
            budget = effective_entry_quote_budget_sec(cfg)
        self.assertEqual(budget, LIVE_QUOTE_MAX_AGE_SEC)

    def test_entry_gate_allows_5s_quote_under_rest_poll(self) -> None:
        epic = "IX.D.DOW.IFM.IP"
        hub = get_market_data_hub()
        hub.invalidate(epic)
        hub.publish(
            epic,
            42000.0,
            42001.0,
            source="yahoo",
            quote_time=time.time() - 5.0,
        )
        cfg = {
            "streaming_transport": "rest_poll",
            "pricing": {"reference_transport": "yahoo"},
            "feed_quality": {
                "entry_gate_enabled": True,
                "entry_veto_age_sec": 10.0,
                "entry_require_non_ig_source": False,
            },
        }
        with patch(
            "system.market_integrity.streaming_transport_is_rest_poll",
            return_value=True,
        ):
            loop = _FakeLoop(epic, cfg)
            self.assertIsNone(loop._feed_freshness_entry_block())


if __name__ == "__main__":
    unittest.main()
