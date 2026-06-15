"""Shared runtime context passed between boot gates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from system.config import Config


@dataclass
class BootContext:
    """Mutable boot artifacts — populated by Gate 1 onward."""

    config: Config | None = None
    raw_config: dict[str, Any] | None = None
    rest_client: Any | None = None
    stream_client: Any | None = None
    orchestrator: Any | None = None
    epics: list[str] = field(default_factory=list)
    account_verify: dict[str, Any] = field(default_factory=dict)
    hydration_detail: dict[str, Any] = field(default_factory=dict)
