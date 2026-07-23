"""
Forceful environmental reset helpers for dual-desk start.

Flushes residual twin locks/tokens for a clean baseline. Never deletes
manual_stop.json, learning DB, journals, or operator deploy holds.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


# Globs / names safe to remove on start — never learning DB / journals / holds.
_SAFE_LOCK_GLOBS = (
    "*.lock",
    "session_ig_*.lock",
    ".ig_agent_*.lock",
)

_PROTECTED_NAMES = frozenset(
    {
        "manual_stop.json",
        "deploy_hold.json",
        "learning.db",
        "triage.db",
        "trade_support_status.json",  # cleared separately by SoT cache eviction
    }
)


def resolve_data_root(explicit: Path | str | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("IG_DATA_ROOT") or os.environ.get("IG_AGENT_DATA_DIR")
    if env:
        return Path(env)
    try:
        from system.paths import data_dir

        return Path(data_dir())
    except Exception:
        return Path("src/data/v31-production")


def flush_twin_runtime_locks(
    data_root: Path | str | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Remove residual ``*.lock`` under state_cfd / state_sb / data root.

    Mirrors the start-loop:
      rm -f $DATA_ROOT/state_cfd/*.lock $DATA_ROOT/state_sb/*.lock $DATA_ROOT/*.lock
    """
    root = resolve_data_root(data_root)
    targets: list[Path] = []
    dirs = (
        root / "state_cfd",
        root / "state_sb",
        root,
    )
    for directory in dirs:
        if not directory.is_dir():
            continue
        for pattern in _SAFE_LOCK_GLOBS:
            for path in directory.glob(pattern):
                if not path.is_file():
                    continue
                if path.name in _PROTECTED_NAMES:
                    continue
                # Only lock-like files at data root (never wipe json/db).
                if directory == root and not path.name.endswith(".lock"):
                    continue
                targets.append(path)

    removed: list[str] = []
    errors: list[str] = []
    for path in targets:
        if dry_run:
            removed.append(str(path))
            continue
        try:
            path.unlink(missing_ok=True)
            removed.append(str(path))
        except OSError as exc:
            errors.append(f"{path}:{type(exc).__name__}")

    return {
        "ok": not errors,
        "data_root": str(root),
        "removed": removed,
        "errors": errors,
        "dry_run": dry_run,
    }


def flush_stale_trade_support_sot_cache(
    data_root: Path | str | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Drop stale trade_support SoT status caches (not learning DB / holds)."""
    root = resolve_data_root(data_root)
    candidates: list[Path] = [
        root / "trade_support_status.json",
    ]
    for base in (root, root / "state", root / "state_cfd", root / "state_sb"):
        if not base.is_dir() and base != root:
            continue
        candidates.extend(base.glob("trade_support_sot.*"))
        candidates.extend(base.glob("trade_support_status.json"))

    removed: list[str] = []
    for path in candidates:
        if not path.is_file():
            continue
        if dry_run:
            removed.append(str(path))
            continue
        try:
            path.unlink(missing_ok=True)
            removed.append(str(path))
        except OSError:
            pass
    return {"ok": True, "removed": removed, "dry_run": dry_run}


def forceful_environmental_reset(
    data_root: Path | str | None = None,
    *,
    dry_run: bool = False,
    clear_sot_cache: bool = True,
) -> dict[str, Any]:
    """Aggregate start-loop env reset (locks + optional SoT cache)."""
    locks = flush_twin_runtime_locks(data_root, dry_run=dry_run)
    sot: dict[str, Any] = {"ok": True, "removed": [], "skipped": True}
    if clear_sot_cache:
        sot = flush_stale_trade_support_sot_cache(data_root, dry_run=dry_run)
        sot["skipped"] = False
    return {
        "ok": bool(locks.get("ok")) and bool(sot.get("ok")),
        "locks": locks,
        "sot_cache": sot,
    }
