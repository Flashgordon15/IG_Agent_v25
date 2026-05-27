"""
Application-wide credentials cache — loaded once at startup, reloadable on demand.

Use :func:`bootstrap_credentials` from main before UI/API clients.
"""

from __future__ import annotations

from system.credentials_loader import (
    Credentials,
    CredentialsError,
    CredentialsStatus,
    load_credentials,
    try_load_credentials,
)

_holder: "CredentialsHolder | None" = None


class CredentialsHolder:
    """Caches validated IG credentials for the running process."""

    def __init__(self) -> None:
        self.reload()

    @property
    def status(self) -> CredentialsStatus:
        return self._status

    @property
    def credentials(self) -> Credentials | None:
        return self._status.credentials if self._status.loaded else None

    @property
    def live_allowed(self) -> bool:
        return self._status.loaded

    def reload(self) -> CredentialsStatus:
        self._status = try_load_credentials()
        return self._status

    def require_credentials(self) -> Credentials:
        """Load and validate; raises CredentialsError if not available."""
        return load_credentials()


def bootstrap_credentials() -> CredentialsHolder:
    """
    Initialise credentials at application startup (non-fatal if missing).

    Call before MainWindow and before any IG REST/streaming client.
    """
    global _holder
    _holder = CredentialsHolder()
    return _holder


def get_credentials_holder() -> CredentialsHolder:
    if _holder is None:
        return bootstrap_credentials()
    return _holder
