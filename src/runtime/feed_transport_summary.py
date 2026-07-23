"""Operator-facing feed/transport truth for Mini desk (rest_poll vs Yahoo/TD)."""

from __future__ import annotations

from typing import Any


def build_feed_transport_summary(cfg: Any | None = None) -> dict[str, Any]:
    """One strip for ops_strip / health — config transport + live quote hub."""
    out: dict[str, Any] = {
        "streaming_transport": "unknown",
        "streaming_reason": "",
        "primary_quote_feed": "",
        "backup_feeds": [],
        "quote_hub_health": "unknown",
        "label": "FEED — unknown",
    }
    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            cfg = None

    requested = "auto"
    if cfg is not None:
        try:
            requested = str(
                cfg.get("streaming_transport")
                if hasattr(cfg, "get")
                else getattr(cfg, "streaming_transport", "auto")
                or "auto"
            ).strip()
        except Exception:
            requested = "auto"

    try:
        from ig_api.streaming_factory import resolve_streaming_transport

        transport, reason = resolve_streaming_transport(requested)
        out["streaming_transport"] = transport
        out["streaming_reason"] = reason
        out["streaming_requested"] = requested
    except Exception as exc:
        out["streaming_error"] = f"{type(exc).__name__}"
        out["streaming_transport"] = str(requested or "unknown")

    try:
        from system.feeds.data_feed_orchestrator import get_data_feed_state

        feed = get_data_feed_state() or {}
        out["primary_quote_feed"] = str(feed.get("primary_feed") or "")
        out["backup_feeds"] = list(feed.get("backup_feeds") or [])
        out["quote_hub_health"] = str(feed.get("health") or "unknown")
        out["fresh_count"] = feed.get("fresh_count")
        out["total_epics"] = feed.get("total_epics")
    except Exception as exc:
        out["quote_hub_error"] = f"{type(exc).__name__}"

    transport = str(out.get("streaming_transport") or "unknown")
    primary = str(out.get("primary_quote_feed") or "—")
    backups = ",".join(str(b) for b in (out.get("backup_feeds") or [])) or "none"
    hub = str(out.get("quote_hub_health") or "unknown")
    out["label"] = (
        f"IG {transport} · quotes {primary} ({hub}) · backup {backups}"
    )
    return out
