"""Adaptive back-off for rest_poll transport — soak-safe 429/timeout recovery."""

from __future__ import annotations

import socket

from ig_api.exceptions import IGAPIError, RateLimitError

_BACKOFF_429_SEC = (2.0, 4.0)


def is_http_429(exc: BaseException) -> bool:
    if isinstance(exc, RateLimitError):
        return True
    if isinstance(exc, IGAPIError) and getattr(exc, "status_code", None) == 429:
        return True
    msg = str(exc).lower()
    return "429" in msg or "too many requests" in msg


def is_connection_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, socket.timeout):
        return True
    name = type(exc).__name__
    if "Timeout" in name or "timed out" in str(exc).lower():
        return True
    try:
        import requests

        if isinstance(exc, requests.exceptions.Timeout):
            return True
    except ImportError:
        pass
    return False


def is_retryable_poll_error(exc: BaseException) -> bool:
    return is_http_429(exc) or is_connection_timeout(exc)


class RestPollBackoff:
    """
    Exponential back-off for rest_poll: 2s on first 429/timeout, 4s on repeat.

    Resets to the normal poll interval after a successful fetch cycle.
    """

    def __init__(self, normal_interval: float) -> None:
        self._normal_interval = max(0.1, float(normal_interval))
        self._strike: int = 0

    @property
    def normal_interval(self) -> float:
        return self._normal_interval

    def set_normal_interval(self, interval: float) -> None:
        self._normal_interval = max(0.1, float(interval))

    @property
    def strike(self) -> int:
        return self._strike

    def on_success(self) -> float:
        """Record successful poll; return normal sleep interval."""
        self._strike = 0
        return self._normal_interval

    def on_retryable_error(self, exc: BaseException) -> tuple[float, str]:
        """
        Advance back-off and return (sleep_seconds, reason_label).

        Does not raise — caller decides logging and state transitions.
        """
        if is_http_429(exc):
            label = "HTTP 429"
        elif is_connection_timeout(exc):
            label = "connection timeout"
        else:
            label = type(exc).__name__

        self._strike = min(self._strike + 1, len(_BACKOFF_429_SEC))
        wait = _BACKOFF_429_SEC[min(self._strike - 1, len(_BACKOFF_429_SEC) - 1)]
        return wait, label

    def reset_for_tests(self) -> None:
        self._strike = 0


def format_backoff_warning(label: str, wait_s: float, *, strike: int) -> str:
    return f"rest_poll: {label} — backing off {wait_s:.0f}s (strike {strike}, soak-safe)"


def soft_streaming_status(label: str, wait_s: float) -> str:
    return f"rest_poll backoff {wait_s:.0f}s ({label})"
