"""
Token-paced dual-desk hydration stagger — CFD must fully ready before SB.

Why stagger exists (rest_pressure / init burst isolation):
  Concurrent twin spawn on rest_poll bursts IG auth + OHLC + SHM bind on both
  accounts at once, driving REST_PRESSURE_HIGH and twin death. CFD Sniper on
  :8080 must authenticate and hydrate SHM arrays completely, then we hold a
  minimum post-ready window before MACRO_SENTINEL on :8081 fires.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable

# Minimum delay AFTER CFD reports healthy/hydrated before SB spawn.
MIN_POST_READY_STAGGER_SEC = 4.0
DEFAULT_CFD_READY_TIMEOUT_SEC = 180.0
DEFAULT_POLL_SEC = 1.0


def cfd_engine_ready(health_payload: dict[str, Any] | None) -> bool:
    """True when CFD health proves auth + trading path hydration complete."""
    if not isinstance(health_payload, dict):
        return False
    if health_payload.get("trading_healthy") is True:
        return True
    if health_payload.get("ok") is True and health_payload.get("trade_ready") is True:
        return True
    # Bound + answering is insufficient alone — require explicit readiness.
    return False


def sb_spawn_allowed(
    *,
    cfd_ready: bool,
    cfd_ready_at_mono: float | None,
    now_mono: float | None = None,
    min_post_ready_sec: float = MIN_POST_READY_STAGGER_SEC,
) -> bool:
    """SB may spawn only after CFD ready AND ≥ min_post_ready_sec elapsed."""
    if not cfd_ready or cfd_ready_at_mono is None:
        return False
    now = float(now_mono if now_mono is not None else time.monotonic())
    stagger = max(float(MIN_POST_READY_STAGGER_SEC), float(min_post_ready_sec))
    return (now - float(cfd_ready_at_mono)) >= stagger


def plan_sb_spawn(
    *,
    cfd_ready: bool,
    cfd_ready_at_mono: float | None,
    now_mono: float | None = None,
    min_post_ready_sec: float = MIN_POST_READY_STAGGER_SEC,
) -> dict[str, Any]:
    """Pure sequencing plan for tests + supervisor scripting."""
    now = float(now_mono if now_mono is not None else time.monotonic())
    stagger = max(float(MIN_POST_READY_STAGGER_SEC), float(min_post_ready_sec))
    if not cfd_ready or cfd_ready_at_mono is None:
        return {
            "action": "wait_cfd",
            "sb_spawn_allowed": False,
            "cfd_ready": False,
            "remaining_stagger_sec": stagger,
            "min_post_ready_sec": stagger,
            "reason": "cfd_not_ready",
        }
    elapsed = max(0.0, now - float(cfd_ready_at_mono))
    remaining = max(0.0, stagger - elapsed)
    allowed = remaining <= 0.0
    return {
        "action": "spawn_sb" if allowed else "wait_stagger",
        "sb_spawn_allowed": allowed,
        "cfd_ready": True,
        "elapsed_since_cfd_ready_sec": round(elapsed, 3),
        "remaining_stagger_sec": round(remaining, 3),
        "min_post_ready_sec": stagger,
        "reason": (
            "rest_pressure_init_burst_isolation"
            if allowed
            else "post_ready_stagger_window"
        ),
    }


def fetch_health_payload(
    port: int,
    *,
    timeout: float = 2.0,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any] | None:
    """GET /api/health — injectable opener for unit tests."""
    url = f"http://127.0.0.1:{int(port)}/api/health"
    open_fn = opener or urllib.request.urlopen
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with open_fn(req, timeout=float(timeout)) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
            return body if isinstance(body, dict) else None
    except (
        OSError,
        urllib.error.URLError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ):
        return None


def wait_cfd_ready_then_stagger(
    *,
    port: int = 8080,
    ready_timeout_sec: float | None = None,
    min_post_ready_sec: float | None = None,
    poll_sec: float = DEFAULT_POLL_SEC,
    sleep_fn: Callable[[float], None] | None = None,
    monotonic_fn: Callable[[], float] | None = None,
    health_fetcher: Callable[[int], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """
    Block until CFD healthy/hydrated, then sleep ≥ min_post_ready_sec, then allow SB.

    Returns a plan dict with ``sb_spawn_allowed`` True on success.
    """
    _sleep = sleep_fn or time.sleep
    _mono = monotonic_fn or time.monotonic
    _fetch = health_fetcher or (lambda p: fetch_health_payload(p))

    timeout = float(
        ready_timeout_sec
        if ready_timeout_sec is not None
        else os.environ.get("IG_V32_CFD_READY_TIMEOUT_SEC", DEFAULT_CFD_READY_TIMEOUT_SEC)
    )
    stagger = max(
        float(MIN_POST_READY_STAGGER_SEC),
        float(
            min_post_ready_sec
            if min_post_ready_sec is not None
            else os.environ.get(
                "IG_V32_SB_POST_READY_STAGGER_SEC", MIN_POST_READY_STAGGER_SEC
            )
        ),
    )

    deadline = _mono() + max(1.0, timeout)
    cfd_ready_at: float | None = None
    last_payload: dict[str, Any] | None = None

    while _mono() < deadline:
        last_payload = _fetch(int(port))
        if cfd_engine_ready(last_payload):
            cfd_ready_at = _mono()
            break
        _sleep(max(0.05, float(poll_sec)))

    if cfd_ready_at is None:
        return {
            "action": "timeout",
            "sb_spawn_allowed": False,
            "cfd_ready": False,
            "remaining_stagger_sec": stagger,
            "min_post_ready_sec": stagger,
            "reason": "cfd_ready_timeout",
            "last_health": last_payload,
        }

    # Post-ready isolation window — rest_pressure / init burst isolation.
    _sleep(stagger)
    plan = plan_sb_spawn(
        cfd_ready=True,
        cfd_ready_at_mono=cfd_ready_at,
        now_mono=_mono(),
        min_post_ready_sec=stagger,
    )
    plan["last_health"] = last_payload
    plan["stagger_slept_sec"] = stagger
    return plan
