"""Backward-compatible re-export — canonical module is ``system.guard.runtime_guard``."""

from system.guard.runtime_guard import guard_call, log_guarded_exception

__all__ = ["guard_call", "log_guarded_exception"]

# Legacy alias
log_subsystem_error = log_guarded_exception
