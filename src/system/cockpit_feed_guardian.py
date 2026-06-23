"""
Cockpit feed linkage guardian — stall detection + self-heal decisions.

Separates SHM publish heartbeat (write_seq) from tick velocity so quiet markets
do not false-positive. Used by desktop_cockpit and the in-agent heal endpoint.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# SHM publisher refreshes every 500ms — 3s without write_seq advance = stall.
PUBLISH_STALL_SEC = 3.0
HEAL_COOLDOWN_SEC = 30.0
AGENT_START_COOLDOWN_SEC = 120.0

HEAL_NONE = "none"
HEAL_FEED_RESET = "feed_reset"
HEAL_START_AGENT = "start_agent"
HEAL_USE_API = "use_api"
HEAL_RELAUNCH_COCKPIT = "relaunch_cockpit"


@dataclass
class FeedWatchState:
    last_write_seq: int | None = None
    last_ticks: int | None = None
    last_live: int | None = None
    last_publish_change_mono: float = field(default_factory=time.monotonic)
    last_tick_change_mono: float = field(default_factory=time.monotonic)
    last_heal_mono: float = 0.0
    last_agent_start_mono: float = 0.0
    heal_count: int = 0


def update_feed_watch(
    watch: FeedWatchState,
    view: dict[str, Any] | None,
    *,
    now: float | None = None,
) -> FeedWatchState:
    """Advance heartbeat trackers from the latest SHM or API view."""
    t = float(now if now is not None else time.monotonic())
    if not view:
        return watch
    write_seq = int(view.get("write_seq") or 0)
    ticks = int(view.get("ticks_cached") or 0)
    live = int(view.get("live_ram_ticks") or 0)

    if watch.last_write_seq is None or write_seq != watch.last_write_seq:
        watch.last_write_seq = write_seq
        watch.last_publish_change_mono = t
    if (
        watch.last_ticks is None
        or watch.last_live is None
        or ticks != watch.last_ticks
        or live != watch.last_live
    ):
        watch.last_ticks = ticks
        watch.last_live = live
        watch.last_tick_change_mono = t
    return watch


def publish_frozen_sec(watch: FeedWatchState, *, now: float | None = None) -> float:
    t = float(now if now is not None else time.monotonic())
    return max(0.0, t - float(watch.last_publish_change_mono or t))


def tick_frozen_sec(watch: FeedWatchState, *, now: float | None = None) -> float:
    t = float(now if now is not None else time.monotonic())
    return max(0.0, t - float(watch.last_tick_change_mono or t))


def is_publish_stalled(
    watch: FeedWatchState,
    view: dict[str, Any] | None,
    *,
    gate: dict[str, Any] | None = None,
    now: float | None = None,
) -> tuple[bool, float, str]:
    """
    True when SHM publish heartbeat or server velocity watchdog reports stall.

    Returns (stalled, frozen_sec, reason).
    """
    t = float(now if now is not None else time.monotonic())
    if view and bool(view.get("stall_active")):
        return True, publish_frozen_sec(watch, now=t), "server_velocity_stall"

    dv = (gate or {}).get("data_velocity") or {}
    if bool(dv.get("stall_active")):
        frozen = float(dv.get("frozen_sec") or publish_frozen_sec(watch, now=t))
        return True, frozen, "api_velocity_stall"

    frozen_pub = publish_frozen_sec(watch, now=t)
    if view is None:
        return False, frozen_pub, ""
    if int(view.get("write_seq") or 0) <= 0:
        return False, frozen_pub, ""
    if frozen_pub >= PUBLISH_STALL_SEC:
        return True, frozen_pub, "write_seq_stalled"
    return False, frozen_pub, ""


def pid_mismatch(
    view: dict[str, Any] | None,
    health: dict[str, Any],
) -> bool:
    api_pid = int(health.get("agent_pid") or 0)
    if not view or api_pid <= 0:
        return False
    shm_pid = int(view.get("agent_pid") or 0)
    return shm_pid > 0 and shm_pid != api_pid


def decide_heal_action(
    *,
    link_state: str,
    stalled: bool,
    stall_reason: str,
    health: dict[str, Any],
    view: dict[str, Any] | None,
    watch: FeedWatchState,
    now: float | None = None,
) -> tuple[str, str]:
    """
    Pick a self-heal action with cooldown.

    Returns (action, detail).
    """
    t = float(now if now is not None else time.monotonic())
    api_pid = int(health.get("agent_pid") or 0)
    api_ready = bool((health.get("boot_metrics") or {}).get("ready"))
    api_alive = bool(health.get("agent_alive")) or api_pid > 0

    if link_state in ("STALE_SHM", "AGENT_OFFLINE", "MANUAL_STOP"):
        if link_state == "MANUAL_STOP":
            return HEAL_NONE, "manual_stop active"
        if t - watch.last_agent_start_mono < AGENT_START_COOLDOWN_SEC:
            return HEAL_NONE, "agent start cooldown"
        if not api_alive or not api_ready:
            watch.last_agent_start_mono = t
            return HEAL_START_AGENT, link_state
        return HEAL_USE_API, "stale shm — api fallback"

    if pid_mismatch(view, health):
        if api_alive and api_ready:
            return HEAL_USE_API, f"pid mismatch shm={view.get('agent_pid')} api={api_pid}"
        return HEAL_START_AGENT, "pid mismatch — agent restarting"

    if not stalled:
        return HEAL_NONE, ""

    if t - watch.last_heal_mono < HEAL_COOLDOWN_SEC:
        return HEAL_NONE, "heal cooldown"

    watch.last_heal_mono = t
    watch.heal_count += 1

    if not api_alive or not api_ready:
        watch.last_agent_start_mono = t
        return HEAL_START_AGENT, stall_reason

    return HEAL_FEED_RESET, stall_reason


def record_heal_applied(watch: FeedWatchState) -> None:
    watch.last_heal_mono = time.monotonic()
    watch.heal_count += 1
