#!/usr/bin/env python3
"""Passive SHM publisher — mirrors fulfillment cache into ig_agent_v30_shm (no HTTP)."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

INTERVAL_SEC = float(os.environ.get("IG_COCKPIT_SHM_MIRROR_SEC", "0.2"))


def main() -> int:
    from system.unified_fulfillment_cache import get_fulfillment_payload
    from system.ipc.ring_buffer import publish_cockpit_shm

    while True:
        try:
            publish_cockpit_shm(get_fulfillment_payload())
        except Exception:
            pass
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
