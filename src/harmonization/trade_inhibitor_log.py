"""
Real-time trade inhibitor log — exact mathematical rejection reasons.

Appends to project-root ``blockers.log`` for operator visibility.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from system.paths import project_root

_BLOCKERS_PATH = project_root() / "blockers.log"


def log_trade_inhibitor(
    *,
    epic: str,
    gate: str,
    reason: str,
    metrics: dict[str, Any] | None = None,
) -> None:
    """Write one inhibitor line — e.g. Confidence 0.81 < Target 0.90."""
    ts = datetime.now(timezone.utc).isoformat()
    extra = ""
    if metrics:
        parts = [f"{k}={v}" for k, v in metrics.items() if v is not None]
        if parts:
            extra = " | " + " ".join(parts)
    line = f"{ts} [INHIBITOR] epic={epic} gate={gate} {reason}{extra}\n"
    try:
        _BLOCKERS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _BLOCKERS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass
