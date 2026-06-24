"""Phase 5 — micro-lot live verification floor (0.1 contracts)."""

from __future__ import annotations

import os
from typing import Any

MICRO_CONTRACT_SIZE = 0.1


def _config_block() -> dict[str, Any]:
    try:
        from system.config_loader import ConfigLoader

        raw = ConfigLoader().load() or {}
        block = raw.get("micro_lot_verification")
        return dict(block) if isinstance(block, dict) else {}
    except Exception:
        return {}


def micro_lot_verification_enabled() -> bool:
    env_on = os.environ.get("IG_MICRO_LOT_VERIFY", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if os.environ.get("IG_AGENT_PYTEST", "").strip() == "1":
        return env_on
    if env_on:
        return True
    return bool(_config_block().get("enabled", False))


def micro_contract_size() -> float:
    block = _config_block()
    try:
        size = float(block.get("contract_size", MICRO_CONTRACT_SIZE))
    except (TypeError, ValueError):
        size = MICRO_CONTRACT_SIZE
    return max(0.01, min(size, 1.0))


def clamp_micro_lot_size(size: float) -> float:
    """Force transmission size to micro contract when Phase 5 is armed."""
    if not micro_lot_verification_enabled():
        return float(size)
    return micro_contract_size()
