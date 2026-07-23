"""Redacted outbound order / REST forensics — per-engine state_dir log."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any

from system.demo_rest_log import mask_token
from system.paths import state_dir

_LOG_LOCK = threading.Lock()
_MAX_BYTES = 5 * 1024 * 1024  # ~5 MB rotate

_SENSITIVE_HEADER_KEYS = frozenset(
    {
        "cst",
        "x-security-token",
        "ig-security-token",
        "x-ig-api-key",
        "authorization",
        "cookie",
        "set-cookie",
    }
)
_SENSITIVE_JSON_KEYS = frozenset(
    {
        "password",
        "identifier",
        "apikey",
        "api_key",
        "cst",
        "securitytoken",
        "security_token",
        "x-security-token",
        "authorization",
        "token",
    }
)


def forensic_network_enabled() -> bool:
    """Gate forensics — default ON for dual-port / per-account processes."""
    env = os.environ.get("IG_FORENSIC_NETWORK", "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    return (
        os.environ.get("IG_V32_DUAL_PORT", "").strip() == "1"
        or os.environ.get("IG_SESSION_REGISTRY", "").strip() == "1"
        or bool(os.environ.get("IG_ACCOUNT_ID", "").strip())
    )


def forensic_log_path() -> str:
    return str(state_dir() / "forensic_network.log")


def redact_header_value(key: str, value: Any) -> dict[str, Any]:
    """Redact a single header — never write full CST / XST / API key."""
    k = str(key or "").strip()
    kl = k.lower()
    raw = "" if value is None else str(value)
    if kl in _SENSITIVE_HEADER_KEYS or "token" in kl or "secret" in kl:
        masked = mask_token(raw) if raw else ""
        return {
            "present": bool(raw),
            "length": len(raw),
            "redacted": masked or ("****" if raw else ""),
        }
    return {"present": bool(raw), "value": raw[:120]}


def redact_headers(headers: dict[str, Any] | None) -> dict[str, Any]:
    if not headers:
        return {}
    out: dict[str, Any] = {}
    for key, val in headers.items():
        info = redact_header_value(str(key), val)
        name = str(key)
        if "redacted" in info:
            suffix = info["redacted"][-4:] if info["redacted"] else ""
            out[name] = (
                f"present={info['present']} len={info['length']} "
                f"****{suffix}" if suffix else f"present={info['present']} len={info['length']}"
            )
        else:
            out[name] = info.get("value", "")
    return out


def redact_json(value: Any, *, _depth: int = 0) -> Any:
    """Deep-redact JSON bodies — strip secrets, truncate large strings."""
    if _depth > 8:
        return "…"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            kl = str(k).lower().replace("-", "").replace("_", "")
            if kl in _SENSITIVE_JSON_KEYS or "password" in kl or "token" in kl:
                sv = "" if v is None else str(v)
                out[str(k)] = {
                    "present": bool(sv),
                    "length": len(sv),
                    "redacted": mask_token(sv) if sv else "",
                }
            else:
                out[str(k)] = redact_json(v, _depth=_depth + 1)
        return out
    if isinstance(value, list):
        return [redact_json(v, _depth=_depth + 1) for v in value[:50]]
    if isinstance(value, str):
        return value[:800] + ("…" if len(value) > 800 else "")
    return value


def truncate_response_body(text: str | None, *, limit: int = 1200) -> str:
    raw = text or ""
    if len(raw) <= limit:
        return raw
    return raw[:limit] + f"…(+{len(raw) - limit} bytes)"


def _parse_response_json(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return truncate_response_body(text, limit=400)


def _rotate_if_needed(path: Any) -> None:
    try:
        if path.is_file() and path.stat().st_size >= _MAX_BYTES:
            rotated = path.with_suffix(path.suffix + ".1")
            if rotated.is_file():
                rotated.unlink()
            path.rename(rotated)
    except OSError:
        pass


def log_forensic_network(
    *,
    account_id: str,
    method: str,
    path: str,
    headers: dict[str, Any] | None = None,
    request_json: dict[str, Any] | None = None,
    status_code: int | None = None,
    response_body: str | None = None,
    deal_reference: str | None = None,
    source: str = "rest_client",
    phase: str = "response",
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one redacted forensic line to ``state_dir()/forensic_network.log``."""
    if not forensic_network_enabled():
        return

    parsed = _parse_response_json(response_body)
    redacted_resp: Any
    if isinstance(parsed, dict):
        redacted_resp = redact_json(parsed)
    elif parsed is None:
        redacted_resp = None
    else:
        redacted_resp = parsed

    deal_ref = deal_reference
    if not deal_ref and isinstance(parsed, dict):
        deal_ref = str(parsed.get("dealReference") or parsed.get("dealId") or "") or None

    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "account_id": str(account_id or "").strip().upper(),
        "source": source,
        "phase": phase,
        "method": str(method or "").upper(),
        "path": str(path or ""),
        "headers_redacted": redact_headers(headers),
        "request_json": redact_json(request_json) if request_json else None,
        "status_code": status_code,
        "response_json": redacted_resp,
        "deal_reference": deal_ref,
    }
    if extra:
        record["extra"] = redact_json(extra)

    line = json.dumps(record, default=str, separators=(",", ":"))

    with _LOG_LOCK:
        try:
            from pathlib import Path

            log_path = Path(forensic_log_path())
            log_path.parent.mkdir(parents=True, exist_ok=True)
            _rotate_if_needed(log_path)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def is_order_dispatch_path(method: str, path: str) -> bool:
    """True for place / close / IOC / confirm order wire paths."""
    p = str(path or "").lower().split("?")[0]
    m = str(method or "").upper()
    if "positions/otc" in p and m in ("POST", "DELETE", "PUT"):
        return True
    if "workingorders" in p and m in ("POST", "DELETE", "PUT"):
        return True
    if "/confirms/" in p:
        return True
    return False
