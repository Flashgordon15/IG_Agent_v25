"""
Account-scoped session lock — one healthy agent per IG account scope.

Coexists with port-scoped ``system.identity.instance_lock``; this module is the
primary anti-zombie gate for DEMO/LIVE account identity. TESTBED uses an
isolated scope (``testbed:local``) and data root.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from runtime.app_mode import (
    AppMode,
    default_api_port,
    parse_app_mode,
    resolve_app_mode,
    resolve_data_root,
    validate_live_armed,
)
from system.engine_log import log_engine

_LOCK_NAME_PREFIX = "session_"
_acquired = False
_acquired_path: Path | None = None

TESTBED_ACCOUNT_SCOPE = "testbed:local"
_SESSION_STATUS_HEALTHY = "HEALTHY"
_SESSION_STATUS_ZOMBIE = "ZOMBIE"


def mask_account_scope(scope: str) -> str:
    """Mask IG account id for health/operator output — never expose full id."""
    raw = str(scope or "").strip()
    if raw.startswith("ig:"):
        return "ig:***"
    return raw


def _sanitize_scope(scope: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", scope.strip())


def lock_path_for_scope(account_scope: str, data_root: Path | str) -> Path:
    safe = _sanitize_scope(account_scope)
    return Path(data_root) / f"{_LOCK_NAME_PREFIX}{safe}.lock"


def resolve_account_scope(app_mode: AppMode | None = None) -> str:
    """Return account scope string for the active APP_MODE."""
    env_scope = os.environ.get("IG_ACCOUNT_SCOPE", "").strip()
    if env_scope:
        return env_scope

    mode = app_mode or resolve_app_mode()
    if mode is AppMode.TESTBED:
        return TESTBED_ACCOUNT_SCOPE

    from system.env_loader import load_dotenv, prepare_boot_env

    prepare_boot_env()
    load_dotenv()

    account_id = os.environ.get("IG_ACCOUNT_ID", "").strip()
    if not account_id:
        try:
            from system.credentials_loader import try_load_credentials

            status = try_load_credentials()
            if status.ok and status.credentials is not None:
                account_id = status.credentials.ig_account_id.strip()
        except Exception:
            account_id = ""

    if not account_id:
        raise RuntimeError(
            "cannot resolve account_scope — set IG_ACCOUNT_ID or credentials for DEMO/LIVE"
        )

    scope = f"ig:{account_id}"
    os.environ["IG_ACCOUNT_SCOPE"] = scope
    return scope


def read_session_lock(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        if raw.startswith("{"):
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        lines = raw.splitlines()
        pid = int(lines[0].strip()) if lines else 0
        port = int(lines[1].strip()) if len(lines) > 1 else default_api_port(AppMode.DEMO)
        scope = lines[2].strip() if len(lines) > 2 else ""
        return {"pid": pid, "port": port, "account_scope": scope}
    except (ValueError, OSError, json.JSONDecodeError):
        return None


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def health_endpoint_ok(port: int, *, timeout_sec: float = 3.0) -> bool:
    url = f"http://127.0.0.1:{int(port)}/api/health"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "IG-Agent-SessionLock/1.0"})
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def session_is_healthy(record: dict[str, Any] | None) -> bool:
    if not record:
        return False
    status = str(record.get("status") or record.get("session_status") or "").upper()
    if status == _SESSION_STATUS_ZOMBIE:
        return False
    pid = int(record.get("pid") or 0)
    if not pid_alive(pid):
        return False
    port_raw = record.get("port")
    if port_raw is not None:
        try:
            port = int(port_raw)
            if port > 0 and health_endpoint_ok(port):
                return True
        except (TypeError, ValueError):
            pass
    return pid_alive(pid)


def session_status_for_record(record: dict[str, Any] | None) -> str:
    """Return HEALTHY or ZOMBIE for a lock record."""
    if not record:
        return _SESSION_STATUS_ZOMBIE
    explicit = str(record.get("status") or record.get("session_status") or "").upper()
    if explicit == _SESSION_STATUS_ZOMBIE:
        return _SESSION_STATUS_ZOMBIE
    return _SESSION_STATUS_HEALTHY if session_is_healthy(record) else _SESSION_STATUS_ZOMBIE


def clear_stale_lock(path: Path) -> bool:
    record = read_session_lock(path)
    if record is None:
        if path.is_file():
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return True
    if session_is_healthy(record):
        return False
    try:
        path.unlink(missing_ok=True)
        log_engine(f"session_lock: cleared stale lock {path.name}")
    except OSError:
        pass
    return True


def port_bound_by_foreign(port: int, *, my_pid: int | None = None) -> int | None:
    """Return PID listening on port if it is not ``my_pid``."""
    import subprocess

    try:
        out = subprocess.run(
            ["lsof", "-iTCP:%d" % port, "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if not line.isdigit():
            continue
        pid = int(line)
        if my_pid is not None and pid == my_pid:
            continue
        if pid_alive(pid):
            return pid
    return None


def find_active_session(
    account_scope: str,
    data_root: Path | str,
) -> tuple[Path | None, dict[str, Any] | None]:
    path = lock_path_for_scope(account_scope, data_root)
    record = read_session_lock(path)
    if session_is_healthy(record):
        return path, record
    clear_stale_lock(path)
    return None, None


def write_session_lock(
    path: Path,
    *,
    pid: int,
    port: int,
    account_scope: str,
    started_at: int | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = int(started_at if started_at is not None else time.time())
    payload = {
        "pid": int(pid),
        "port": int(port),
        "account_scope": account_scope,
        "app_mode": os.environ.get("APP_MODE", ""),
        "started_at": ts,
        "session_id": f"{int(pid)}-{ts}",
        "status": _SESSION_STATUS_HEALTHY,
        "session_status": _SESSION_STATUS_HEALTHY,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def mark_session_zombie(path: Path) -> None:
    record = read_session_lock(path)
    if record is None:
        return
    record["status"] = _SESSION_STATUS_ZOMBIE
    record["session_status"] = _SESSION_STATUS_ZOMBIE
    try:
        path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def _terminate_pid(pid: int, *, term_wait_sec: float = 15.0) -> tuple[bool, str]:
    if pid <= 0:
        return False, "invalid pid"
    if not pid_alive(pid):
        return True, "already dead"
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return False, f"SIGTERM failed: {exc}"
    deadline = time.monotonic() + term_wait_sec
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True, "terminated (SIGTERM)"
        time.sleep(0.25)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError as exc:
        return False, f"SIGKILL failed: {exc}"
    time.sleep(0.5)
    if pid_alive(pid):
        return False, "process still alive after SIGKILL"
    return True, "terminated (SIGKILL)"


def shutdown_session(
    *,
    app_mode: AppMode,
    account_scope: str | None = None,
    data_root: str | Path | None = None,
    term_wait_sec: float = 15.0,
) -> tuple[int, dict[str, Any]]:
    """
    Symmetrical shutdown for start.sh — terminate lock holder and clear lock.

    Returns ``(exit_code, summary)`` where exit_code 0 = success, 1 = no session.
    """
    os.environ["APP_MODE"] = app_mode.value
    try:
        scope = account_scope or resolve_account_scope(app_mode)
    except RuntimeError as exc:
        return 1, {"message": str(exc), "lock_cleared": False}

    root = Path(data_root) if data_root else Path(resolve_data_root(app_mode))
    path = lock_path_for_scope(scope, root)
    record = read_session_lock(path)
    if record is None:
        return 1, {
            "message": f"no active session for this account_scope ({scope})",
            "account_scope": scope,
            "app_mode": app_mode.value,
            "lock_cleared": False,
        }

    pid = int(record.get("pid") or 0)
    port = int(record.get("port") or default_api_port(app_mode))
    session_id = str(record.get("session_id") or "")
    lock_app_mode = str(record.get("app_mode") or app_mode.value)
    was_healthy = session_is_healthy(record)
    terminated = False
    term_detail = "not running"
    if pid > 0 and pid_alive(pid):
        terminated, term_detail = _terminate_pid(pid, term_wait_sec=term_wait_sec)
    elif pid > 0:
        term_detail = "pid already dead (zombie lock)"

    if path.is_file():
        if not was_healthy:
            mark_session_zombie(path)
        try:
            path.unlink(missing_ok=True)
            lock_cleared = True
        except OSError:
            lock_cleared = False
    else:
        lock_cleared = True

    return 0, {
        "message": "session shutdown complete",
        "app_mode": lock_app_mode or app_mode.value,
        "account_scope": scope,
        "account_scope_masked": mask_account_scope(scope),
        "pid": pid,
        "pid_terminated": terminated or not pid_alive(pid),
        "termination": term_detail,
        "port": port,
        "session_id": session_id,
        "session_status": _SESSION_STATUS_ZOMBIE if not was_healthy else _SESSION_STATUS_HEALTHY,
        "lock_cleared": lock_cleared,
        "lock_path": str(path),
    }


def preflight_startup(
    *,
    app_mode: AppMode,
    port: int,
    account_scope: str | None = None,
    data_root: str | Path | None = None,
) -> tuple[int, str]:
    """
    start.sh preflight — returns (exit_code, message).

    Exit codes: 0 ok, 2 LIVE gate, 3 session active, 4 port conflict.
    """
    os.environ["APP_MODE"] = app_mode.value
    os.environ["IG_API_PORT"] = str(int(port))

    if app_mode is AppMode.LIVE:
        try:
            validate_live_armed(app_mode)
        except RuntimeError as exc:
            return 2, str(exc)

    try:
        scope = account_scope or resolve_account_scope(app_mode)
    except RuntimeError as exc:
        return 2, str(exc)

    root = Path(data_root) if data_root else Path(resolve_data_root(app_mode))
    os.environ["IG_ACCOUNT_SCOPE"] = scope
    os.environ["IG_DATA_ROOT"] = str(root.resolve())

    active_path, active = find_active_session(scope, root)
    if active_path is not None and active is not None:
        pid = active.get("pid")
        return 3, (
            f"session already active for account_scope={scope} "
            f"(pid={pid}, lock={active_path.name})"
        )

    foreign = port_bound_by_foreign(port)
    if foreign is not None:
        return 4, f"port {port} bound by foreign process pid={foreign}"

    return 0, f"preflight ok mode={app_mode.value} scope={scope} port={port}"


def acquire_session_lock() -> tuple[bool, str]:
    """Runtime acquire — call from main.py after apply_app_mode_to_environ."""
    global _acquired, _acquired_path

    if os.environ.get("IG_AGENT_ALLOW_MULTI_INSTANCE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return True, "multi-instance override"

    if os.environ.get("IG_AGENT_PYTEST") == "1":
        return True, "pytest skip"

    mode = resolve_app_mode()
    scope = resolve_account_scope(mode)
    root = Path(resolve_data_root(mode))
    path = lock_path_for_scope(scope, root)
    my_pid = os.getpid()
    port = int(os.environ.get("IG_API_PORT", str(default_api_port(mode))))

    if _acquired and _acquired_path == path:
        record = read_session_lock(path)
        if record and int(record.get("pid") or 0) == my_pid:
            return True, "ok"

    active_path, active = find_active_session(scope, root)
    if active_path is not None and active is not None:
        holder = int(active.get("pid") or 0)
        if holder != my_pid:
            return (
                False,
                f"healthy session holds account_scope={scope} (pid={holder})",
            )

    try:
        write_session_lock(path, pid=my_pid, port=port, account_scope=scope)
        _acquired = True
        _acquired_path = path
        import atexit

        atexit.register(release_session_lock)
        log_engine(
            f"session_lock acquired pid={my_pid} scope={scope} port={port} lock={path.name}"
        )
        return True, "ok"
    except OSError as exc:
        return False, f"could not acquire session lock: {type(exc).__name__}: {exc}"


def release_session_lock() -> None:
    global _acquired, _acquired_path
    if not _acquired or _acquired_path is None:
        return
    my_pid = os.getpid()
    record = read_session_lock(_acquired_path)
    holder = int(record.get("pid") or 0) if record else 0
    if holder in (0, my_pid):
        try:
            _acquired_path.unlink(missing_ok=True)
        except OSError:
            pass
    _acquired = False
    _acquired_path = None


def reset_session_lock_state_for_tests() -> None:
    global _acquired, _acquired_path
    _acquired = False
    _acquired_path = None


def _cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="IG Agent session lock preflight")
    sub = parser.add_subparsers(dest="command", required=True)

    pre = sub.add_parser("preflight", help="Preflight before supervisor launch")
    pre.add_argument("--mode", required=True, choices=sorted(_VALID_APP_MODES))
    pre.add_argument("--port", type=int, default=None)
    pre.add_argument("--config", default="")
    pre.add_argument("--data-root", default="")

    shut = sub.add_parser("shutdown", help="Graceful shutdown for account-scoped session")
    shut.add_argument("--mode", required=True, choices=sorted(_VALID_APP_MODES))
    shut.add_argument("--data-root", default="")
    shut.add_argument("--account-scope", default="")
    shut.add_argument("--term-wait", type=float, default=15.0)

    args = parser.parse_args(argv)
    if args.command == "preflight":
        mode = parse_app_mode(args.mode)
        port = args.port if args.port is not None else default_api_port(mode)
        if args.config:
            os.environ["IG_AGENT_CONFIG"] = args.config
        data_root = args.data_root or None
        code, msg = preflight_startup(app_mode=mode, port=port, data_root=data_root)
        if code != 0:
            print(msg, file=sys.stderr)
        else:
            print(msg)
        return code
    if args.command == "shutdown":
        mode = parse_app_mode(args.mode)
        scope = args.account_scope.strip() or None
        data_root = args.data_root or None
        code, summary = shutdown_session(
            app_mode=mode,
            account_scope=scope,
            data_root=data_root,
            term_wait_sec=float(args.term_wait),
        )
        if code != 0:
            print(summary.get("message", "shutdown failed"), file=sys.stderr)
            return code
        print(
            f"shutdown ok app_mode={summary.get('app_mode')} "
            f"account_scope={summary.get('account_scope_masked')} "
            f"pid={summary.get('pid')} terminated={summary.get('pid_terminated')} "
            f"lock_cleared={summary.get('lock_cleared')}"
        )
        return 0
    return 1


_VALID_APP_MODES = ("DEMO", "LIVE", "TESTBED")


if __name__ == "__main__":
    raise SystemExit(_cli_main())
