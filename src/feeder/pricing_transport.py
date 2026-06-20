"""Reference vs execution pricing transport resolution (Yahoo display / IG orders)."""

from __future__ import annotations

import os
from typing import Any, Literal

ReferenceTransport = Literal["ig", "yahoo", "mock"]
ExecutionTransport = Literal["ig"]

_VALID_REFERENCE = frozenset({"ig", "yahoo", "mock"})


def _pricing_block(cfg: Any | None) -> dict[str, Any]:
    if cfg is None:
        return {}
    if isinstance(cfg, dict):
        raw = cfg.get("pricing")
        return dict(raw) if isinstance(raw, dict) else {}
    getter = getattr(cfg, "get", None)
    if callable(getter):
        raw = getter("pricing", {})
        return dict(raw) if isinstance(raw, dict) else {}
    data = getattr(cfg, "_data", None)
    if isinstance(data, dict):
        raw = data.get("pricing")
        return dict(raw) if isinstance(raw, dict) else {}
    return {}


def reference_transport(cfg: Any | None = None) -> ReferenceTransport:
    """Resolve hub quote source: ig (default), yahoo, or mock."""
    env = os.environ.get("IG_PRICING_REFERENCE", "").strip().lower()
    if env in _VALID_REFERENCE:
        return env  # type: ignore[return-value]
    block = _pricing_block(cfg)
    raw = str(block.get("reference_transport") or "ig").strip().lower()
    if raw in _VALID_REFERENCE:
        return raw  # type: ignore[return-value]
    return "ig"


def execution_transport(cfg: Any | None = None) -> ExecutionTransport:
    block = _pricing_block(cfg)
    raw = str(block.get("execution_transport") or "ig").strip().lower()
    return "ig" if raw != "ig" else "ig"


def reference_transport_is_yahoo(cfg: Any | None = None) -> bool:
    return reference_transport(cfg) == "yahoo"


def yahoo_poll_seconds(cfg: Any | None = None) -> float:
    block = _pricing_block(cfg)
    try:
        return max(1.0, float(block.get("yahoo_poll_sec", 3)))
    except (TypeError, ValueError):
        return 3.0


def ig_snapshot_at_execution(cfg: Any | None = None) -> bool:
    block = _pricing_block(cfg)
    return bool(block.get("ig_snapshot_at_execution", False))
