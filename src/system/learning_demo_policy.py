"""Profile B — Learning Demo policy (throughput + labelled relaxations + integrity)."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from typing import Any

from system.paths import project_root


@lru_cache(maxsize=1)
def _policy_block() -> dict[str, Any]:
    try:
        from system.config_loader import get_config

        raw = get_config().get("learning_demo_mode") or {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


_effective_policy_cache: dict[str, Any] | None = None


def reset_learning_demo_policy_cache_for_tests() -> None:
    _policy_block.cache_clear()


def reset_effective_policy_snapshot_cache() -> None:
    global _effective_policy_cache
    _effective_policy_cache = None


def learning_demo_enabled() -> bool:
    return bool(_policy_block().get("enabled"))


def learning_demo_profile() -> str:
    return str(_policy_block().get("profile") or "B")


def learning_demo_policy_id() -> str:
    return str(_policy_block().get("policy_id") or "learning_demo_v1")


def learning_demo_integrity_enabled() -> bool:
    block = _policy_block()
    if not block.get("enabled"):
        return False
    return bool(block.get("require_gate_sourced_submit", True))


def v26_gate_relaxations_suppressed() -> bool:
    """When Learning Demo is active, v26 gate_relaxations must not double-relax."""
    return learning_demo_enabled() and bool(
        _policy_block().get("suppress_v26_gate_relaxations", True)
    )


def config_hash_short() -> str:
    """Stable fingerprint of merged primary config for demo session labelling."""
    try:
        from system.config_loader import ConfigLoader

        data = ConfigLoader().load_config().as_dict()
        payload = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:12]
    except Exception:
        return ""


def _static_policy_snapshot() -> dict[str, Any]:
    """Cacheable policy fields — excludes live daily_loss counters."""
    global _effective_policy_cache
    if _effective_policy_cache is not None:
        return dict(_effective_policy_cache)
    from system.gate_relaxation import relaxation_snapshot

    block = _policy_block()
    relax = relaxation_snapshot()
    static = {
        "app_version": "v29.1",
        "learning_demo_enabled": learning_demo_enabled(),
        "profile": learning_demo_profile(),
        "policy_id": learning_demo_policy_id(),
        "integrity": {
            "require_gate_sourced_submit": learning_demo_integrity_enabled(),
            "suppress_v26_gate_relaxations": v26_gate_relaxations_suppressed(),
            "disable_dynamic_sizing": bool(block.get("disable_dynamic_sizing", True)),
        },
        "config_hash": config_hash_short(),
        "relaxation": relax,
        "spread_to_atr_circuit_max": relax.get("spread_to_atr_circuit_max"),
        "note": str(block.get("_note") or ""),
    }
    _effective_policy_cache = dict(static)
    return dict(static)


def effective_policy_snapshot(store: Any | None = None) -> dict[str, Any]:
    """Dashboard / SUBMIT_TRUTH — single source for demo operating policy."""
    from system.daily_loss_policy import (
        daily_loss_reset_snapshot,
        effective_daily_loss_gbp,
        hard_daily_loss_limit_gbp,
        soft_pause_threshold_gbp,
    )

    payload = _static_policy_snapshot()
    payload["daily_loss"] = {
        "soft_pause_gbp": soft_pause_threshold_gbp(),
        "hard_limit_gbp": hard_daily_loss_limit_gbp(),
        "effective_loss_gbp": round(effective_daily_loss_gbp(store), 2),
        "reset": daily_loss_reset_snapshot(store),
    }
    return payload
