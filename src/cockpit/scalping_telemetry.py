"""Flight Deck scalping payload composer — delegates to intelligence modules."""

from __future__ import annotations

from typing import Any

from intelligence.time_decay import scalping_time_decay_telemetry
from intelligence.velocity_filter import scalping_velocity_telemetry


def collect_scalping_telemetry(
    *,
    position_map: dict[str, dict[str, Any]],
    primary_epic: str,
    micro_confidence: float,
) -> dict[str, Any]:
    from system.protective_learning import apply_test_mode_scalping_telemetry

    return apply_test_mode_scalping_telemetry(
        {
            "engine_state": "ENGAGED" if position_map else "STANDBY",
            "primary_epic": str(primary_epic or "").strip(),
            "open_positions": len(position_map),
            "time_decay": scalping_time_decay_telemetry(position_map),
            "tick_velocity": scalping_velocity_telemetry(
                primary_epic,
                micro_confidence=micro_confidence,
            ),
        }
    )
