"""Overnight supervision — launchd ownership, Safe to Leave bundle, armed state."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.paths import data_dir, project_root

_FRAGILE_ANCESTOR_MARKERS: tuple[str, ...] = (
    "cursor",
    "code helper",
    "electron",
    "terminal.app",
    "iterm",
    "warp",
    "kitty",
    "alacritty",
    "hyper",
)

_LAUNCHD_WATCHDOG_LABEL = "com.igagent.v25.watchdog"
_LAUNCHD_CAFF_LABEL = "com.igagent.v25.caffeinate"
_LAUNCHD_AGENT_LABEL = "com.igagent.v25"
_LAUNCHD_NIGHTLY_LABEL = "com.igagent.v29nightly"
_LAUNCHD_WEEKLY_LABEL = "com.igagent.v29weekly"
_LAUNCHD_BACKUP_LABEL = "com.igagent.v25backup"
_SUPERVISION_PLISTS: tuple[str, ...] = (
    "com.igagent.v25.caffeinate.plist",
    "com.igagent.v25.watchdog.plist",
)
_SCHEDULED_PLISTS: tuple[str, ...] = (
    "com.igagent.v25backup.plist",
    "com.igagent.v29nightly.plist",
    "com.igagent.v29weekly.plist",
)
_OVERNIGHT_ARMED_FILE = data_dir() / "state" / "overnight_armed.json"


def _launchd_job_loaded(label: str) -> bool:
    uid = os.getuid()
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{label}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        # launchctl is macOS-only — treat as "not loaded" on Linux CI/dev hosts.
        return False


def launchd_supervision_status() -> tuple[bool, str]:
    """True when launchd owns the watchdog (survives IDE / terminal close)."""
    wd = _launchd_job_loaded(_LAUNCHD_WATCHDOG_LABEL)
    agent = _launchd_job_loaded(_LAUNCHD_AGENT_LABEL)
    if wd:
        if agent:
            return True, "launchd watchdog + agent loaded"
        return True, "launchd watchdog loaded (watchdog starts main.py)"
    if agent:
        return True, "launchd agent loaded (watchdog may be manual)"
    return (
        False,
        "launchd not installed — run: ./scripts/install_launchd.sh (once per Mac)",
    )


def launchd_watchdog_active() -> bool:
    return _launchd_job_loaded(_LAUNCHD_WATCHDOG_LABEL)


def _launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _bootstrap_launchd_plist(plist_name: str) -> tuple[bool, str]:
    label = plist_name.replace(".plist", "")
    path = _launch_agents_dir() / plist_name
    if not path.is_file():
        return False, f"missing {plist_name}"
    domain = f"gui/{os.getuid()}"
    try:
        subprocess.run(
            ["launchctl", "bootout", f"{domain}/{label}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        result = subprocess.run(
            ["launchctl", "bootstrap", domain, str(path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False, "launchctl unavailable (macOS only)"
    if result.returncode == 0 or _launchd_job_loaded(label):
        return True, label
    err = (result.stderr or result.stdout or "").strip().splitlines()
    tail = err[-1] if err else f"exit {result.returncode}"
    return False, f"{label}: {tail}"


def ensure_launchd_supervision_loaded() -> tuple[bool, str]:
    """
    Load caffeinate + watchdog launchd jobs if plists are installed.
    Safe to call from Safe to Leave — does not stop a running agent.
    """
    if launchd_watchdog_active():
        return True, "launchd watchdog already active"

    missing = [
        p for p in _SUPERVISION_PLISTS if not (_launch_agents_dir() / p).is_file()
    ]
    if missing:
        root = project_root()
        install = root / "scripts" / "install_launchd.sh"
        if install.is_file():
            return (
                False,
                "supervision not installed — run: ./scripts/install_launchd.sh",
            )
        return False, f"missing LaunchAgents: {', '.join(missing)}"

    notes: list[str] = []
    for plist_name in _SUPERVISION_PLISTS:
        ok, detail = _bootstrap_launchd_plist(plist_name)
        if not ok:
            return False, detail
        notes.append(detail)

    if launchd_watchdog_active():
        return True, "loaded " + ", ".join(notes)
    return False, "bootstrap ran but watchdog not active — check watchdog_launchd.log"


def _listener_pid(port: int = 8080) -> int | None:
    try:
        result = subprocess.run(
            ["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        for line in (result.stdout or "").strip().splitlines():
            if line.strip().isdigit():
                return int(line.strip())
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


def _process_command(pid: int) -> str:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return (result.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _parent_pid(pid: int) -> int | None:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "ppid="],
            capture_output=True,
            text=True,
            timeout=3,
        )
        raw = (result.stdout or "").strip()
        if raw.isdigit():
            ppid = int(raw)
            return ppid if ppid > 1 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


def _fragile_ancestors(pid: int, *, max_depth: int = 12) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    current = pid
    for _ in range(max_depth):
        cmd = _process_command(current)
        lowered = cmd.lower()
        if any(marker in lowered for marker in _FRAGILE_ANCESTOR_MARKERS):
            hits.append((current, cmd))
        ppid = _parent_pid(current)
        if ppid is None or ppid == current:
            break
        current = ppid
    return hits


def agent_process_supervision_status(*, port: int = 8080) -> tuple[bool, str]:
    """True when agent is not tied to Cursor/terminal (fallback if launchd missing)."""
    launchd_ok, launchd_detail = launchd_supervision_status()
    if launchd_ok:
        return True, launchd_detail

    pid = _listener_pid(port)
    if pid is None:
        return False, f"nothing listening on :{port}"

    fragile = _fragile_ancestors(pid)
    if fragile:
        owner = fragile[0][1]
        if len(owner) > 80:
            owner = owner[:77] + "..."
        return (
            False,
            f"agent tied to IDE/terminal (pid={fragile[0][0]}: {owner}) — "
            "install launchd: ./scripts/install_launchd.sh",
        )

    cmd = _process_command(pid)
    if "main.py" in cmd:
        return (
            True,
            "agent detached from IDE (Desktop Launcher — launchd still recommended)",
        )
    return True, f"listener pid={pid}"


def overnight_supervision_summary(*, port: int = 8080) -> dict[str, Any]:
    launchd_ok, launchd_detail = launchd_supervision_status()
    agent_ok, agent_detail = agent_process_supervision_status(port=port)
    armed = read_overnight_armed()
    return {
        "launchd_ok": launchd_ok,
        "launchd_detail": launchd_detail,
        "launchd_watchdog": launchd_watchdog_active(),
        "nightly_job_loaded": _launchd_job_loaded(_LAUNCHD_NIGHTLY_LABEL),
        "weekly_job_loaded": _launchd_job_loaded(_LAUNCHD_WEEKLY_LABEL),
        "backup_job_loaded": _launchd_job_loaded(_LAUNCHD_BACKUP_LABEL),
        "scheduled_plists": list(_SCHEDULED_PLISTS),
        "agent_supervision_ok": agent_ok,
        "agent_supervision_detail": agent_detail,
        "overnight_ok": launchd_ok,
        "overnight_armed": armed.get("armed") is True,
        "overnight_armed_at": armed.get("armed_at"),
        "overnight_armed_source": armed.get("source"),
        "independent_of_cursor": launchd_ok,
    }


def read_overnight_armed() -> dict[str, Any]:
    if not _OVERNIGHT_ARMED_FILE.is_file():
        return {"armed": False}
    try:
        data = json.loads(_OVERNIGHT_ARMED_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return {"armed": False}


def mark_overnight_armed(*, source: str = "safe_to_leave") -> None:
    """Record that Safe to Leave passed — operator may close IDE/browser."""
    _OVERNIGHT_ARMED_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "armed": True,
        "armed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        + "Z",
        "source": source,
        "supervision": overnight_supervision_summary(),
    }
    _OVERNIGHT_ARMED_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def clear_overnight_armed() -> None:
    try:
        _OVERNIGHT_ARMED_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def prepare_overnight_bundle() -> tuple[bool, str]:
    """Safe to Leave preamble: ensure launchd supervision is loaded."""
    return ensure_launchd_supervision_loaded()
