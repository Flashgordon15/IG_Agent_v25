"""
IG API rate-limit protection — detection, cooldown, recovery.

REST calls paused for 15+ minutes (exponential backoff on repeat hits).
Streaming reconnects paused for 5 minutes per activation.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from system.demo_rest_log import log_demo_rest
from system.engine_log import log_engine
from system.paths import logs_dir

REST_COOLDOWN_BASE_SEC = 15 * 60
STREAM_COOLDOWN_SEC = 5 * 60
MAX_REST_COOLDOWN_SEC = 60 * 60

RATE_LIMIT_ERROR_CODES = frozenset(
    {
        "error.public-api.exceeded-api-key-allowance",
        "error.public-api.exceeded-account-allowance",
    }
)

_LOG = logs_dir() / "rate_limit.log"
_LOCK = threading.RLock()
_manager: "RateLimitManager | None" = None


@dataclass
class RateLimitSnapshot:
    active: bool = False
    error_code: str = ""
    last_403_at: str = ""
    blocked_calls: int = 0
    backoff_stage: int = 0
    rest_seconds_remaining: float = 0.0
    stream_seconds_remaining: float = 0.0
    rest_reset_at: str = ""
    stream_reset_at: str = ""


def get_rate_limit_manager() -> "RateLimitManager":
    global _manager
    if _manager is None:
        _manager = RateLimitManager()
    return _manager


def parse_rate_limit_error(status_code: int, body: str | None) -> str | None:
    """Return IG errorCode if response is a rate-limit failure."""
    if status_code not in (403, 429):
        return None
    text = body or ""
    try:
        data = json.loads(text)
        code = str(data.get("errorCode", ""))
        if code in RATE_LIMIT_ERROR_CODES:
            return code
    except (json.JSONDecodeError, TypeError):
        pass
    for code in RATE_LIMIT_ERROR_CODES:
        if code in text:
            return code
    return None


class RateLimitManager:
    def __init__(self) -> None:
        self._active = False
        self._error_code = ""
        self._rest_until = 0.0
        self._stream_until = 0.0
        self._last_403_ts = 0.0
        self._blocked_calls = 0
        self._backoff_stage = 0
        self._on_cleared: list[Callable[[], None]] = []

    def register_on_cleared(self, callback: Callable[[], None]) -> None:
        self._on_cleared.append(callback)

    def is_rest_blocked(self) -> bool:
        with _LOCK:
            self._try_clear_unlocked()
            return self._active and time.time() < self._rest_until

    def is_stream_blocked(self) -> bool:
        with _LOCK:
            self._try_clear_unlocked()
            return self._active and time.time() < self._stream_until

    def is_active(self) -> bool:
        with _LOCK:
            self._try_clear_unlocked()
            return self._active

    def seconds_until_rest_reset(self) -> float:
        with _LOCK:
            return max(0.0, self._rest_until - time.time())

    def seconds_until_stream_reset(self) -> float:
        with _LOCK:
            return max(0.0, self._stream_until - time.time())

    def check_rest_allowed(self) -> None:
        """Raise RateLimitError if REST calls are blocked."""
        with _LOCK:
            self._try_clear_unlocked()
            if not self._active or time.time() >= self._rest_until:
                return
            self._blocked_calls += 1
            self._sync_diagnostics_unlocked()
            remaining = self._rest_until - time.time()
            raise self._make_error(remaining)

    def check_stream_allowed(self) -> None:
        with _LOCK:
            self._try_clear_unlocked()
            if not self._active or time.time() >= self._stream_until:
                return
            self._blocked_calls += 1
            self._sync_diagnostics_unlocked()
            remaining = self._stream_until - time.time()
            raise self._make_error(remaining, stream=True)

    def activate(self, error_code: str, *, source: str = "REST", path: str = "") -> float:
        with _LOCK:
            return self._activate_unlocked(error_code, source=source, path=path)

    def _activate_unlocked(
        self, error_code: str, *, source: str = "REST", path: str = ""
    ) -> float:
        self._backoff_stage += 1
        mult = 2 ** min(self._backoff_stage - 1, 2)
        rest_pause = min(REST_COOLDOWN_BASE_SEC * mult, MAX_REST_COOLDOWN_SEC)
        now = time.time()
        self._active = True
        self._error_code = error_code
        self._last_403_ts = now
        self._rest_until = now + rest_pause
        self._stream_until = now + STREAM_COOLDOWN_SEC
        self._blocked_calls += 1
        msg = (
            f"IG rate limit ({error_code}) — REST paused {rest_pause // 60}m, "
            f"streaming paused {STREAM_COOLDOWN_SEC // 60}m (stage {self._backoff_stage})"
        )
        self._log(msg, source=source, path=path, stage=self._backoff_stage)
        log_engine(msg)
        self._sync_diagnostics_unlocked()
        return self._rest_until - now

    def handle_http_response(
        self,
        response: Any,
        *,
        source: str = "REST",
        path: str = "",
    ) -> None:
        """If response is rate-limited, activate cooldown and raise RateLimitError."""
        from ig_api.exceptions import RateLimitError

        body = getattr(response, "text", "") or ""
        status = int(getattr(response, "status_code", 0))
        code = parse_rate_limit_error(status, body)
        if not code:
            return
        with _LOCK:
            remaining = self._activate_unlocked(code, source=source, path=path)
        raise RateLimitError(
            f"IG API rate limit: {code} — wait {int(remaining // 60)}m {int(remaining % 60)}s",
            error_code=code,
            status_code=status,
            body=body[:500],
            seconds_until_reset=remaining,
        )

    def snapshot(self) -> RateLimitSnapshot:
        with _LOCK:
            self._try_clear_unlocked()
            return RateLimitSnapshot(
                active=self._active,
                error_code=self._error_code,
                last_403_at=self._fmt_ts(self._last_403_ts),
                blocked_calls=self._blocked_calls,
                backoff_stage=self._backoff_stage,
                rest_seconds_remaining=max(0.0, self._rest_until - time.time()),
                stream_seconds_remaining=max(0.0, self._stream_until - time.time()),
                rest_reset_at=self._fmt_ts(self._rest_until),
                stream_reset_at=self._fmt_ts(self._stream_until),
            )

    def format_countdown(self) -> str:
        snap = self.snapshot()
        if not snap.active:
            return ""
        r = int(snap.rest_seconds_remaining)
        return f"{r // 60:02d}:{r % 60:02d}"

    def clear_after_successful_auth_probe(self) -> None:
        """
        Legacy hook — does NOT end an active IG quota cooldown.

        A successful POST /session only proves credentials; trading endpoints
        may still return 403 until the cooldown timer expires.
        """
        with _LOCK:
            if not self._active:
                return
            if time.time() < self._rest_until:
                self._log(
                    "Auth probe OK — IG quota cooldown unchanged",
                    remaining_sec=int(self._rest_until - time.time()),
                )
                return
            self._try_clear_unlocked()

    def try_clear_if_expired(self) -> bool:
        with _LOCK:
            return self._try_clear_unlocked()

    def reset_for_tests(self) -> None:
        """Clear local cooldown (unit tests only)."""
        with _LOCK:
            self._active = False
            self._rest_until = 0.0
            self._stream_until = 0.0
            self._error_code = ""
            self._backoff_stage = 0
            self._blocked_calls = 0
            self._sync_diagnostics_unlocked()

    def _try_clear_unlocked(self) -> bool:
        if not self._active:
            return False
        now = time.time()
        if now < self._rest_until or now < self._stream_until:
            return False
        self._active = False
        msg = "Rate limit cleared — DEMO mode restored"
        self._log(msg)
        log_engine(msg)
        log_demo_rest("Rate limit cooldown expired — safe to retry authentication")
        self._sync_diagnostics_unlocked()
        for cb in list(self._on_cleared):
            try:
                cb()
            except Exception as e:
                log_engine(f"rate limit on_cleared callback error: {e}")
        return True

    def _make_error(self, remaining: float, *, stream: bool = False) -> Exception:
        from ig_api.exceptions import RateLimitError

        kind = "streaming" if stream else "REST"
        return RateLimitError(
            f"IG API rate limit active — {kind} blocked for {int(remaining // 60)}m {int(remaining % 60)}s",
            error_code=self._error_code,
            status_code=403,
            seconds_until_reset=remaining,
        )

    def _sync_diagnostics_unlocked(self) -> None:
        try:
            from system.demo_execution_trace import update_demo_diagnostics

            snap = self.snapshot()
            update_demo_diagnostics(
                rate_limit_active=snap.active,
                rate_limit_countdown=self.format_countdown() if snap.active else "",
                last_403_timestamp=snap.last_403_at,
                blocked_calls_count=snap.blocked_calls,
                backoff_stage=snap.backoff_stage,
                rest_status="rate limited" if snap.active else "",
                fallback_reason=(
                    "IG API rate limit — no simulator fallback"
                    if snap.active
                    else ""
                ),
            )
        except Exception:
            pass

    @staticmethod
    def _fmt_ts(ts: float) -> str:
        if ts <= 0:
            return ""
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _log(message: str, **fields: Any) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {message}"
        if fields:
            line += f" | {json.dumps(fields, default=str)}"
        _LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
