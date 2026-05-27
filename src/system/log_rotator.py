"""Simple size-based log rotation for append-only log files."""

from __future__ import annotations

import os
from pathlib import Path


def rotate_if_needed(
    path: Path,
    *,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """Rotate log file when it exceeds max_bytes."""
    if not path.exists():
        return
    try:
        if path.stat().st_size < max_bytes:
            return
    except OSError:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    for i in range(backup_count - 1, 0, -1):
        src = path.with_name(f"{path.name}.{i}")
        dst = path.with_name(f"{path.name}.{i + 1}")
        if src.exists():
            if dst.exists():
                dst.unlink()
            src.rename(dst)
    if path.exists():
        path.rename(path.with_name(f"{path.name}.1"))
        path.touch()
