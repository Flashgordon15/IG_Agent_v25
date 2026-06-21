"""Runtime exception boundaries for critical hot paths."""

from system.guard.runtime_guard import guard_call, log_guarded_exception

__all__ = ["guard_call", "log_guarded_exception"]
