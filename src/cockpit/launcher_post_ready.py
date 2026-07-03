"""Launcher Stage 6 — post-G5 execution plane acceptance (desktop + agent_start)."""

from __future__ import annotations

from typing import Any


def post_ready_execution_acceptable(
    *,
    health_light: dict[str, Any] | None,
    boot_status: dict[str, Any] | None = None,
    boot_tier: str = "",
) -> tuple[bool, str]:
    """
    Return (True, reason) when Stage 6 wait can exit.

    Strict path: stacked sweep + exec loop, trade_ready, or armed routes.
    Amber path: G5 passed with API live — execution plane hydrates in background.
    """
    hl = health_light if isinstance(health_light, dict) else {}
    boot = boot_status if isinstance(boot_status, dict) else {}
    tier = str(boot_tier or "").strip().lower()

    rs = hl.get("routing_state") or {}
    armed = int(rs.get("armed") or 0)
    sweep_alive = bool(hl.get("stacked_sweep_alive"))
    exec_active = bool(hl.get("execution_loop_active"))
    trade_ready = bool(boot.get("trade_ready"))

    if sweep_alive and exec_active:
        return True, "execution_loop_active"
    if trade_ready:
        return True, "trade_ready"
    if armed > 0:
        return True, f"routes_armed={armed}"
    if sweep_alive:
        return True, "stacked_sweep_alive"

    if tier == "green":
        return True, "boot_tier=green"

    if tier == "amber":
        if hl.get("agent_online") is not False:
            phase = str(boot.get("phase") or boot.get("stage") or "").upper()
            if phase in ("G5", "READY", "G", "B"):
                return True, "amber_post_g5"
            if bool(hl.get("ig_available")) or bool(hl.get("yahoo_available")):
                return True, "amber_feeds_hydrating"
            return True, "amber_api_live"

    return False, "pending"


def launcher_stage_visual_state(
    *,
    stage: str,
    detail: str,
    boot_tier: str = "",
    status: str = "",
) -> str:
    """Map launcher telemetry to splash checklist state: complete | warming | active."""
    stage_key = str(stage or "").strip().lower()
    detail_l = str(detail or "").lower()
    status_l = str(status or "").lower()
    tier = str(boot_tier or "").strip().lower()

    if stage_key in ("ready", "gui", "failed"):
        return "complete" if stage_key != "failed" else "active"
    if "complete" in detail_l or "complete" in status_l:
        return "complete"
    if stage_key == "post_ready" and detail_l.startswith("execution plane"):
        return "complete"
    if tier == "amber" or stage_key in ("g5", "post_ready", "warmup"):
        return "warming"
    return "active"
