"""
Desk Support Wrapper — out-of-process AI monitoring for IG Agent Trading Desk.

Complements ``runtime.trading_desk_liveness``:

* **trading_desk_liveness** (in-process): runs inside ``main.py`` when the agent is
  up. Detects stale IG sync, inactive position supervision, and unmonitored opens;
  nudges recovery via background threads without blocking API handlers.

* **desk_support_wrapper** (out-of-process): standalone daemon that survives agent
  death. Polls health/liveness endpoints and process state every 15–30s; clears
  zombie session locks, kills stuck ``session_ready.py`` piles, runs anti-zombie
  recovery, and spawns offline ``manage_live_positions --supervise-loop`` when REST
  is degraded but broker positions remain open.

Launch::

    IG_AGENT_CONFIG=config/config_v31_demo_throughput.json \\
      PYTHONPATH=src python3 -m runtime.desk_support_wrapper

Or via ``scripts/desk_support_wrapper.sh`` (launchd-friendly).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.paths import data_dir, find_python_executable, project_root

_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "poll_interval_sec": 20.0,
    "max_restarts_per_hour": 3,
    "manual_stop_override_sec": 1800.0,
    "trade_ready_timeout_sec": 180.0,
    "manager_tick_stale_sec": 60.0,
    "restart_cooldown_sec": 120.0,
    "api_port": 8080,
    "supervise_timeout_sec": 120.0,
    "supervise_loop_poll_sec": 30.0,
    "http_timeout_sec": 5.0,
    "term_wait_sec": 30.0,
    # Hung-boot reaper: a session_ready starter alive this long while the API is
    # still down is a wedged boot — reap the whole tree (reaps zombie children)
    # and retry, rather than deferring to it forever.
    "boot_hang_sec": 240.0,
    # Escalation no longer stops recovery permanently: after the restart cap is
    # hit, back off for this long then resume trying (with an alert), so a
    # flapping agent never goes dark indefinitely.
    "escalation_backoff_sec": 600.0,
    # Dead-man's switch: alert to Telegram once the API has been down this long,
    # and again when it recovers.
    "down_alert_sec": 300.0,
}

_AUDIT_PATH = data_dir() / "desk_support_audit.jsonl"
_stop = False


def _load_wrapper_config(cfg: Any | None = None) -> dict[str, Any]:
    out = dict(_DEFAULTS)
    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            return out
    block = (cfg.get("desk_support_wrapper") if isinstance(cfg, dict) else None) or {}
    if hasattr(cfg, "get"):
        block = cfg.get("desk_support_wrapper") or {}
    if isinstance(block, dict):
        for key, val in block.items():
            if key.startswith("_"):
                continue
            out[key] = val
    try:
        out["api_port"] = int(
            os.environ.get("IG_API_PORT", out.get("api_port", 8080))
        )
    except (TypeError, ValueError):
        out["api_port"] = 8080
    return out


def _audit(event: str, detail: dict[str, Any]) -> None:
    row = {"ts": time.time(), "event": event, **detail}
    try:
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except OSError:
        pass
    log_engine(f"DeskSupport: {event} {detail}")


def _fetch_json(
    path: str,
    *,
    port: int,
    method: str = "GET",
    timeout: float = 5.0,
) -> tuple[dict[str, Any] | None, str | None]:
    url = f"http://127.0.0.1:{int(port)}{path}"
    try:
        req = urllib.request.Request(
            url,
            method=method,
            headers={"User-Agent": "DeskSupportWrapper/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body) if body.strip() else {}
            return (data if isinstance(data, dict) else {"raw": data}), None
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
            data = json.loads(body) if body.strip() else {}
            if isinstance(data, dict):
                return data, f"http_{exc.code}"
        except Exception:
            pass
        return None, f"http_{exc.code}"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return None, type(exc).__name__


def port_bound(port: int, *, timeout_sec: float = 1.0) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(max(0.2, float(timeout_sec)))
            return sock.connect_ex(("127.0.0.1", int(port))) == 0
    except OSError:
        return False


def process_state(pid: int) -> str | None:
    """Return first ps state letter (R,S,Z,...) or None when process is gone."""
    if pid <= 0:
        return None
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "state=", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if result.returncode != 0:
            return None
        raw = (result.stdout or "").strip()
        return raw[:1] if raw else None
    except (OSError, subprocess.SubprocessError):
        return None


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def is_zombie(pid: int) -> bool:
    return process_state(pid) == "Z"


def process_age_sec(pid: int) -> float | None:
    """Elapsed wall time for *pid* in seconds (macOS/Linux ``ps etime``)."""
    if pid <= 0:
        return None
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "etime=", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if result.returncode != 0:
            return None
        raw = (result.stdout or "").strip()
        if not raw:
            return None
        # Formats: MM:SS | HH:MM:SS | DD-HH:MM:SS
        days = 0
        if "-" in raw:
            day_s, raw = raw.split("-", 1)
            days = int(day_s)
        parts = [int(x) for x in raw.split(":")]
        if len(parts) == 2:
            mins, secs = parts
            hours = 0
        elif len(parts) == 3:
            hours, mins, secs = parts
        else:
            return None
        return float(days * 86400 + hours * 3600 + mins * 60 + secs)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def list_main_py_pids() -> list[int]:
    pids: list[int] = []
    try:
        result = subprocess.run(
            ["/usr/bin/pgrep", "-f", "src/main.py"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
    except (OSError, subprocess.SubprocessError):
        pass
    return sorted(set(pids))


def list_session_ready_pids() -> list[int]:
    """Return Python PIDs running session_ready.py (exclude shell wrappers)."""
    pids: list[int] = []
    try:
        result = subprocess.run(
            ["/usr/bin/pgrep", "-f", "scripts/session_ready.py"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
    except (OSError, subprocess.SubprocessError):
        pass
    return sorted(set(pids))


def list_trade_support_pids() -> list[int]:
    """Return PIDs for the always-on trade_support wrapper."""
    pids: list[int] = []
    try:
        result = subprocess.run(
            ["/usr/bin/pgrep", "-f", "runtime.trade_support_wrapper"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
    except (OSError, subprocess.SubprocessError):
        pass
    return sorted(set(pids))


def _child_pids(parent: int) -> list[int]:
    if parent <= 0:
        return []
    try:
        result = subprocess.run(
            ["/usr/bin/pgrep", "-P", str(int(parent))],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return [int(x) for x in (result.stdout or "").split() if x.strip().isdigit()]
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def process_tree_pids(root: int) -> list[int]:
    """Return ``root`` and all descendant PIDs (depth-first, deduped)."""
    if root <= 0:
        return []
    seen: list[int] = []
    stack = [int(root)]
    while stack:
        pid = stack.pop()
        if pid in seen or pid <= 0:
            continue
        seen.append(pid)
        stack.extend(_child_pids(pid))
    return seen


def port_listener_pid(port: int) -> int | None:
    try:
        result = subprocess.run(
            ["lsof", "-iTCP:%d" % int(port), "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                return int(line)
    except (OSError, subprocess.SubprocessError):
        pass
    return None


_V32_DEFAULT_PORTS = (8080, 8081)
_V32_PORT_ACCOUNTS: dict[int, tuple[str, str, str]] = {
    8080: ("Z6BAH4", "QUANT_SNIPER", "state_cfd"),
    8081: ("Z6BAH3", "MACRO_SENTINEL", "state_sb"),
}


def v32_dual_port_active() -> bool:
    """True when v32 twin engines are expected — sibling mains must not be killed."""
    try:
        from system.engine_cli import is_v32_dual_port_mode, parse_engine_cli

        if is_v32_dual_port_mode() or parse_engine_cli().dual_port_mode:
            return True
    except Exception:
        if os.environ.get("IG_V32_DUAL_PORT", "").strip() == "1":
            return True
    try:
        from system.paths import shared_state_dir

        marker = shared_state_dir() / "v32_dual_supervision.json"
        if marker.is_file():
            raw = json.loads(marker.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("dual_port"):
                return True
    except Exception:
        pass
    # Both watch ports healthy/listening implies dual supervision even without marker.
    watch = dual_watch_ports()
    if len(watch) >= 2 and all(port_bound(p) for p in watch[:2]):
        return True
    return False


def dual_watch_ports() -> list[int]:
    """CFD/SB ports under v32 dual supervision."""
    ports: list[int] = []
    try:
        from system.paths import shared_state_dir

        marker = shared_state_dir() / "v32_dual_supervision.json"
        if marker.is_file():
            raw = json.loads(marker.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for p in raw.get("ports") or []:
                    try:
                        ports.append(int(p))
                    except (TypeError, ValueError):
                        pass
    except Exception:
        pass
    env_ports = os.environ.get("IG_V32_WATCH_PORTS", "").strip()
    if env_ports:
        for chunk in env_ports.split(","):
            chunk = chunk.strip()
            if chunk.isdigit():
                ports.append(int(chunk))
    if not ports:
        ports = list(_V32_DEFAULT_PORTS)
    return sorted(set(ports))


def dual_protected_listener_pids() -> set[int]:
    """Listeners on healthy dual ports — never SIGTERM/SIGKILL during recovery."""
    protected: set[int] = set()
    for port in dual_watch_ports():
        if not port_bound(port):
            continue
        listener = port_listener_pid(port)
        if listener and listener > 0:
            protected.add(listener)
    return protected


def dual_down_ports() -> list[int]:
    return [p for p in dual_watch_ports() if not port_bound(p)]


def stale_lock_diagnosis(
    *,
    port: int,
    data_root: Path | None = None,
) -> dict[str, Any]:
    """Detect session/instance locks held by dead or zombie PIDs."""
    issues: list[str] = []
    lock_pid: int | None = None
    lock_path: str | None = None
    lock_status = "absent"
    holder_alive = False
    holder_zombie = False

    try:
        from runtime.app_mode import resolve_app_mode, resolve_data_root
        from runtime.session_lock import (
            lock_path_for_scope,
            read_session_lock,
            resolve_account_scope,
            session_is_healthy,
        )

        mode = resolve_app_mode()
        scope = resolve_account_scope(mode)
        root = Path(data_root) if data_root else Path(resolve_data_root(mode))
        path = lock_path_for_scope(scope, root)
        record = read_session_lock(path)
        if record:
            lock_path = str(path)
            lock_pid = int(record.get("pid") or 0) or None
            healthy = session_is_healthy(record)
            lock_status = "healthy" if healthy else "stale"
            if lock_pid:
                holder_alive = pid_alive(lock_pid)
                holder_zombie = is_zombie(lock_pid)
                if not healthy:
                    issues.append("session_lock_stale")
                if holder_zombie:
                    issues.append("session_lock_zombie_pid")
                if lock_pid and not holder_alive and path.is_file():
                    issues.append("session_lock_dead_pid")
    except Exception as exc:
        issues.append(f"session_lock_check_error:{type(exc).__name__}")

    instance_holder: int | None = None
    try:
        from system.identity.instance_lock import lock_path, read_lock_holder

        ipath = lock_path(port)
        instance_holder = read_lock_holder(ipath)
        if instance_holder and not pid_alive(instance_holder):
            issues.append("instance_lock_dead_pid")
        elif instance_holder and is_zombie(instance_holder):
            issues.append("instance_lock_zombie_pid")
    except Exception as exc:
        issues.append(f"instance_lock_check_error:{type(exc).__name__}")

    listener = port_listener_pid(port)
    if listener and lock_pid and listener != lock_pid:
        issues.append("port_listener_pid_mismatch")
    if not port_bound(port) and lock_pid and holder_alive:
        issues.append("lock_alive_but_port_down")

    return {
        "issues": issues,
        "lock_path": lock_path,
        "lock_pid": lock_pid,
        "lock_status": lock_status,
        "holder_alive": holder_alive,
        "holder_zombie": holder_zombie,
        "instance_holder": instance_holder,
        "port_listener": listener,
    }


def epic_cap_breach(
    positions: dict[str, Any] | None,
    *,
    max_per_epic: int,
) -> tuple[bool, dict[str, int]]:
    if not positions or max_per_epic <= 0:
        return False, {}
    rows = positions.get("positions") or []
    counts: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        epic = str(row.get("epic") or "").strip()
        if epic:
            counts[epic] = counts.get(epic, 0) + 1
    breached = {epic: n for epic, n in counts.items() if n > max_per_epic}
    return bool(breached), breached


@dataclass
class MonitorSnapshot:
    ts: float = field(default_factory=time.time)
    api_up: bool = False
    health: dict[str, Any] | None = None
    liveness: dict[str, Any] | None = None
    manager: dict[str, Any] | None = None
    positions: dict[str, Any] | None = None
    port_bound: bool = False
    main_pids: list[int] = field(default_factory=list)
    session_ready_pids: list[int] = field(default_factory=list)
    lock_diag: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


@dataclass
class DeskSupportState:
    restart_times: list[float] = field(default_factory=list)
    last_restart_mono: float = 0.0
    trade_ready_false_since: float | None = None
    supervise_loop_pid: int | None = None
    escalated: bool = False
    escalated_until_mono: float = 0.0
    api_down_since: float | None = None
    started_mono: float = field(default_factory=time.monotonic)
    spawned_session_ready: set[int] = field(default_factory=set)
    session_ready_first_seen: dict[int, float] = field(default_factory=dict)
    down_alert_sent: bool = False


class DeskSupportWrapper:
    """Out-of-process Trading Desk supervisor — polls and remediates."""

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        self.cfg = _load_wrapper_config(cfg)
        self.port = int(self.cfg["api_port"])
        self.state = DeskSupportState()
        if not os.environ.get("APP_MODE"):
            os.environ.setdefault("APP_MODE", "DEMO")

    def collect_snapshot(self) -> MonitorSnapshot:
        snap = MonitorSnapshot()
        timeout = float(self.cfg.get("http_timeout_sec", 5.0))
        snap.port_bound = port_bound(self.port)
        snap.main_pids = list_main_py_pids()
        snap.session_ready_pids = list_session_ready_pids()
        snap.lock_diag = stale_lock_diagnosis(port=self.port)

        # Track how long each session_ready starter has been observed so a wedged
        # boot can be aged out. Prune entries for starters that have exited.
        now_mono = time.monotonic()
        seen = self.state.session_ready_first_seen
        for pid in snap.session_ready_pids:
            seen.setdefault(pid, now_mono)
        for pid in list(seen.keys()):
            if pid not in snap.session_ready_pids:
                seen.pop(pid, None)
                self.state.spawned_session_ready.discard(pid)

        if snap.port_bound:
            snap.health, _ = _fetch_json(
                "/api/health", port=self.port, timeout=timeout
            )
            snap.api_up = snap.health is not None
            if snap.api_up:
                snap.liveness, _ = _fetch_json(
                    "/api/trading_desk/liveness", port=self.port, timeout=timeout
                )
                snap.manager, _ = _fetch_json(
                    "/api/position_manager/status", port=self.port, timeout=timeout
                )
                snap.positions, _ = _fetch_json(
                    "/api/positions/live", port=self.port, timeout=timeout
                )
        else:
            snap.api_up = False

        snap.problems = self._diagnose(snap)
        return snap

    def _diagnose(self, snap: MonitorSnapshot) -> list[str]:
        problems: list[str] = []
        stale_mgr_sec = float(self.cfg.get("manager_tick_stale_sec", 60.0))

        if snap.lock_diag.get("issues"):
            problems.extend(snap.lock_diag["issues"])

        zombies = [p for p in snap.main_pids if is_zombie(p)]
        if zombies:
            problems.append(f"main_py_zombie:{zombies}")

        if len(snap.session_ready_pids) > 1:
            foreign = [
                p
                for p in snap.session_ready_pids
                if p not in self.state.spawned_session_ready
            ]
            if len(foreign) > 1:
                problems.append(f"stuck_session_ready:{foreign}")

        # Hung-boot detection — the failure mode that caused the 76-minute
        # outage. A session_ready starter (mine or foreign) alive well past the
        # boot budget while the API port is still down is wedged. A defunct
        # main.py child under such a starter confirms it. Neither the watchdog
        # (defers on "booting") nor the old escalation path recovered this.
        boot_hang_sec = float(self.cfg.get("boot_hang_sec", 240.0))
        now_mono = time.monotonic()
        if not snap.port_bound and snap.session_ready_pids:
            wedged = [
                p
                for p in snap.session_ready_pids
                if now_mono - self.state.session_ready_first_seen.get(p, now_mono)
                >= boot_hang_sec
            ]
            if wedged:
                problems.append(f"hung_boot:{wedged}")
        # A zombie main.py child under a live session_ready is a dead boot that
        # its stuck parent never reaped — reap the parent tree regardless of age.
        for sr in snap.session_ready_pids:
            zchildren = [c for c in _child_pids(sr) if is_zombie(c)]
            if zchildren:
                problems.append(f"hung_boot_zombie_child:{sr}")
                break

        if not snap.port_bound:
            problems.append("api_port_down")
        elif not snap.api_up:
            problems.append("api_health_unreachable")

        health = snap.health or {}
        if snap.api_up:
            if not health.get("ok"):
                problems.append("health_not_ok")
            if health.get("trade_ready") is False:
                problems.append("trade_ready_false")
            if health.get("trading_loops_running") is False:
                problems.append("trading_loops_stopped")

        pos = snap.positions or {}
        open_count = int(pos.get("count") or 0)
        unmonitored = int(pos.get("unmonitored") or 0)
        verdict = str(pos.get("verdict") or "")
        ts_block = pos.get("trade_support") or {}
        try:
            ts_broker_open = int(ts_block.get("broker_open") or 0)
        except (TypeError, ValueError):
            ts_broker_open = 0
        sot = pos.get("broker_open_sot") or {}
        try:
            sot_count = int(sot.get("count") if sot.get("count") is not None else -1)
        except (TypeError, ValueError):
            sot_count = -1
        flat_book = (
            open_count == 0
            and ts_broker_open == 0
            and (sot_count in (-1, 0))
            and verdict in ("FLAT", "HEALTHY", "")
        )

        liv = snap.liveness or {}
        if liv and not liv.get("ok"):
            # Flat false-stale must not escalate to recover_and_supervise.
            liv_open = bool(liv.get("has_open_risk")) or int(liv.get("open_count") or 0) > 0
            if not (flat_book and not liv_open):
                problems.append("liveness_degraded")

        if open_count > 0 and unmonitored > 0:
            problems.append(f"unmonitored_positions:{unmonitored}")
        if pos.get("stale") and not flat_book:
            problems.append("positions_stale")
        if verdict == "DEGRADED" and not flat_book:
            problems.append("positions_degraded")

        mgr = snap.manager or {}
        if open_count > 0:
            tick_count = int(mgr.get("tick_count") or 0)
            last_tick = float(mgr.get("last_tick_at") or 0)
            tick_age = (time.time() - last_tick) if last_tick > 0 else None
            if tick_count == 0 and mgr.get("active"):
                problems.append("manager_tick_zero")
            if tick_age is not None and tick_age > stale_mgr_sec:
                problems.append(f"manager_tick_stale:{int(tick_age)}s")

        try:
            from system.config_loader import get_config

            cfg = get_config()
            max_epic = int(cfg.get("max_positions_per_epic") or 0)
            breached, detail = epic_cap_breach(pos, max_per_epic=max_epic)
            if breached:
                problems.append(f"epic_cap_breach:{detail}")
        except Exception:
            pass

        # trade_support SoT freshness — kickstart when status goes stale
        # (even when flat — stale SoT lies about risk). Also heal when API
        # reports heal_recommended / false-flat vs snapshot.
        try:
            ts_path = data_dir() / "trade_support_status.json"
            if ts_path.is_file():
                raw = json.loads(ts_path.read_text(encoding="utf-8"))
                age = time.time() - float(raw.get("ts") or 0)
                ts_stale_sec = float(self.cfg.get("trade_support_stale_sec", 90.0))
                if age > ts_stale_sec:
                    problems.append(f"trade_support_stale:{int(age)}s")
                # False-flat SoT: status says 0 but snapshot has opens.
                try:
                    from runtime import broker_snapshot

                    snap = broker_snapshot.read_snapshot(max_age_sec=None) or {}
                    snap_n = int(
                        snap.get("count") or len(snap.get("positions") or [])
                    )
                    status_n = int(raw.get("broker_open") or 0)
                    if status_n == 0 and snap_n > 0:
                        problems.append(
                            f"trade_support_sot_lie:status0_snap{snap_n}"
                        )
                except Exception:
                    pass
            if not list_trade_support_pids():
                problems.append("trade_support_down")
        except Exception:
            pass

        return problems

    def _manual_stop_blocks(self) -> tuple[bool, float | None]:
        try:
            from system.shutdown_cleanup import manual_stop_active

            path = data_dir() / "state" / "manual_stop.json"
            if not manual_stop_active(max_age_sec=86400.0):
                return False, None
            age: float | None = None
            if path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                age = time.time() - float(raw.get("ts") or 0)
            override = float(self.cfg.get("manual_stop_override_sec", 1800.0))
            if age is not None and age >= override:
                return False, age
            return True, age
        except Exception:
            return False, None

    def _restart_allowed(self) -> bool:
        if self.state.escalated:
            return False
        now = time.time()
        window = 3600.0
        fresh = [t for t in self.state.restart_times if now - t < window]
        self.state.restart_times = fresh
        cap = int(self.cfg.get("max_restarts_per_hour", 3))
        return len(fresh) < cap

    def _cooldown_ready(self, key: str = "restart") -> bool:
        cooldown = float(self.cfg.get("restart_cooldown_sec", 120.0))
        return time.monotonic() - self.state.last_restart_mono >= cooldown

    def _signal_pid(self, pid: int, sig: int) -> bool:
        if pid <= 0 or pid == os.getpid():
            return False
        try:
            os.kill(pid, sig)
            return True
        except OSError:
            return False

    def _terminate_pids(
        self,
        pids: list[int],
        *,
        label: str,
        term_wait: float | None = None,
        protected_pids: set[int] | None = None,
    ) -> list[int]:
        wait = float(term_wait if term_wait is not None else self.cfg.get("term_wait_sec", 30.0))
        protected = protected_pids or set()
        signalled: list[int] = []
        for pid in pids:
            if pid in protected:
                _audit("sigterm_skipped", {"pid": pid, "label": label, "reason": "dual_sibling"})
                continue
            if self._signal_pid(pid, signal.SIGTERM):
                signalled.append(pid)
                _audit("sigterm", {"pid": pid, "label": label})
        if not signalled:
            return []
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            alive = [p for p in signalled if pid_alive(p)]
            if not alive:
                break
            time.sleep(0.5)
        for pid in signalled:
            if pid in protected:
                continue
            if pid_alive(pid):
                self._signal_pid(pid, signal.SIGKILL)
                _audit("sigkill", {"pid": pid, "label": label})
        return signalled

    def _clear_locks(self) -> list[str]:
        removed: list[str] = []
        try:
            from runtime.app_mode import resolve_app_mode, resolve_data_root
            from runtime.session_lock import (
                clear_stale_lock,
                lock_path_for_scope,
                resolve_account_scope,
            )

            mode = resolve_app_mode()
            scope = resolve_account_scope(mode)
            root = Path(resolve_data_root(mode))
            spath = lock_path_for_scope(scope, root)
            if clear_stale_lock(spath) or not spath.is_file():
                if not spath.is_file():
                    removed.append(str(spath))
            elif spath.is_file():
                try:
                    spath.unlink(missing_ok=True)
                    removed.append(str(spath))
                except OSError:
                    pass
        except Exception as exc:
            _audit("lock_clear_error", {"error": str(exc)})

        try:
            from system.identity.instance_lock import force_release_instance_lock

            force_release_instance_lock()
            removed.append("instance_lock")
        except Exception as exc:
            _audit("instance_lock_clear_error", {"error": str(exc)})

        for legacy in (
            data_dir() / ".ig_agent_v29.lock",
            data_dir() / ".ig_agent_v31.lock",
        ):
            if legacy.is_file():
                try:
                    legacy.unlink(missing_ok=True)
                    removed.append(str(legacy))
                except OSError:
                    pass
        return removed

    def anti_zombie_recovery(self, *, reason: str) -> dict[str, Any]:
        """mark_manual_stop → TERM stale PIDs → rm locks → restart agent(s)."""
        summary: dict[str, Any] = {"reason": reason, "started": False}
        dual = v32_dual_port_active()
        summary["dual_port"] = dual
        protected = dual_protected_listener_pids() if dual else set()
        if dual:
            summary["protected_pids"] = sorted(protected)
            summary["down_ports"] = dual_down_ports()

        try:
            from system.shutdown_cleanup import mark_manual_stop

            mark_manual_stop(source="desk_support_wrapper")
            summary["manual_stop"] = True
        except Exception as exc:
            summary["manual_stop_error"] = str(exc)

        targets: list[int] = []
        targets.extend(list_session_ready_pids())
        if dual:
            ports_to_heal = dual_down_ports() or dual_watch_ports()
            for port in ports_to_heal:
                diag = stale_lock_diagnosis(port=port)
                if diag.get("lock_pid"):
                    targets.append(int(diag["lock_pid"]))
                if diag.get("instance_holder"):
                    targets.append(int(diag["instance_holder"]))
                listener = port_listener_pid(port)
                if listener:
                    targets.append(listener)
            for pid in list_main_py_pids():
                if pid not in protected:
                    targets.append(pid)
        else:
            targets.extend(list_main_py_pids())
            diag = stale_lock_diagnosis(port=self.port)
            if diag.get("lock_pid"):
                targets.append(int(diag["lock_pid"]))
            if diag.get("instance_holder"):
                targets.append(int(diag["instance_holder"]))
            listener = port_listener_pid(self.port)
            if listener:
                targets.append(listener)

        targets = sorted(
            {p for p in targets if p > 0 and p != os.getpid() and p not in protected}
        )
        summary["terminated"] = self._terminate_pids(
            targets, label="anti_zombie", protected_pids=protected
        )
        summary["locks_removed"] = self._clear_locks()

        try:
            from system.shutdown_cleanup import clear_manual_stop

            clear_manual_stop()
        except Exception:
            pass

        if dual:
            started = self._start_dual_heal()
        else:
            started = self._start_agent()
        summary["started"] = started
        if started:
            self.state.restart_times.append(time.time())
            self.state.last_restart_mono = time.monotonic()
        _audit("anti_zombie_recovery", summary)
        return summary

    def reap_hung_boot(self, *, reason: str) -> dict[str, Any]:
        """Kill wedged session_ready trees (reaping zombie children), clear locks, retry.

        Distinct from ``anti_zombie_recovery``: the culprit here is a live-but-stuck
        ``session_ready`` starter that will never bind the port. Killing the whole
        tree lets init reap any defunct ``main.py`` child, then a clean start can
        proceed. Counts against the restart budget.
        """
        summary: dict[str, Any] = {"reason": reason, "started": False}
        try:
            from system.shutdown_cleanup import mark_manual_stop

            mark_manual_stop(source="desk_support_hung_boot")
        except Exception:
            pass

        dual = v32_dual_port_active()
        protected = dual_protected_listener_pids() if dual else set()
        summary["dual_port"] = dual
        if dual:
            summary["protected_pids"] = sorted(protected)

        targets: list[int] = []
        for sr in list_session_ready_pids():
            targets.extend(process_tree_pids(sr))
        if dual:
            for port in dual_down_ports() or dual_watch_ports():
                diag = stale_lock_diagnosis(port=port)
                if diag.get("lock_pid"):
                    targets.append(int(diag["lock_pid"]))
            for pid in list_main_py_pids():
                if pid not in protected:
                    targets.append(pid)
        else:
            targets.extend(list_main_py_pids())
            diag = stale_lock_diagnosis(port=self.port)
            if diag.get("lock_pid"):
                targets.append(int(diag["lock_pid"]))
        targets = sorted(
            {p for p in targets if p > 0 and p != os.getpid() and p not in protected}
        )
        summary["terminated"] = self._terminate_pids(
            targets, label="hung_boot", protected_pids=protected
        )
        self.state.session_ready_first_seen.clear()
        self.state.spawned_session_ready.clear()
        summary["locks_removed"] = self._clear_locks()

        try:
            from system.shutdown_cleanup import clear_manual_stop

            clear_manual_stop()
        except Exception:
            pass

        if dual:
            started = self._start_dual_heal()
        else:
            started = self._start_agent()
        summary["started"] = started
        if started:
            self.state.restart_times.append(time.time())
            self.state.last_restart_mono = time.monotonic()
        _audit("reap_hung_boot", summary)
        return summary

    def _launch_v32_engine(self, port: int) -> bool:
        """Start a single v32 twin engine on *port* (never disturbs sibling)."""
        spec = _V32_PORT_ACCOUNTS.get(int(port))
        if not spec:
            return False
        account, origin, state_leaf = spec
        root = project_root()
        py = find_python_executable()
        data_root = data_dir()
        state_dir = data_root / state_leaf
        state_dir.mkdir(parents=True, exist_ok=True)
        log_dir = data_root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"v32_{state_leaf.replace('state_', '')}.log"
        pid_file = state_dir / "agent.pid"
        env = os.environ.copy()
        from kernel.ring_buffer import dual_port_shm_lane_from

        lane = dual_port_shm_lane_from(int(port), origin=origin, account=account)
        env.update(
            {
                "APP_MODE": env.get("APP_MODE", "DEMO"),
                "IG_AGENT_CONFIG": env.get(
                    "IG_AGENT_CONFIG", "config/config_v31_demo_throughput.json"
                ),
                "PYTHONPATH": str(root / "src"),
                "CORE_DETACHED": env.get("CORE_DETACHED", "FALSE"),
                "IG_V32_DUAL_PORT": "1",
                "IG_API_PORT": str(int(port)),
                "PORT": str(int(port)),
                "IG_ACCOUNT_ID": account,
                "IG_ACCOUNT_SCOPE": f"ig:{account}",
                "IG_ENGINE_ORIGIN": origin,
                "IG_SHM_RING_NAME": f"ig_agent_v33_shm_{lane}",
                "IG_SHM_RING_CREATE": "1",
                "IG_COCKPIT_SHM_NAME": f"ig_agent_v33_cockpit_{lane}",
            }
        )
        if int(port) == 8080:
            env["IG_AGENT_ORCHESTRATOR"] = "1"
        try:
            with log_file.open("a", encoding="utf-8") as logfh:
                proc = subprocess.Popen(
                    [
                        py,
                        str(root / "src" / "main.py"),
                        f"--port={int(port)}",
                        f"--account-id={account}",
                        f"--origin={origin}",
                    ],
                    cwd=str(root),
                    env=env,
                    stdout=logfh,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            pid_file.write_text(str(proc.pid), encoding="utf-8")
            _audit("v32_engine_spawned", {"port": int(port), "pid": proc.pid})
            return True
        except OSError as exc:
            _audit("v32_engine_spawn_failed", {"port": int(port), "error": str(exc)})
            return False

    def _start_dual_heal(self) -> bool:
        """Heal only down v32 ports — never restart healthy siblings."""
        down = dual_down_ports()
        if not down:
            return True
        if len(down) == len(dual_watch_ports()):
            root = project_root()
            script = root / "scripts" / "v32_runtime_start.sh"
            if script.is_file():
                try:
                    proc = subprocess.Popen(
                        ["/bin/bash", str(script), "start"],
                        cwd=str(root),
                        env=os.environ.copy(),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    _audit("v32_dual_start_spawned", {"pid": proc.pid, "ports": down})
                    return True
                except OSError as exc:
                    _audit("v32_dual_start_failed", {"error": str(exc)})
        ok = True
        for port in down:
            if not self._launch_v32_engine(port):
                ok = False
        return ok

    def _start_agent(self) -> bool:
        root = project_root()
        py = find_python_executable()
        env = os.environ.copy()
        env.setdefault("APP_MODE", "DEMO")
        env.setdefault(
            "IG_AGENT_CONFIG", "config/config_v31_demo_throughput.json"
        )
        env["PYTHONPATH"] = str(root / "src")
        script = root / "scripts" / "session_ready.py"
        if not script.is_file():
            return False
        try:
            proc = subprocess.Popen(
                [py, str(script), "--start-agent"],
                cwd=str(root),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.state.spawned_session_ready.add(proc.pid)
            _audit("session_ready_spawned", {"pid": proc.pid})
            return True
        except OSError as exc:
            _audit("session_ready_spawn_failed", {"error": str(exc)})
            return False

    def post_recover(self) -> dict[str, Any] | None:
        data, err = _fetch_json(
            "/api/trading_desk/recover",
            port=self.port,
            method="POST",
            timeout=float(self.cfg.get("http_timeout_sec", 5.0)),
        )
        _audit("trading_desk_recover", {"ok": data is not None, "error": err})
        return data

    def heal_trade_support(self) -> dict[str, Any]:
        """Kickstart trade_support when status is stale or the process is down."""
        summary: dict[str, Any] = {"ok": False, "action": "heal_trade_support"}
        if not self._cooldown_ready("heal_trade_support"):
            summary["skipped"] = True
            summary["reason"] = "cooldown"
            return summary
        uid = os.getuid()
        label = f"gui/{uid}/com.igagent.trade_support"
        try:
            kick = subprocess.run(
                ["launchctl", "kickstart", "-k", label],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            summary["launchctl"] = {
                "returncode": kick.returncode,
                "stderr": (kick.stderr or "")[:200],
            }
        except (OSError, subprocess.SubprocessError) as exc:
            summary["launchctl_error"] = str(exc)
        time.sleep(2.0)
        pids = list_trade_support_pids()
        if not pids:
            root = project_root()
            script = root / "scripts" / "trade_support_wrapper.sh"
            if script.is_file():
                try:
                    subprocess.Popen(
                        ["/bin/bash", str(script)],
                        cwd=str(root),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    summary["spawned_script"] = True
                    time.sleep(2.0)
                    pids = list_trade_support_pids()
                except OSError as exc:
                    summary["spawn_error"] = str(exc)
        summary["pids"] = pids
        summary["ok"] = bool(pids)
        self.state.last_restart_mono = time.monotonic()
        _audit("heal_trade_support", summary)
        return summary

    def run_supervise_once(self) -> dict[str, Any]:
        root = project_root()
        py = find_python_executable()
        env = os.environ.copy()
        env.setdefault(
            "IG_AGENT_CONFIG", "config/config_v31_demo_throughput.json"
        )
        env["PYTHONPATH"] = str(root / "src")
        script = root / "scripts" / "manage_live_positions.py"
        timeout = float(self.cfg.get("supervise_timeout_sec", 120.0))
        try:
            result = subprocess.run(
                [py, str(script), "--supervise"],
                cwd=str(root),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            out = {
                "returncode": result.returncode,
                "stdout_tail": (result.stdout or "")[-500:],
                "stderr_tail": (result.stderr or "")[-500:],
            }
            _audit("supervise_once", out)
            return out
        except subprocess.TimeoutExpired:
            _audit("supervise_once_timeout", {"timeout_sec": timeout})
            return {"error": "timeout"}

    def ensure_supervise_loop(self) -> int | None:
        if self.state.supervise_loop_pid and pid_alive(self.state.supervise_loop_pid):
            return self.state.supervise_loop_pid
        root = project_root()
        py = find_python_executable()
        env = os.environ.copy()
        env.setdefault(
            "IG_AGENT_CONFIG", "config/config_v31_demo_throughput.json"
        )
        env["PYTHONPATH"] = str(root / "src")
        script = root / "scripts" / "manage_live_positions.py"
        poll = float(self.cfg.get("supervise_loop_poll_sec", 30.0))
        try:
            proc = subprocess.Popen(
                [
                    py,
                    str(script),
                    "--supervise-loop",
                    "--poll-sec",
                    str(poll),
                ],
                cwd=str(root),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.state.supervise_loop_pid = proc.pid
            _audit("supervise_loop_spawned", {"pid": proc.pid})
            return proc.pid
        except OSError as exc:
            _audit("supervise_loop_failed", {"error": str(exc)})
            return None

    def escalate(self, *, reason: str) -> None:
        backoff = float(self.cfg.get("escalation_backoff_sec", 600.0))
        self.state.escalated = True
        self.state.escalated_until_mono = time.monotonic() + backoff
        _audit(
            "CRITICAL_ESCALATION",
            {
                "reason": reason,
                "restart_count": len(self.state.restart_times),
                "backoff_sec": backoff,
            },
        )
        log_engine(
            f"DeskSupport CRITICAL: {reason} — backing off {int(backoff)}s "
            "then resuming recovery"
        )
        # Escalation is exactly when a human needs to know — alert immediately.
        self._send_alert(
            f"Agent recovery escalated — {reason}. "
            f"Restart cap hit; backing off {int(backoff)}s then retrying. "
            "Manual intervention may be required.",
            dedupe_key="desk_support_escalation",
        )

    def _send_alert(self, message: str, *, dedupe_key: str | None = None) -> bool:
        """Out-of-process Telegram critical alert. Fail-open, never raises.

        Builds a notifier directly from config so it works without the agent's
        in-process telegram bootstrap. Message includes an operator token so it
        passes the executive-mode preserve filter.
        """
        try:
            from system.config_loader import get_config
            from system.telegram_notifier import TelegramNotifier

            cfg = get_config()
            data: dict[str, Any] = {}
            tg = getattr(cfg, "telegram", None)
            if isinstance(tg, dict):
                data = dict(tg)
            elif hasattr(cfg, "get"):
                raw = cfg.get("telegram")
                if isinstance(raw, dict):
                    data = dict(raw)
            notifier = TelegramNotifier.from_config(data)
            if not notifier.enabled:
                _audit("alert_skipped_disabled", {"message": message[:120]})
                return False
            text = f"🚨 IG Agent CRITICAL — manual intervention\n{message}"
            ok = notifier.send_now(text)
            _audit("alert_sent", {"ok": ok, "dedupe_key": dedupe_key})
            return bool(ok)
        except Exception as exc:
            _audit("alert_error", {"error": f"{type(exc).__name__}: {exc}"})
            return False

    def select_action(self, snap: MonitorSnapshot) -> str | None:
        # Escalation is now a timed backoff, not a permanent halt. Once the
        # backoff window elapses, resume recovery so a flapping agent never
        # stays dark indefinitely.
        if self.state.escalated:
            if time.monotonic() < self.state.escalated_until_mono:
                return None
            self.state.escalated = False
            self.state.restart_times = []
            _audit("escalation_backoff_elapsed", {"resuming": True})

        # Grace period after wrapper start — avoid acting during transient boot.
        if time.monotonic() - self.state.started_mono < 60.0:
            return None

        problems = set(snap.problems)
        health = snap.health or {}
        agent_healthy = (
            snap.api_up
            and snap.port_bound
            and health.get("ok") is True
            and health.get("trade_ready") is not False
        )

        if agent_healthy:
            self.state.api_down_since = None
        elif not snap.port_bound or not snap.api_up:
            if self.state.api_down_since is None:
                self.state.api_down_since = time.time()
        else:
            self.state.api_down_since = None

        api_down_sec = (
            time.time() - self.state.api_down_since
            if self.state.api_down_since is not None
            else 0.0
        )
        sustained_down = api_down_sec >= 45.0

        # Dead-man's switch — never be blind again. Alert once the API has been
        # down past the threshold, and again when it recovers.
        down_alert_sec = float(self.cfg.get("down_alert_sec", 300.0))
        if api_down_sec >= down_alert_sec and not self.state.down_alert_sent:
            self.state.down_alert_sent = True
            self._send_alert(
                f"Agent API down {int(api_down_sec)}s on port {self.port} — "
                "auto-recovery in progress.",
                dedupe_key="desk_support_api_down",
            )
        elif agent_healthy and self.state.down_alert_sent:
            self.state.down_alert_sent = False
            self._send_alert(
                f"Agent API recovered on port {self.port}.",
                dedupe_key="desk_support_api_recovered",
            )

        lock_issues = {
            p
            for p in problems
            if p.startswith(
                (
                    "session_lock_stale",
                    "session_lock_zombie_pid",
                    "session_lock_dead_pid",
                    "instance_lock_dead_pid",
                    "instance_lock_zombie_pid",
                    "main_py_zombie",
                    "lock_alive_but_port_down",
                    "port_listener_pid_mismatch",
                )
            )
        }
        # A wedged boot must be reaped, not deferred to. This takes priority over
        # anti_zombie_recovery because the culprit is a live session_ready that
        # would otherwise look like "boot in progress" forever.
        if any(
            p.startswith(("hung_boot", "hung_boot_zombie_child")) for p in problems
        ):
            return "reap_hung_boot"

        # Live young main.py must never be SIGTERM'd for hydrate-era port blips —
        # that storm was killing DEMO boots mid Gate-2 and leaving the desk dead.
        live_mains = [
            p
            for p in (snap.main_pids or [])
            if pid_alive(p) and not is_zombie(p)
        ]
        boot_grace = float(self.cfg.get("live_main_boot_grace_sec", 180.0))
        live_down_grace = float(self.cfg.get("live_main_api_down_grace_sec", 120.0))
        if live_mains:
            ages = [process_age_sec(p) for p in live_mains]
            ages_f = [a for a in ages if a is not None]
            min_age = min(ages_f) if ages_f else 0.0
            if min_age < boot_grace:
                agent_dead = False
            else:
                agent_dead = api_down_sec >= live_down_grace and (
                    "api_port_down" in problems
                    or "api_health_unreachable" in problems
                )
        else:
            agent_dead = sustained_down and (
                "api_port_down" in problems
                or "api_health_unreachable" in problems
                or bool(lock_issues)
                or sustained_down
            )
        if agent_dead and not agent_healthy:
            return "anti_zombie_recovery"

        if "trade_ready_false" in problems:
            if self.state.trade_ready_false_since is None:
                self.state.trade_ready_false_since = time.time()
            elif (
                time.time() - self.state.trade_ready_false_since
                > float(self.cfg.get("trade_ready_timeout_sec", 180.0))
            ):
                return "post_recover"
        else:
            self.state.trade_ready_false_since = None

        # Flat book: never recover_and_supervise for stale/liveness noise alone.
        pos = snap.positions or {}
        try:
            open_count = int(pos.get("count") or 0)
        except (TypeError, ValueError):
            open_count = 0
        flat_book = open_count <= 0 and str(pos.get("verdict") or "") in (
            "FLAT",
            "HEALTHY",
            "",
        )

        risk_prefixes = (
            "unmonitored_positions",
            "positions_stale",
            "positions_degraded",
            "liveness_degraded",
            "manager_tick_zero",
            "manager_tick_stale",
        )
        risk_hits = [
            p
            for p in problems
            if p in risk_prefixes or p.split(":")[0] in risk_prefixes
        ]
        if risk_hits:
            if flat_book and all(
                p.split(":")[0]
                in (
                    "positions_stale",
                    "positions_degraded",
                    "liveness_degraded",
                    "manager_tick_zero",
                    "manager_tick_stale",
                )
                for p in risk_hits
            ):
                return None
            return "recover_and_supervise"

        if any(p.startswith("epic_cap_breach") for p in problems):
            return "supervise_once"

        if any(
            p.startswith(
                ("trade_support_stale", "trade_support_down", "trade_support_sot_lie")
            )
            for p in problems
        ):
            return "heal_trade_support"

        if problems and any(p.startswith("stuck_session_ready") for p in problems):
            return "kill_stuck_session_ready"

        return None

    def execute_action(self, action: str, snap: MonitorSnapshot) -> dict[str, Any]:
        if action in ("anti_zombie_recovery", "reap_hung_boot"):
            if not self._restart_allowed():
                self.escalate(reason="max_restarts_per_hour exceeded")
                return {"skipped": True, "reason": "restart_cap"}
            if not self._cooldown_ready():
                return {"skipped": True, "reason": "cooldown"}
            blocked, age = self._manual_stop_blocks()
            if blocked and snap.port_bound:
                return {"skipped": True, "reason": "manual_stop", "age_sec": age}
            if action == "reap_hung_boot":
                return self.reap_hung_boot(reason=",".join(snap.problems[:5]))
            return self.anti_zombie_recovery(reason=",".join(snap.problems[:5]))

        if action == "post_recover":
            return {"recover": self.post_recover()}

        if action == "recover_and_supervise":
            out: dict[str, Any] = {"recover": self.post_recover()}
            open_count = int((snap.positions or {}).get("count") or 0)
            if open_count > 0 and not snap.api_up:
                out["supervise_loop"] = self.ensure_supervise_loop()
            elif open_count > 0:
                out["supervise_once"] = self.run_supervise_once()
            return out

        if action == "supervise_once":
            return self.run_supervise_once()

        if action == "heal_trade_support":
            return self.heal_trade_support()

        if action == "kill_stuck_session_ready":
            pids = snap.session_ready_pids
            if len(pids) > 1:
                # Keep newest; kill older stuck starters.
                victims = pids[:-1]
                return {
                    "killed": self._terminate_pids(victims, label="stuck_session_ready")
                }
        return {"skipped": True}

    def poll_once(self) -> dict[str, Any]:
        snap = self.collect_snapshot()
        action = self.select_action(snap)
        result: dict[str, Any] = {
            "ts": snap.ts,
            "problems": snap.problems,
            "action": action,
            "api_up": snap.api_up,
            "port_bound": snap.port_bound,
        }
        if action:
            result["outcome"] = self.execute_action(action, snap)
            _audit("poll_action", result)
        elif snap.problems:
            _audit("poll_degraded", {"problems": snap.problems})
        return result

    def run(self) -> None:
        global _stop
        interval = max(15.0, float(self.cfg.get("poll_interval_sec", 20.0)))
        log_engine(
            f"DeskSupport: wrapper armed poll={interval}s port={self.port} "
            f"max_restarts={self.cfg.get('max_restarts_per_hour')}"
        )
        _audit("wrapper_start", {"poll_interval_sec": interval, "port": self.port})
        while not _stop:
            try:
                self.poll_once()
            except Exception as exc:
                _audit("poll_error", {"error": f"{type(exc).__name__}: {exc}"})
            time.sleep(interval)
        _audit("wrapper_stop", {})


def _handle_signal(signum: int, _frame: Any) -> None:
    global _stop
    _stop = True
    log_engine(f"DeskSupport: signal {signum} — stopping")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Desk Support Wrapper daemon")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single poll cycle (for tests / manual check)",
    )
    parser.add_argument(
        "--poll-sec",
        type=float,
        default=None,
        help="Override poll interval",
    )
    args = parser.parse_args(argv)

    wrapper = DeskSupportWrapper()
    if args.poll_sec is not None:
        wrapper.cfg["poll_interval_sec"] = max(15.0, float(args.poll_sec))

    if not wrapper.cfg.get("enabled", True):
        print("desk_support_wrapper disabled in config", file=sys.stderr)
        return 0

    if args.once:
        print(json.dumps(wrapper.poll_once(), indent=2, default=str))
        return 0

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    wrapper.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
