"""
Streaming client factory — optional Lightstreamer with REST poll fallback.
"""

from __future__ import annotations

from typing import Any

from ig_api.auth import SessionTokens
from ig_api.streaming_client import IGStreamingClient
from system.credentials_loader import Credentials
from system.engine_log import log_engine


def lightstreamer_available() -> bool:
    try:
        import lightstreamer.client  # noqa: F401

        return True
    except ImportError:
        return False


def lightstreamer_unavailable_reason() -> str:
    try:
        import lightstreamer.client  # noqa: F401

        return ""
    except ImportError as e:
        return f"Lightstreamer SDK not installed ({e})"
    except Exception as e:
        return f"Lightstreamer SDK check failed ({type(e).__name__}: {e})"


def resolve_streaming_transport(
    transport: str | None,
    *,
    session: SessionTokens | None = None,
) -> tuple[str, str]:
    """Return (effective_transport, human-readable reason)."""
    requested = (transport or "auto").lower().strip()
    if requested == "auto":
        if lightstreamer_available():
            if session is not None and not session.lightstreamer_endpoint:
                return (
                    "rest_poll",
                    "auto: login response missing lightstreamerEndpoint",
                )
            return "lightstreamer", "auto: Lightstreamer SDK available"
        return "rest_poll", f"auto: {lightstreamer_unavailable_reason() or 'SDK unavailable'}"

    if requested == "lightstreamer":
        if not lightstreamer_available():
            return (
                "rest_poll",
                f"lightstreamer: {lightstreamer_unavailable_reason() or 'SDK unavailable'}",
            )
        if session is not None and not session.lightstreamer_endpoint:
            return (
                "rest_poll",
                "lightstreamer: login response missing lightstreamerEndpoint",
            )
        return "lightstreamer", "config transport=lightstreamer"

    if requested == "rest_poll":
        return "rest_poll", "config transport=rest_poll"

    return "rest_poll", f"unknown transport '{requested}' — using REST poll"


def create_streaming_client(
    credentials: Credentials,
    session: SessionTokens,
    *,
    rest_client: Any,
    poll_interval_seconds: float = 5.0,
    transport: str | None = None,
) -> Any:
    """
    Create IG price streaming client.

    transport: auto | rest_poll | lightstreamer (from config when None)
    """
    from system.config_loader import get_config

    requested = transport or get_config().streaming_transport
    mode, reason = resolve_streaming_transport(requested, session=session)
    log_engine(f"Streaming transport selected: {mode} (reason: {reason})")

    if mode == "lightstreamer":
        try:
            from ig_api.lightstreamer_streaming import IGLightstreamerStreamingClient

            client = IGLightstreamerStreamingClient(
                credentials,
                session,
                rest_client=rest_client,
                poll_interval_seconds=poll_interval_seconds,
            )
            log_engine("streaming transport=lightstreamer (with REST poll fallback)")
            return client
        except Exception as e:
            log_engine(
                f"Lightstreamer init failed — REST poll fallback: {type(e).__name__}: {e}"
            )

    log_engine("streaming transport=rest_poll")
    return IGStreamingClient(
        credentials,
        session,
        rest_client=rest_client,
        poll_interval_seconds=poll_interval_seconds,
    )
