"""
Structured trade audit logging — signal through close (read-only).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from system.paths import logs_dir

_AUDIT_LOG = logs_dir() / "trade_audit.log"


def _line(event: str, **fields: Any) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = {k: v for k, v in fields.items() if v is not None}
    return f"{ts} | {event} | {json.dumps(payload, default=str)}"


def log_trade_audit(event: str, **fields: Any) -> None:
    from system.log_rotator import rotate_if_needed

    _AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    rotate_if_needed(_AUDIT_LOG)
    with open(_AUDIT_LOG, "a", encoding="utf-8") as f:
        f.write(_line(event, **fields) + "\n")
