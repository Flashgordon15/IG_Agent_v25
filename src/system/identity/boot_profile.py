"""
Immutable boot profiles — single source of truth for parallel track deployment.

Replaces ad-hoc ``daemon_cycle_kernel`` environment mutation for orchestrated
Live Vanguard (:8080) and Shadow Simulator (:9199) tracks.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from system.identity.app_identity import RuntimeIdentity

TrackKind = Literal["live", "shadow"]


@dataclass(frozen=True)
class BootProfile:
    """Immutable runtime profile — ports, locks, and execution plane flags."""

    track: TrackKind
    api_port: int
    cockpit_port: int
    lock_basename: str
    apex_runtime_mode: str
    node_profile: str
    ig_agent_mode: str
    mock_feed: bool
    allow_mock_trading: bool
    parallel_track: str
    daemon_cycle_sec: int | None = None

    @property
    def log_path(self) -> str:
        if self.track == "live":
            return "/tmp/ig_agent.live.log"
        return "/tmp/ig_agent.shadow.log"

    def apply_to_environ(self) -> None:
        """Publish profile to ``os.environ`` — idempotent boot law."""
        os.environ["IG_PARALLEL_TRACK"] = self.parallel_track
        os.environ["IG_ORCHESTRATOR_CHILD"] = "1"
        os.environ["IG_APEX_RUNTIME_MODE"] = self.apex_runtime_mode
        os.environ["IG_API_PORT"] = str(int(self.api_port))
        os.environ["IG_NODE_PROFILE"] = self.node_profile
        os.environ["NODE_ENV"] = self.node_profile
        os.environ["IG_AGENT_MODE"] = self.ig_agent_mode
        os.environ["IG_MOCK_FEED"] = "1" if self.mock_feed else "0"
        os.environ["IG_AGENT_SKIP_ORPHAN_KILL"] = "1"
        os.environ.pop("IG_TEST_HARNESS", None)
        if self.allow_mock_trading:
            os.environ["IG_ALLOW_MOCK_TRADING"] = "1"
        else:
            os.environ.pop("IG_ALLOW_MOCK_TRADING", None)
        if self.track == "shadow":
            os.environ["IG_AGENT_SKIP_DEPLOY_CHECK"] = "1"
            os.environ["IG_HISTORICAL_REPLAY_LOOP"] = "1"
        else:
            os.environ.pop("IG_HISTORICAL_REPLAY_LOOP", None)
        if self.daemon_cycle_sec is not None:
            os.environ["IG_DAEMON_CYCLE_SEC"] = str(int(self.daemon_cycle_sec))
        os.environ["IG_KERNEL_ARMED"] = "1"

    @classmethod
    def for_live(cls, *, cycle_sec: int | None = 900) -> BootProfile:
        port = 8080
        return cls(
            track="live",
            api_port=port,
            cockpit_port=8787,
            lock_basename=RuntimeIdentity.lock_basename(port),
            apex_runtime_mode="PRODUCTION",
            node_profile="production",
            ig_agent_mode="DEMO",
            mock_feed=False,
            allow_mock_trading=False,
            parallel_track="live",
            daemon_cycle_sec=cycle_sec,
        )

    @classmethod
    def for_shadow(cls, *, cycle_sec: int | None = 900) -> BootProfile:
        port = 9199
        return cls(
            track="shadow",
            api_port=port,
            cockpit_port=9191,
            lock_basename=RuntimeIdentity.lock_basename(port),
            apex_runtime_mode="SHADOW",
            node_profile="shadow",
            ig_agent_mode="SHADOW",
            mock_feed=True,
            allow_mock_trading=True,
            parallel_track="shadow",
            daemon_cycle_sec=cycle_sec,
        )

    @classmethod
    def resolve_current(cls) -> BootProfile:
        track = os.environ.get("IG_PARALLEL_TRACK", "").strip().lower()
        port_raw = os.environ.get("IG_API_PORT", "").strip()
        port = int(port_raw) if port_raw.isdigit() else (9199 if track == "shadow" else 8080)
        if track == "shadow" or port == 9199:
            return cls.for_shadow()
        return cls.for_live()


def apply_boot_profile(profile: BootProfile) -> BootProfile:
    profile.apply_to_environ()
    return profile
