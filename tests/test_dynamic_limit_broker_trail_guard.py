"""DynamicLimit broker PUT guards — software-only / circuit-break under REST pressure."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import runtime.dynamic_limit_engine as dle


def setup_function() -> None:
    dle.reset_dynamic_limit_for_tests()


def teardown_function() -> None:
    dle.reset_dynamic_limit_for_tests()


def _arm_track() -> None:
    dle.start_dynamic_limit_engine()
    dle.register_dynamic_limit(
        deal_id="D1",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        entry_level=44000.0,
        limit_pts=2.0,
        size=0.5,
        trail_trigger_ig_pts=1.0,
    )
    with dle._lock:
        track = dle._tracks["D1"]
        track.peak_profit_ig_pts = 5.0
        track.limit_pts = 3.5


def test_software_only_skips_broker_put():
    _arm_track()
    rest = MagicMock()
    dle.bind_rest_client(rest)
    with patch.object(dle, "_broker_puts_allowed", return_value=False):
        dle._maybe_broker_trail("IX.D.DOW.IFM.IP", 44010.0)
    rest.update_position_stops.assert_not_called()


def test_put_failure_trips_circuit_and_stops_retry_spam():
    _arm_track()
    rest = MagicMock()
    dle.bind_rest_client(rest)

    class Boom(Exception):
        status_code = 403

    with (
        patch.object(dle, "_broker_puts_allowed", return_value=True),
        patch(
            "execution.live_broker_order_router.compute_step_trail_update",
            return_value=MagicMock(
                deal_id="D1",
                stop_level=43980.0,
                limit_level=44020.0,
            ),
        ),
        patch(
            "execution.live_broker_order_router.apply_step_trail_put",
            side_effect=Boom("Update stops failed: HTTP 403"),
        ) as apply_put,
    ):
        dle._maybe_broker_trail("IX.D.DOW.IFM.IP", 44010.0)
        dle._maybe_broker_trail("IX.D.DOW.IFM.IP", 44010.0)
        # First failure arms cooldown / circuit — second call must not PUT again.
        assert apply_put.call_count == 1


def test_elevated_rest_blocks_broker_put():
    _arm_track()
    rest = MagicMock()
    dle.bind_rest_client(rest)
    with (
        patch.object(dle, "_omit_broker_trail_configured", return_value=False),
        patch.object(dle, "_software_only_killfile_active", return_value=False),
        patch.object(dle, "_rest_pressure_blocks_put", return_value=True),
    ):
        assert dle._broker_puts_allowed() is False
        dle._maybe_broker_trail("IX.D.DOW.IFM.IP", 44010.0)
    rest.update_position_stops.assert_not_called()
