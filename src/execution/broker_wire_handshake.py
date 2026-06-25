"""Permanent outbound/inbound IG order wire log — full request + response pairs."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_DEFAULT_LOG = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "v31-production"
    / "logs"
    / "broker_wire_handshake.log"
)


def broker_wire_handshake_log_path() -> Path:
    raw = os.environ.get("IG_BROKER_WIRE_LOG", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return _DEFAULT_LOG.resolve()


def append_broker_wire_handshake(
    *,
    source: str,
    phase: str,
    epic: str = "",
    direction: str = "",
    request_payload: dict[str, Any] | None = None,
    response_text: str = "",
    response_json: dict[str, Any] | None = None,
    status_code: int | None = None,
    ok: bool = False,
    error_code: str = "",
    message: str = "",
) -> None:
    """Append one JSON line: complete outbound payload + inbound broker body."""
    raw = (response_text or "").strip()
    parsed = response_json
    if parsed is None and raw:
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
    if parsed and not error_code:
        error_code = str(
            parsed.get("errorCode")
            or parsed.get("reasonCode")
            or parsed.get("dealStatus")
            or parsed.get("reason")
            or ""
        )

    record: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "ts_epoch": time.time(),
        "source": source,
        "phase": phase,
        "ok": ok,
        "epic": epic,
        "direction": direction,
        "status_code": status_code,
        "error_code": error_code,
        "message": message,
        "outbound_request_json": request_payload or {},
        "inbound_response_raw": raw,
        "inbound_response_json": parsed,
    }

    path = broker_wire_handshake_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
