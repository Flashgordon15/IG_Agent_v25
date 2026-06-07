"""v25 → v26 feeder event bus (data_lake telemetry)."""

from feeder.event_bus import emit, is_enabled, set_enabled_for_tests

__all__ = ["emit", "is_enabled", "set_enabled_for_tests"]
