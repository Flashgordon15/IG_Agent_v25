"""IG API exception hierarchy."""


class IGAPIError(Exception):
    """Base exception for all IG API failures."""

    def __init__(self, message: str, *, status_code: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class IGAuthError(IGAPIError):
    """Login, session refresh, or credential validation failed."""


class IGOrderError(IGAPIError):
    """Order placement, confirmation, or position update failed."""


class IGStreamError(IGAPIError):
    """Lightstreamer connection, subscription, or reconnect failure."""


class RateLimitError(IGAPIError):
    """IG API rate limit exceeded — REST/streaming paused until cooldown."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "",
        status_code: int | None = 403,
        body: str | None = None,
        seconds_until_reset: float = 0.0,
    ) -> None:
        super().__init__(message, status_code=status_code, body=body)
        self.error_code = error_code
        self.seconds_until_reset = seconds_until_reset
