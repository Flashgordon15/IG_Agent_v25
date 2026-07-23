"""
Cross-process REST budget coordinator.

``ChaosGuardian`` token buckets are per-process. With the agent plus the desk /
trade support wrappers all running, each held its own 3-calls/min budget, so the
*combined* outbound rate to IG blew past the real account limit → 429s → stale
data. This module keeps a small shared ledger of recent outbound calls per bucket
so any process can see the GLOBAL recent rate and back off before piling on.

It is intentionally advisory and fail-open: if the ledger can't be read/written
(permissions, race, corruption) callers proceed as before. Critical lanes
(orders / confirms / fast-pass exits) are never deferred — only best-effort read
lanes (positions/ledger/yahoo) consult it.

Concurrency: a best-effort ``fcntl`` flock guards the read-modify-write. The file
holds, per bucket, a list of recent unix timestamps pruned to a 60s window.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

try:
    import fcntl  # POSIX only

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - non-POSIX
    _HAVE_FCNTL = False

from system.paths import shared_state_dir

def _ledger_path() -> Path:
    """Shared REST budget ledger — one file across v32 dual-port processes."""
    return shared_state_dir() / "rest_budget_shared.json"
_WINDOW_SEC = 60.0
# Read lanes that are safe to coordinate/defer across processes.
_COORDINATED_BUCKETS = {"ig_positions", "ig_ledger", "yahoo"}


def _now() -> float:
    return time.time()


def _read_locked(fh: Any) -> dict[str, list[float]]:
    try:
        fh.seek(0)
        raw = fh.read()
        data = json.loads(raw) if raw.strip() else {}
        if isinstance(data, dict):
            return {k: list(v) for k, v in data.items() if isinstance(v, list)}
    except (ValueError, OSError):
        pass
    return {}


def _prune(stamps: list[float], now: float) -> list[float]:
    cutoff = now - _WINDOW_SEC
    return [t for t in stamps if t >= cutoff]


def _open_ledger():
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # a+ so the file is created if absent; we manage the cursor explicitly.
    return open(path, "a+", encoding="utf-8")


def record(bucket: str) -> None:
    """Record one outbound call for ``bucket`` (best-effort, fail-open)."""
    if not bucket:
        return
    try:
        with _open_ledger() as fh:
            if _HAVE_FCNTL:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            now = _now()
            data = _read_locked(fh)
            stamps = _prune(data.get(bucket, []), now)
            stamps.append(now)
            data[bucket] = stamps
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps(data))
            fh.flush()
            if _HAVE_FCNTL:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def recent_count(bucket: str, *, window_sec: float = _WINDOW_SEC) -> int:
    """Global count of calls for ``bucket`` in the trailing window."""
    try:
        path = _ledger_path()
        if not path.is_file():
            return 0
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return 0
        now = _now()
        cutoff = now - float(window_sec)
        return sum(1 for t in data.get(bucket, []) if isinstance(t, (int, float)) and t >= cutoff)
    except (OSError, ValueError):
        return 0


def is_coordinated(bucket: str) -> bool:
    return bucket in _COORDINATED_BUCKETS


def over_global_limit(bucket: str, limit_per_min: float) -> bool:
    """True when the GLOBAL recent rate for a coordinated bucket is at/over cap."""
    if bucket not in _COORDINATED_BUCKETS or limit_per_min <= 0:
        return False
    return recent_count(bucket) >= float(limit_per_min)


def snapshot() -> dict[str, Any]:
    """Diagnostic view of global recent counts per bucket."""
    out: dict[str, Any] = {"window_sec": _WINDOW_SEC, "buckets": {}}
    try:
        path = _ledger_path()
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            now = _now()
            cutoff = now - _WINDOW_SEC
            for k, v in (data.items() if isinstance(data, dict) else []):
                if isinstance(v, list):
                    out["buckets"][k] = sum(
                        1 for t in v if isinstance(t, (int, float)) and t >= cutoff
                    )
    except (OSError, ValueError):
        pass
    return out
