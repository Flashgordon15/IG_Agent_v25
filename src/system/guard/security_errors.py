"""Institutional fail-closed security exceptions — hard stop boundaries."""

from __future__ import annotations


class FailClosedSecurityError(RuntimeError):
    """Fatal security boundary violation — process must not continue."""


class SharedMemoryOverflowAlert(FailClosedSecurityError):
    """Telemetry JSON exceeds fixed shared-memory segment capacity."""
