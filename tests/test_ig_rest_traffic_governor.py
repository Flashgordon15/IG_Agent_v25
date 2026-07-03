"""IG REST traffic governor — demo throughput bypass."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from execution.ig_rest_traffic_governor import (  # noqa: E402
    consume_positions_otc_transmit_slot,
    reset_ig_rest_traffic_governor_for_tests,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_ig_rest_traffic_governor_for_tests()
    yield
    reset_ig_rest_traffic_governor_for_tests()


def test_demo_throughput_raises_traffic_cap() -> None:
    with patch("system.demo_execution_plane.demo_throughput_active", return_value=True):
        with patch(
            "system.config_loader.get_config",
            return_value={
                "demo_throughput_mode": {
                    "bypass_traffic_governor": True,
                    "demo_max_tx_per_60s": 12,
                }
            },
        ):
            for _ in range(12):
                ok, reason = consume_positions_otc_transmit_slot(epic="CS.D.CFPGOLD.CFP.IP")
                assert ok is True
                assert reason == ""
            ok, reason = consume_positions_otc_transmit_slot(epic="CS.D.CFPGOLD.CFP.IP")
            assert ok is False
            assert "traffic governor" in reason


def test_positions_otc_slot_available_peek() -> None:
    with patch("system.demo_execution_plane.demo_throughput_active", return_value=True):
        with patch(
            "system.config_loader.get_config",
            return_value={
                "demo_throughput_mode": {
                    "bypass_traffic_governor": True,
                    "demo_max_tx_per_60s": 2,
                }
            },
        ):
            from execution.ig_rest_traffic_governor import positions_otc_transmit_slot_available

            assert positions_otc_transmit_slot_available() is True
            consume_positions_otc_transmit_slot(epic="E1")
            assert positions_otc_transmit_slot_available() is True
            consume_positions_otc_transmit_slot(epic="E2")
            assert positions_otc_transmit_slot_available() is False


def test_default_cap_blocks_after_three() -> None:
    with patch("system.demo_execution_plane.demo_throughput_active", return_value=False):
        for i in range(3):
            ok, _ = consume_positions_otc_transmit_slot(epic=f"EPIC{i}")
            assert ok is True
        ok, reason = consume_positions_otc_transmit_slot(epic="EPIC4")
        assert ok is False
        assert "traffic governor" in reason
