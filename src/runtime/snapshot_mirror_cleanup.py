"""Deactivate temporary snapshot-mirror helpers when the book is flat.

Once ``POST /api/admin/force_snapshot_sync`` is deployed and open DOW/book count
is zero, touch the production stop flag under
``src/data/v31-production/state/.stop_snapshot_mirror`` and best-effort reap
leftover mirror PIDs — never touch the main agent PID.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from system.engine_log import log_engine

_MIRROR_CMD_MARKERS = (
    "stop_snapshot_mirror",
    "legacy_mirror",
    "force_disk_sync_operator",
    "snapshot_legacy_mirror",
)


def stop_flag_path() -> Path:
    """Production-only path — ``src/data/v31-production/state/`` (no legacy)."""
    from system.paths import v31_production_data_dir

    return v31_production_data_dir() / "state" / ".stop_snapshot_mirror"


def force_snapshot_sync_route_deployed() -> bool:
    """True when the permanent admin sync route is registered in this process."""
    try:
        from api import routes

        for route in getattr(routes, "router", None).routes or []:
            path = getattr(route, "path", "") or ""
            if path == "/api/admin/force_snapshot_sync":
                return True
    except Exception:
        pass
    # Source-present fallback (pre-import / unit tests)
    try:
        routes_py = Path(__file__).resolve().parents[1] / "api" / "routes.py"
        text = routes_py.read_text(encoding="utf-8")
        return "force_snapshot_sync" in text and "/api/admin/force_snapshot_sync" in text
    except OSError:
        return False


def _open_position_count() -> int:
    try:
        from runtime import broker_snapshot

        snap = broker_snapshot.read_snapshot(max_age_sec=None) or {}
        return int(snap.get("count") or len(snap.get("positions") or []))
    except Exception:
        return -1


def _agent_listen_pids() -> set[int]:
    pids: set[int] = set()
    try:
        lsof = subprocess.check_output(
            ["lsof", "-iTCP:8080", "-sTCP:LISTEN", "-t"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        for line in lsof.splitlines():
            try:
                pids.add(int(line.strip()))
            except ValueError:
                pass
    except (OSError, subprocess.CalledProcessError):
        pass
    return pids


def _reap_mirror_pids() -> list[int]:
    """SIGTERM mirror helper processes only — never the agent listen PID."""
    reaped: list[int] = []
    self_pid = os.getpid()
    protected = _agent_listen_pids() | {self_pid, 0}
    try:
        # Fingerprint: the emergency loop embeds this stop-flag path in argv.
        out = subprocess.check_output(
            ["pgrep", "-fl", "stop_snapshot_mirror"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return reaped

    for line in out.splitlines():
        parts = line.split(None, 1)
        if not parts:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid in protected:
            continue
        cmd = parts[1] if len(parts) > 1 else ""
        if "main.py" in cmd or "uvicorn" in cmd:
            continue
        if not any(m in cmd for m in _MIRROR_CMD_MARKERS):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            reaped.append(pid)
            log_engine(f"SnapshotMirrorCleanup: SIGTERM mirror pid={pid}")
        except OSError:
            continue
    return reaped


def maybe_deactivate_legacy_snapshot_mirror(
    *,
    open_count: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Touch stop flag + reap mirrors when flat and force_snapshot_sync is live."""
    count = _open_position_count() if open_count is None else int(open_count)
    deployed = force_snapshot_sync_route_deployed()
    result: dict[str, Any] = {
        "ok": False,
        "open_count": count,
        "route_deployed": deployed,
        "stop_flag": str(stop_flag_path()),
        "reaped": [],
    }
    if not force:
        if count < 0:
            result["skipped"] = "open_count_unknown"
            return result
        if count > 0:
            result["skipped"] = "positions_open"
            return result
        if not deployed:
            result["skipped"] = "route_not_deployed"
            return result

    path = stop_flag_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"ts={time.time()}\npid={os.getpid()}\n", encoding="utf-8")
        result["stop_flag_written"] = True
    except OSError as exc:
        result["error"] = f"stop_flag:{type(exc).__name__}:{exc}"
        return result

    reaped = _reap_mirror_pids()
    result["reaped"] = reaped
    result["ok"] = True

    try:
        from diagnostics.performance_journal import record_flat_session

        record_flat_session(reason="mirror_cleanup")
    except Exception:
        pass

    log_engine(
        "SnapshotMirrorCleanup: deactivated legacy mirror "
        f"(open={count} reaped={reaped})"
    )
    return result
