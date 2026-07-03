"""Tests for multimarket_eval snapshot."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from analytics.multimarket_eval import (
    get_multimarket_eval_snapshot,
    reset_multimarket_eval_for_tests,
)
from system.market_data_hub import NIGHT_MATRIX_EPICS


@pytest.fixture(autouse=True)
def _reset():
    reset_multimarket_eval_for_tests()
    yield
    reset_multimarket_eval_for_tests()


def test_refresh_populates_all_markets():
    hub = MagicMock()
    hub.get_snapshot.return_value = MagicMock(
        age_seconds=lambda: 2.0, bid=100.0, offer=100.1, source="yahoo"
    )
    with patch("analytics.multimarket_eval.get_market_data_hub", return_value=hub):
        with patch("analytics.multimarket_eval._shadow_signal_counts", return_value={}):
            with patch("analytics.multimarket_eval._lifecycle_by_epic", return_value={}):
                with patch("analytics.multimarket_eval._stops_limits_by_epic", return_value={}):
                    with patch("analytics.multimarket_eval._pnl_by_epic", return_value={}):
                        with patch(
                            "system.feeds.data_feed_orchestrator.get_data_feed_state",
                            return_value={"health": "ok", "primary_feed": "yahoo", "fresh_count": 7, "total_epics": 7},
                        ):
                            with patch(
                                "runtime.dual_core_execution.get_rotation_state",
                                return_value={"active_stack_epics": list(NIGHT_MATRIX_EPICS[:2]), "active_instruments": []},
                            ):
                                with patch("runtime.dual_core_execution._ticks_per_minute", return_value=5):
                                    with patch("runtime.dual_core_execution._snapshots", {}):
                                        from analytics import multimarket_eval

                                        multimarket_eval._refresh_snapshot()
    snap = get_multimarket_eval_snapshot()
    assert snap["ok"] is True
    assert len(snap["markets"]) == len(NIGHT_MATRIX_EPICS)
    assert snap["feed_summary"]["health"] == "ok"


def test_get_snapshot_is_copy():
    from analytics import multimarket_eval

    multimarket_eval._snapshot["markets"] = [{"epic": "test"}]
    a = get_multimarket_eval_snapshot()
    b = get_multimarket_eval_snapshot()
    a["markets"].append({"epic": "mutate"})
    assert len(b["markets"]) == 1
