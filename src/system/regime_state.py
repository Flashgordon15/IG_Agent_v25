"""
Unified regime_state API — O(1) snapshot from regime_switch_engine cache.
"""

from __future__ import annotations

import time
from typing import Any


def get_regime_state_snapshot() -> dict[str, Any]:
    from runtime.regime_switch_engine import get_regime_switch_snapshot

    snap = get_regime_switch_snapshot()
    snap["endpoint"] = "regime_state"
    snap["ts"] = time.time()
    return snap
