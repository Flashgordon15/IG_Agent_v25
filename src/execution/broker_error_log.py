"""Append-only IG broker rejection log — raw JSON payloads for order audit."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_DEFAULT_LOG = (
    Path(__file__).resolve().parents[1] / "data" / "v31-production" / "logs" / "broker_errors.log"
)


def broker_errors_log_path() -> Path:
    raw = os.environ.get("IG_BROKER_ERRORS_LOG", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return _DEFAULT_LOG.resolve()


def append_broker_rejection(
    *,
    source: str,
    epic: str = "",
    direction: str = "",
    payload: dict[str, Any] | None = None,
    response_body: str | dict[str, Any] | None = None,
    status_code: int | None = None,
    error_code: str = "",
    exception_type: str = "",
    message: str = "",
) -> None:
    """Write one JSON line with the full IG rejection context (never swallow silently)."""
    parsed: dict[str, Any] | None = None
    raw_text = ""
    if isinstance(response_body, dict):
        parsed = response_body
        raw_text = json.dumps(response_body, default=str)
    elif isinstance(response_body, str) and response_body.strip():
        raw_text = response_body.strip()
        try:
            parsed = json.loads(raw_text)
        except Exception:
            parsed = None

    if parsed and not error_code:
        error_code = str(
            parsed.get("errorCode")
            or parsed.get("reasonCode")
            or parsed.get("reason")
            or parsed.get("code")
            or ""
        )

    record: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "ts_epoch": time.time(),
        "source": source,
        "epic": epic,
        "direction": direction,
        "status_code": status_code,
        "error_code": error_code,
        "exception_type": exception_type,
        "message": message,
        "request_payload": payload or {},
        "response_raw": raw_text,
        "response_json": parsed,
    }

    path = broker_errors_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")

    try:
        from system.engine_log import log_engine

        log_engine(
            f"BROKER_REJECT source={source} epic={epic} code={error_code or '—'} "
            f"http={status_code} msg={(message or raw_text)[:200]}"
        )
    except Exception:
        pass
