"""Dedicated REST authentication log for DEMO/LIVE IG API."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from system.paths import logs_dir

_LOG = logs_dir() / "demo_rest.log"


def log_demo_rest(message: str, **fields: Any) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    extra = ""
    if fields:
        try:
            extra = " | " + json.dumps(fields, default=str)
        except Exception:
            extra = " | " + str(fields)
    line = f"{ts} | {message}{extra}\n"
    _LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(_LOG, "a", encoding="utf-8") as f:
        f.write(line)


def demo_rest_log_path() -> str:
    return str(_LOG)


def mask_token(token: str | None, show: int = 4) -> str:
    if not token:
        return ""
    t = str(token)
    if len(t) <= show * 2:
        return "*" * len(t)
    return t[:show] + "…" + t[-show:]
