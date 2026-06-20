"""
v30 Apex — frozen lifecycle parameter matrix and adversarial safety hooks.

Central authority for shadow monolith risk ceilings, ML veto floor, and
network-degradation execution freeze (Workers B/C).
"""

from __future__ import annotations

import threading
import time
from typing import Any

# Pillar 1 & 3 — £10k session profile
BASELINE_EQUITY_GBP = 10_000.0
PER_ASSET_RISK_CAP_GBP = 350.0
PORTFOLIO_RISK_CEILING_GBP = 750.0

# Pillar 5 — ML veto floor (unblocked overnight)
ML_VETO_FLOOR = 0.450

# Pillar 2 — network heartbeat cadence (seconds)
NETWORK_HEARTBEAT_INTERVAL_SEC = 3.0

_MIN_CONTRACT_LOT = 1

_FREEZE_LOCK = threading.RLock()
_execution_frozen = False
_network_degraded_since: float | None = None


def floor_contract_size(size: float, *, min_lot: int = _MIN_CONTRACT_LOT) -> tuple[int, bool]:
    """
    Integer floor truncation for IG contract lots.

    Returns (size_int, under_min_lot). Negative sizes floor toward zero lots.
    """
    try:
        raw = float(size)
    except (TypeError, ValueError):
        return 0, True
    if raw != raw:  # NaN
        return 0, True
    size_int = int(raw // 1)
    if raw < 0:
        size_int = 0
    return size_int, size_int < int(min_lot)


def under_min_lot_detail(size_int: int, min_lot: int = _MIN_CONTRACT_LOT) -> str:
    return f"HOLD: UNDER_MIN_LOT — integer size {size_int} < min lot {min_lot}"


def set_network_degraded(active: bool, *, source: str = "network") -> None:
    """Freeze execution path when broadband / gateway stability drops."""
    global _execution_frozen, _network_degraded_since
    with _FREEZE_LOCK:
        if active:
            if not _execution_frozen:
                _network_degraded_since = time.time()
            _execution_frozen = True
            try:
                from system.engine_log import log_engine

                log_engine(
                    f"[SHADOW HARDENING] NETWORK DEGRADED — execution freeze "
                    f"(source={source} ts={_network_degraded_since:.3f})"
                )
            except Exception:
                pass
        else:
            _execution_frozen = False
            _network_degraded_since = None
            try:
                from system.engine_log import log_engine

                log_engine("[SHADOW HARDENING] network stable — execution unfreeze")
            except Exception:
                pass


def is_execution_frozen() -> bool:
    with _FREEZE_LOCK:
        return _execution_frozen


def network_degraded_snapshot() -> dict[str, Any]:
    with _FREEZE_LOCK:
        return {
            "frozen": _execution_frozen,
            "since_ts": _network_degraded_since,
        }


def reset_hardening_for_tests() -> None:
    global _execution_frozen, _network_degraded_since
    with _FREEZE_LOCK:
        _execution_frozen = False
        _network_degraded_since = None
