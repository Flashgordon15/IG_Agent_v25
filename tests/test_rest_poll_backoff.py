"""Tests for rest_poll adaptive back-off."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ig_api.exceptions import IGAPIError, RateLimitError
from ig_api.rest_poll_backoff import (
    RestPollBackoff,
    is_connection_timeout,
    is_http_429,
    is_retryable_poll_error,
)
from ig_api.streaming_client import ConnectionState, IGStreamingClient


class RestPollBackoffTests(unittest.TestCase):
    def test_429_backoff_sequence(self) -> None:
        bo = RestPollBackoff(5.0)
        wait1, label1 = bo.on_retryable_error(RateLimitError("limited"))
        self.assertEqual(wait1, 2.0)
        self.assertEqual(label1, "HTTP 429")
        wait2, _ = bo.on_retryable_error(RateLimitError("limited"))
        self.assertEqual(wait2, 4.0)
        wait3, _ = bo.on_retryable_error(RateLimitError("limited"))
        self.assertEqual(wait3, 4.0)
        self.assertEqual(bo.on_success(), 5.0)
        self.assertEqual(bo.strike, 0)

    def test_timeout_is_retryable(self) -> None:
        self.assertTrue(is_connection_timeout(TimeoutError()))
        self.assertTrue(is_retryable_poll_error(TimeoutError()))

    def test_http_429_detection(self) -> None:
        self.assertTrue(is_http_429(RateLimitError("x")))
        self.assertTrue(is_http_429(IGAPIError("x", status_code=429)))


class StreamingClientBackoffTests(unittest.TestCase):
    def test_retryable_error_does_not_reset_first_tick(self) -> None:
        client = IGStreamingClient(
            MagicMock(),
            MagicMock(is_valid=True),
            rest_client=MagicMock(),
            poll_interval_seconds=1.0,
        )
        client._first_tick_received = True
        client._set_state(ConnectionState.CONNECTED)
        client._running = True
        backoff = RestPollBackoff(1.0)
        mgr = MagicMock()
        mgr.is_rest_blocked.return_value = False
        mgr.is_stream_blocked.return_value = False

        with patch("ig_api.streaming_client.time.sleep") as mock_sleep:
            client._handle_retryable_poll_error(RateLimitError("429"), backoff, mgr)

        mock_sleep.assert_called_once_with(2.0)
        self.assertTrue(client._first_tick_received)
        self.assertEqual(client.state.value, "connected")


if __name__ == "__main__":
    unittest.main()
