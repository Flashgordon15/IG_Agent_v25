"""GUI Desk Supervisor — Phase 2 allowlisted self-heal ONLY.

Safe heals (ops plane). Never loosens gates / Instant / REST / strategy.
Never uses kill -9 / SIGKILL.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# --- Allowlist (explicit) ---------------------------------------------------
HEAL_PORT_HUNG = "port_hung_soft_recycle"
HEAL_LOOPS_NOT_ARMING = "loops_not_arming_unpause_or_recycle"
HEAL_UI_DOWN = "ui_restart_only"
HEAL_SILENCE_SOFT_PAUSE = "armed_silence_soft_pause_sb"
HEAL_REAPPLY_A2 = "reapply_a2_cfd_pause"
HEAL_ENSURE_BLEED_HALT = "ensure_operator_bleed_halt"

ALLOWED_HEALS = frozenset(
    {
        HEAL_PORT_HUNG,
        HEAL_LOOPS_NOT_ARMING,
        HEAL_UI_DOWN,
        HEAL_SILENCE_SOFT_PAUSE,
        HEAL_REAPPLY_A2,
        HEAL_ENSURE_BLEED_HALT,
    }
)

FORBIDDEN_ACTIONS = frozenset(
    {
        "kill_9",
        "sigkill",
        "raise_rest_cap",
        "reenable_instant_micro",
        "loosen_elastic_gate",
        "obi_fail_open",
        "strategy_rewrite",
        "allow_non_dow_global_unlock",
        "lift_a2_without_operator",
    }
)

# Durable operator bleed / halt locks — heal must NEVER POST /api/start while present.
OPERATOR_BLEED_LOCK_GLOB = "operator_bleed_lock_*.json"
OPERATOR_BLEED_LOCK_REASON_DEFAULT = "operator_halt_unacceptable_bleed"

DEFAULT_HEAL_CAP_PER_HOUR = int(os.environ.get("IG_GUI_SUP_HEAL_CAP_PER_HOUR", "2"))
DEFAULT_SILENCE_SOFT_PAUSE = os.environ.get("IG_GUI_SUP_SILENCE_SOFT_PAUSE", "1").strip() not in (
    "0",
    "false",
    "False",
    "no",
)
TERM_WAIT_SEC = float(os.environ.get("IG_GUI_SUP_TERM_WAIT_SEC", "30"))
HTTP_TIMEOUT_SEC = float(os.environ.get("IG_GUI_SUP_HTTP_TIMEOUT", "2.5"))
POST_READY_WAIT_SEC = float(os.environ.get("IG_GUI_SUP_POST_READY_WAIT_SEC", "45"))


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _data_root() -> Path:
    env = (os.environ.get("IG_DATA_ROOT") or os.environ.get("IG_AGENT_DATA_DIR") or "").strip()
    if env:
        return Path(env)
    try:
        from system.paths import data_dir

        return Path(data_dir())
    except Exception:
        return _repo_root() / "src" / "data" / "v31-production"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _heal_log_path(root: Path | None = None) -> Path:
    return (root or _data_root()) / "state" / "gui_supervisor_heal_log.jsonl"


def _heal_budget_path(root: Path | None = None) -> Path:
    return (root or _data_root()) / "state" / "gui_supervisor_heal_budget.json"


def _audit(event: str, payload: dict[str, Any], *, root: Path | None = None) -> None:
    path = _heal_log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": time.time(), "checked_at": _now_iso(), "event": event, **payload}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def load_heal_budget(*, root: Path | None = None) -> dict[str, Any]:
    path = _heal_budget_path(root)
    if not path.is_file():
        return {"window_start": time.time(), "heals": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {"window_start": time.time(), "heals": []}
    except Exception:
        return {"window_start": time.time(), "heals": []}


def save_heal_budget(budget: dict[str, Any], *, root: Path | None = None) -> None:
    path = _heal_budget_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(budget, indent=2, default=str) + "\n", encoding="utf-8")


def heals_in_last_hour(budget: dict[str, Any] | None = None, *, root: Path | None = None) -> list[dict[str, Any]]:
    budget = budget or load_heal_budget(root=root)
    cutoff = time.time() - 3600.0
    heals = [h for h in (budget.get("heals") or []) if isinstance(h, dict) and float(h.get("ts") or 0) >= cutoff]
    return heals


def budget_allows_heal(*, root: Path | None = None, cap: int | None = None) -> tuple[bool, dict[str, Any]]:
    cap_n = int(cap if cap is not None else DEFAULT_HEAL_CAP_PER_HOUR)
    budget = load_heal_budget(root=root)
    recent = heals_in_last_hour(budget, root=root)
    budget["heals"] = recent
    save_heal_budget(budget, root=root)
    ok = len(recent) < cap_n
    return ok, {"cap": cap_n, "used": len(recent), "recent": recent, "ok": ok}


def record_heal(action: str, *, detail: dict[str, Any] | None = None, root: Path | None = None) -> None:
    budget = load_heal_budget(root=root)
    recent = heals_in_last_hour(budget, root=root)
    recent.append({"ts": time.time(), "action": action, "detail": detail or {}})
    budget["heals"] = recent
    budget["window_start"] = recent[0]["ts"] if recent else time.time()
    save_heal_budget(budget, root=root)
    _audit("heal_recorded", {"action": action, "detail": detail or {}}, root=root)


def books_flat(payload: dict[str, Any]) -> bool:
    ports = payload.get("ports") if isinstance(payload.get("ports"), dict) else {}
    for key in ("cfd", "sb"):
        row = ports.get(key) if isinstance(ports.get(key), dict) else {}
        broker = row.get("broker_open")
        verdict = str(row.get("positions_verdict") or "")
        if broker is None:
            continue
        try:
            if int(broker) > 0:
                return False
        except (TypeError, ValueError):
            return False
        if verdict in ("CRITICAL", "DEGRADED") or (verdict and verdict not in ("FLAT", "HEALTHY", "")):
            if verdict not in ("FLAT", "HEALTHY"):
                # HEALTHY with opens would have broker_open>0; treat unknown open risk as not flat
                if verdict not in ("FLAT", "HEALTHY"):
                    pass
        if verdict == "CRITICAL":
            return False
    # Prefer explicit flat from area grades / open risk PASS with broker 0
    cfd_b = (ports.get("cfd") or {}).get("broker_open")
    sb_b = (ports.get("sb") or {}).get("broker_open")
    try:
        return int(cfd_b or 0) == 0 and int(sb_b or 0) == 0
    except (TypeError, ValueError):
        return False


def operator_bleed_lock_paths(*, root: Path | None = None) -> list[Path]:
    """Known durable lock locations under state_cfd / state_sb (and legacy state/)."""
    data_root = root or _data_root()
    dirs = (data_root / "state_cfd", data_root / "state_sb", data_root / "state")
    found: list[Path] = []
    for d in dirs:
        if not d.is_dir():
            continue
        try:
            found.extend(sorted(d.glob(OPERATOR_BLEED_LOCK_GLOB)))
        except OSError:
            continue
    return found


def load_operator_bleed_lock(*, root: Path | None = None) -> dict[str, Any] | None:
    """Return first active bleed lock with do_not_auto_resume, else None."""
    for path in operator_bleed_lock_paths(root=root):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        active = raw.get("active", True)
        if active is False:
            continue
        # Default True when key omitted — operator halt is sticky.
        if raw.get("do_not_auto_resume", True) is False:
            continue
        out = dict(raw)
        out["_path"] = str(path)
        return out
    return None


def operator_bleed_lock_blocks_resume(*, root: Path | None = None) -> tuple[bool, dict[str, Any] | None]:
    lock = load_operator_bleed_lock(root=root)
    return (lock is not None, lock)


def write_operator_bleed_locks(
    *,
    root: Path | None = None,
    reason: str = OPERATOR_BLEED_LOCK_REASON_DEFAULT,
    detail: dict[str, Any] | None = None,
) -> list[str]:
    """Write durable do_not_auto_resume locks under state_cfd + state_sb (idempotent)."""
    data_root = root or _data_root()
    day = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    engaged = _now_iso()
    epoch = time.time()
    written: list[str] = []
    body_base: dict[str, Any] = {
        "active": True,
        "mode": "OPERATOR_HALT_BLEED",
        "date": day,
        "reason": reason,
        "do_not_auto_resume": True,
        "engaged_at": engaged,
        "engaged_at_epoch": epoch,
        "scope": "BOTH :8080 CFD QUANT_SNIPER and :8081 SB MACRO_SENTINEL",
        "mechanism": [
            "POST /api/stop on :8080 and :8081",
            "durable operator_bleed_lock_*.json under state_cfd + state_sb",
            "Phase2 heal must NOT POST /api/start while lock present",
        ],
        "forbidden_while_locked": [
            "POST /api/start on either port (auto or probe)",
            "re-enable Instant/micro",
            "ranked loosen / one more probe",
        ],
        "unlock_requires": (
            "explicit operator action after review — remove BOTH lock files "
            "then curl POST /api/start per port"
        ),
        "source": "gui_desk_supervisor_ensure_bleed_halt",
    }
    if detail:
        body_base["supervisor_detail"] = detail
    for lane, sub in (("state_cfd", "state_cfd"), ("state_sb", "state_sb")):
        d = data_root / sub
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"operator_bleed_lock_{day}.json"
        # Respect existing lock content — only refresh sticky fields, never clear.
        existing: dict[str, Any] = {}
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    existing = raw
            except Exception:
                existing = {}
        body = dict(existing)
        body.update(body_base)
        body["lane"] = lane
        body["active"] = True
        body["do_not_auto_resume"] = True
        if existing.get("engaged_at"):
            body["engaged_at"] = existing.get("engaged_at")
            body["engaged_at_epoch"] = existing.get("engaged_at_epoch", epoch)
            body["reasserted_at"] = engaged
        path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
        written.append(str(path))
    return written


def ensure_operator_bleed_halt(
    *,
    ports: list[int] | None = None,
    root: Path | None = None,
    reason: str = OPERATOR_BLEED_LOCK_REASON_DEFAULT,
    detail: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Force-pause both desks + write bleed locks. Never POST /api/start."""
    ports = ports or [8080, 8081]
    root = root or _data_root()
    summary: dict[str, Any] = {
        "action": HEAL_ENSURE_BLEED_HALT,
        "dry_run": dry_run,
        "reason": reason,
        "ports": list(ports),
        "used_sigkill": False,
        "posted_start": False,
    }
    if dry_run:
        summary["ok"] = True
        summary["planned"] = [
            f"POST /api/stop on {ports}",
            "write operator_bleed_lock_*.json (do_not_auto_resume)",
            "NEVER POST /api/start",
        ]
        return summary
    stops: list[dict[str, Any]] = []
    for port in ports:
        stops.append({"port": int(port), **_post_api(int(port), "/api/stop")})
    summary["stops"] = stops
    summary["locks"] = write_operator_bleed_locks(root=root, reason=reason, detail=detail)
    summary["ok"] = True
    _audit("ensure_operator_bleed_halt", summary, root=root)
    return summary


def _http_json(url: str, *, method: str = "GET", timeout: float = HTTP_TIMEOUT_SEC) -> tuple[dict[str, Any] | None, str | None]:
    try:
        req = urllib.request.Request(
            url,
            method=method,
            headers={"Accept": "application/json", "User-Agent": "gui-desk-supervisor-heal/2"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw.strip() else {}
            return (data if isinstance(data, dict) else {"_non_object": data}), None
    except urllib.error.HTTPError as exc:
        return None, f"http_{exc.code}"
    except Exception as exc:
        return None, f"{type(exc).__name__}:{exc}"


def _post_api(port: int, path: str) -> dict[str, Any]:
    data, err = _http_json(f"http://127.0.0.1:{int(port)}{path}", method="POST")
    return {"ok": data is not None and (data.get("ok") is not False), "data": data, "error": err}


def _tcp_listen(port: int) -> bool:
    try:
        result = subprocess.run(
            ["lsof", f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return any(line.strip().isdigit() for line in (result.stdout or "").splitlines())
    except (OSError, subprocess.SubprocessError):
        return False


def _listener_pid(port: int) -> int | None:
    try:
        result = subprocess.run(
            ["lsof", f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        for line in (result.stdout or "").splitlines():
            if line.strip().isdigit():
                return int(line.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def soft_term_pid(pid: int, *, wait_sec: float = TERM_WAIT_SEC) -> dict[str, Any]:
    """SIGTERM only — never SIGKILL / kill -9."""
    out: dict[str, Any] = {"pid": pid, "signalled": False, "exited": False, "used_sigkill": False}
    if pid <= 0 or not _pid_alive(pid):
        out["exited"] = True
        return out
    try:
        os.kill(pid, signal.SIGTERM)
        out["signalled"] = True
    except OSError as exc:
        out["error"] = f"{type(exc).__name__}:{exc}"
        return out
    deadline = time.monotonic() + float(wait_sec)
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            out["exited"] = True
            return out
        time.sleep(0.4)
    out["exited"] = not _pid_alive(pid)
    out["still_alive"] = _pid_alive(pid)
    # Explicit: do NOT escalate to SIGKILL
    return out


def plan_heals(payload: dict[str, Any], *, silence_soft_pause: bool | None = None) -> list[dict[str, Any]]:
    """Derive allowlisted heal actions from an assess() payload (no side effects)."""
    plans: list[dict[str, Any]] = []
    findings = list(payload.get("findings") or [])
    ports = payload.get("ports") if isinstance(payload.get("ports"), dict) else {}
    a2 = payload.get("a2") if isinstance(payload.get("a2"), dict) else {}
    stuck = payload.get("stuck_plane") if isinstance(payload.get("stuck_plane"), dict) else {}
    flat = books_flat(payload)
    soft_pause = DEFAULT_SILENCE_SOFT_PAUSE if silence_soft_pause is None else bool(silence_soft_pause)

    def _has(title_substr: str, *, severity: str | None = None) -> bool:
        for f in findings:
            title = str(f.get("title") or "")
            if title_substr.lower() in title.lower():
                if severity is None or str(f.get("severity")) == severity:
                    return True
        return False

    # UI down
    ui = ports.get("ui") if isinstance(ports.get("ui"), dict) else {}
    if not (ui.get("http") or ui.get("tcp")) or _has("Quantum Terminal unreachable", severity="fail"):
        plans.append(
            {
                "action": HEAL_UI_DOWN,
                "requires_flat": False,
                "reason": "Quantum Terminal :3000 unreachable",
                "ports": [int((ui.get("port") or 3000))],
            }
        )

    # Hung port: LISTEN but API unreachable / timeout
    for key in ("cfd", "sb"):
        row = ports.get(key) if isinstance(ports.get(key), dict) else {}
        port = int(row.get("port") or (8080 if key == "cfd" else 8081))
        hung = bool(row.get("hung_api")) or bool(row.get("listen") and not row.get("reachable"))
        if not hung:
            continue
        plans.append(
            {
                "action": HEAL_PORT_HUNG,
                "requires_flat": True,
                "reason": f":{port} LISTEN but API hung/timeout",
                "ports": [port],
                "reapply_a2": bool(a2.get("marker_active")) and key == "cfd",
                "restart_sb_if_armed": key == "cfd" and a2.get("sb_trading_paused") is not True,
            }
        )

    # Loops not arming after READY
    for key, loops_key in (("cfd", "cfd_loops"), ("sb", "sb_loops")):
        loops = stuck.get(loops_key) if isinstance(stuck.get(loops_key), dict) else {}
        row = ports.get(key) if isinstance(ports.get(key), dict) else {}
        paused = row.get("trading_paused")
        if paused is True:
            continue
        accepting = loops.get("accepting_ticks")
        readyish = loops.get("boot_ready") is True or loops.get("trade_ready") is True
        if readyish and accepting is False:
            port = int(row.get("port") or (8080 if key == "cfd" else 8081))
            plans.append(
                {
                    "action": HEAL_LOOPS_NOT_ARMING,
                    "requires_flat": True,  # recycle requires flat; unpause alone is always ok
                    "reason": f":{port} READY/trade_ready but accepting_ticks=false",
                    "ports": [port],
                    "try_unpause_first": True,
                }
            )
        if _has("TradingLoops not accepting ticks", severity="fail") and key == "sb":
            if not any(p.get("action") == HEAL_LOOPS_NOT_ARMING and int(row.get("port") or 8081) in (p.get("ports") or []) for p in plans):
                plans.append(
                    {
                        "action": HEAL_LOOPS_NOT_ARMING,
                        "requires_flat": True,
                        "reason": "finding: TradingLoops not accepting ticks (STUCK)",
                        "ports": [int(row.get("port") or 8081)],
                        "try_unpause_first": True,
                    }
                )

    # A2 marker active but CFD not paused
    if a2.get("marker_active") and a2.get("cfd_trading_paused") is not True:
        plans.append(
            {
                "action": HEAL_REAPPLY_A2,
                "requires_flat": False,
                "reason": "A2 marker active but CFD not paused",
                "ports": [int((ports.get("cfd") or {}).get("port") or 8080)],
            }
        )

    # Armed silence → optional soft pause SB (halt bleed), never loosen gates
    if soft_pause and (
        _has("ARMED but silent", severity="fail")
        or _has("zero-attempt silence", severity="fail")
        or _has("armed silence", severity="fail")
    ):
        plans.append(
            {
                "action": HEAL_SILENCE_SOFT_PAUSE,
                "requires_flat": False,
                "reason": "ARMED silence timer exceeded — soft-pause SB entries (halt bleed)",
                "ports": [int((ports.get("sb") or {}).get("port") or 8081)],
            }
        )

    # Journal/session bleed / SESSION_KILL → ensure both paused + durable locks (never start)
    if (
        payload.get("ensure_bleed_halt")
        or _has("BLEED:", severity="fail")
        or _has("SESSION_KILL:", severity="fail")
    ):
        plans.append(
            {
                "action": HEAL_ENSURE_BLEED_HALT,
                "requires_flat": False,
                "reason": "BLEED/SESSION_KILL — force pause both + write do_not_auto_resume locks",
                "ports": [
                    int((ports.get("cfd") or {}).get("port") or 8080),
                    int((ports.get("sb") or {}).get("port") or 8081),
                ],
            }
        )

    # De-dupe by action+ports
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for p in plans:
        action = str(p.get("action") or "")
        if action not in ALLOWED_HEALS:
            continue
        key = f"{action}:{sorted(p.get('ports') or [])}"
        if key in seen:
            continue
        seen.add(key)
        p["allowed"] = True
        p["books_flat"] = flat
        p["blocked_by_open_book"] = bool(p.get("requires_flat") and not flat)
        out.append(p)
    return out


def heal_ui_only(*, dry_run: bool = False, root: Path | None = None) -> dict[str, Any]:
    repo = _repo_root()
    script = repo / "scripts" / "start_ui_background.sh"
    summary: dict[str, Any] = {
        "action": HEAL_UI_DOWN,
        "dry_run": dry_run,
        "script": str(script),
    }
    if dry_run:
        summary["ok"] = True
        summary["planned"] = "kickstart com.igagent.v30.ui or start_ui_background.sh"
        return summary
    # Prefer LaunchAgent kickstart (UI only)
    try:
        uid = os.getuid()
        domain = f"gui/{uid}"
        subprocess.run(
            ["launchctl", "kickstart", f"{domain}/com.igagent.v30.ui"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        summary["kickstart"] = True
    except Exception as exc:
        summary["kickstart_error"] = f"{type(exc).__name__}:{exc}"
    if script.is_file():
        try:
            proc = subprocess.Popen(
                ["/bin/bash", str(script)],
                cwd=str(repo),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            summary["spawned_pid"] = proc.pid
            summary["ok"] = True
        except OSError as exc:
            summary["ok"] = False
            summary["error"] = str(exc)
    else:
        summary["ok"] = bool(summary.get("kickstart"))
    _audit("heal_ui", summary, root=root)
    return summary


def reapply_a2_cfd_pause(*, port: int = 8080, dry_run: bool = False, root: Path | None = None) -> dict[str, Any]:
    summary: dict[str, Any] = {"action": HEAL_REAPPLY_A2, "port": port, "dry_run": dry_run}
    if dry_run:
        summary["ok"] = True
        summary["planned"] = f"POST http://127.0.0.1:{port}/api/stop"
        return summary
    result = _post_api(port, "/api/stop")
    summary.update(result)
    # Refresh marker stamp (do not clear active)
    data_root = root or _data_root()
    marker = data_root / "state_cfd" / "a2_entries_paused.json"
    try:
        if marker.is_file():
            raw = json.loads(marker.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                raw["active"] = True
                raw["reengaged_at"] = _now_iso()
                raw["reengage_reason"] = "gui_desk_supervisor_heal_reapply_a2"
                marker.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
                summary["marker_updated"] = True
    except Exception as exc:
        summary["marker_error"] = f"{type(exc).__name__}:{exc}"
    _audit("heal_reapply_a2", summary, root=root)
    return summary


def soft_pause_sb(*, port: int = 8081, dry_run: bool = False, root: Path | None = None) -> dict[str, Any]:
    summary: dict[str, Any] = {"action": HEAL_SILENCE_SOFT_PAUSE, "port": port, "dry_run": dry_run}
    if dry_run:
        summary["ok"] = True
        summary["planned"] = f"POST http://127.0.0.1:{port}/api/stop (halt bleed; no gate loosen)"
        return summary
    result = _post_api(port, "/api/stop")
    summary.update(result)
    _audit("heal_silence_soft_pause", summary, root=root)
    return summary


def unpause_port(*, port: int, dry_run: bool = False, root: Path | None = None) -> dict[str, Any]:
    summary: dict[str, Any] = {"action": "unpause", "port": port, "dry_run": dry_run}
    blocked, lock = operator_bleed_lock_blocks_resume(root=root)
    if blocked:
        summary["ok"] = False
        summary["blocked_by_operator_bleed_lock"] = True
        summary["lock"] = {
            "path": (lock or {}).get("_path"),
            "reason": (lock or {}).get("reason"),
            "do_not_auto_resume": True,
        }
        summary["error"] = "operator_bleed_lock_blocks_api_start"
        if dry_run:
            summary["planned"] = "SKIP POST /api/start — operator bleed lock present"
        _audit("heal_unpause_blocked_bleed_lock", summary, root=root)
        return summary
    if dry_run:
        summary["ok"] = True
        summary["planned"] = f"POST http://127.0.0.1:{port}/api/start"
        return summary
    result = _post_api(port, "/api/start")
    summary.update(result)
    _audit("heal_unpause", summary, root=root)
    return summary


def soft_recycle_port(
    port: int,
    *,
    dry_run: bool = False,
    root: Path | None = None,
    reapply_a2: bool = False,
    start_sb_if_armed: bool = False,
    sb_port: int = 8081,
    spawn_fn: Callable[[int], bool] | None = None,
) -> dict[str, Any]:
    """Soft anti-zombie for one port: mark_manual_stop → SIGTERM → wait → spawn.

    Never SIGKILL. Protects sibling dual-port listener.
    """
    summary: dict[str, Any] = {
        "action": HEAL_PORT_HUNG,
        "port": int(port),
        "dry_run": dry_run,
        "used_sigkill": False,
        "reapply_a2": reapply_a2,
    }
    if dry_run:
        summary["ok"] = True
        summary["planned"] = [
            "mark_manual_stop(source='gui_desk_supervisor_heal')",
            f"SIGTERM listener on :{port} (no SIGKILL)",
            "clear port locks if free",
            f"spawn v32 engine :{port}",
            "reapply A2 /api/stop on CFD if requested",
            "POST /api/start on SB if was armed",
            "clear_manual_stop",
        ]
        return summary

    try:
        from system.shutdown_cleanup import mark_manual_stop

        mark_manual_stop(source="gui_desk_supervisor_heal")
        summary["manual_stop"] = True
    except Exception as exc:
        summary["manual_stop_error"] = f"{type(exc).__name__}:{exc}"

    pid = _listener_pid(int(port))
    summary["listener_pid"] = pid
    if pid:
        # Protect sibling: never TERM the other dual port
        sibling = 8081 if int(port) == 8080 else 8080
        sibling_pid = _listener_pid(sibling)
        if sibling_pid and sibling_pid == pid:
            summary["ok"] = False
            summary["error"] = "refusing to TERM shared pid across dual ports"
            return summary
        term = soft_term_pid(pid, wait_sec=TERM_WAIT_SEC)
        summary["term"] = term
        if term.get("still_alive"):
            summary["ok"] = False
            summary["error"] = "SIGTERM did not exit within wait; escalate to operator (no kill -9)"
            _audit("heal_recycle_stuck", summary, root=root)
            return summary

    # Clear locks via desk_support helpers when available
    try:
        from runtime.desk_support_wrapper import DeskSupportWrapper

        wrapper = DeskSupportWrapper()
        summary["locks_removed"] = wrapper._clear_locks()
    except Exception as exc:
        summary["locks_error"] = f"{type(exc).__name__}:{exc}"

    started = False
    if spawn_fn is not None:
        started = bool(spawn_fn(int(port)))
    else:
        try:
            from runtime.desk_support_wrapper import DeskSupportWrapper

            wrapper = DeskSupportWrapper()
            started = bool(wrapper._launch_v32_engine(int(port)))
        except Exception as exc:
            summary["spawn_error"] = f"{type(exc).__name__}:{exc}"
    summary["started"] = started

    # Wait briefly for health
    deadline = time.monotonic() + POST_READY_WAIT_SEC
    healthy = False
    while time.monotonic() < deadline:
        data, err = _http_json(f"http://127.0.0.1:{int(port)}/api/health")
        if data is not None:
            healthy = True
            summary["health_pid"] = data.get("agent_pid")
            break
        summary["health_wait_err"] = err
        time.sleep(1.0)
    summary["healthy"] = healthy

    if reapply_a2 and int(port) == 8080:
        summary["a2"] = reapply_a2_cfd_pause(port=8080, dry_run=False, root=root)
    elif reapply_a2 and healthy:
        # If we recycled SB but A2 was for CFD — still ensure CFD pause
        summary["a2"] = reapply_a2_cfd_pause(port=8080, dry_run=False, root=root)

    if start_sb_if_armed:
        # Ensure SB entries path is running when CFD was the recycle target —
        # but NEVER while operator bleed lock is active.
        blocked, lock = operator_bleed_lock_blocks_resume(root=root)
        if blocked:
            summary["sb_start"] = {
                "ok": False,
                "skipped": True,
                "blocked_by_operator_bleed_lock": True,
                "lock_path": (lock or {}).get("_path"),
                "reason": (lock or {}).get("reason"),
            }
        else:
            summary["sb_start"] = unpause_port(port=sb_port, dry_run=False, root=root)

    try:
        from system.shutdown_cleanup import clear_manual_stop

        clear_manual_stop()
        summary["manual_stop_cleared"] = True
    except Exception:
        pass

    summary["ok"] = bool(started or healthy)
    _audit("heal_soft_recycle", summary, root=root)
    return summary


def execute_heal_plan(
    plans: list[dict[str, Any]],
    *,
    dry_run: bool = True,
    root: Path | None = None,
    cap: int | None = None,
) -> dict[str, Any]:
    """Execute allowlisted plans. Caps heals/hour; skips open-book restarts."""
    root = root or _data_root()
    allowed_ok, budget_info = budget_allows_heal(root=root, cap=cap)
    results: list[dict[str, Any]] = []
    escalations: list[str] = []

    if not plans:
        return {
            "ok": True,
            "dry_run": dry_run,
            "executed": [],
            "skipped": [],
            "escalations": [],
            "budget": budget_info,
            "message": "no heals planned",
        }

    if not allowed_ok and not dry_run:
        msg = f"heal cap reached ({budget_info.get('used')}/{budget_info.get('cap')} per hour) — hard FAIL escalate"
        _audit("heal_cap_exceeded", {"budget": budget_info}, root=root)
        return {
            "ok": False,
            "dry_run": dry_run,
            "executed": [],
            "skipped": plans,
            "escalations": [msg],
            "budget": budget_info,
            "hard_fail": True,
            "message": msg,
        }

    skipped: list[dict[str, Any]] = []
    for plan in plans:
        action = str(plan.get("action") or "")
        if action not in ALLOWED_HEALS:
            skipped.append({**plan, "skip_reason": "not_allowlisted"})
            escalations.append(f"blocked non-allowlisted action: {action}")
            continue
        # Operator bleed lock: silence/loops heals may still soft-PAUSE, never unpause/start.
        # ensure_bleed_halt is pause+lock only — always allowed under lock.
        bleed_blocked, bleed_lock = operator_bleed_lock_blocks_resume(root=root)
        if bleed_blocked and action in (HEAL_LOOPS_NOT_ARMING,):
            skipped.append(
                {
                    **plan,
                    "skip_reason": "operator_bleed_lock",
                    "lock_path": (bleed_lock or {}).get("_path"),
                }
            )
            escalations.append(
                f"{action} blocked — operator bleed lock "
                f"{(bleed_lock or {}).get('_path')} (do_not_auto_resume)"
            )
            continue
        if bleed_blocked and action == HEAL_PORT_HUNG and plan.get("restart_sb_if_armed"):
            # Recycle may still TERM hung API, but must not restart via /api/start.
            plan = {**plan, "restart_sb_if_armed": False}

        if plan.get("blocked_by_open_book") and action in (HEAL_PORT_HUNG, HEAL_LOOPS_NOT_ARMING):
            # Unpause-only path for loops may still run — unless bleed lock (handled above).
            if action == HEAL_LOOPS_NOT_ARMING and plan.get("try_unpause_first"):
                port = int((plan.get("ports") or [8081])[0])
                res = unpause_port(port=port, dry_run=dry_run, root=root)
                res["note"] = "open book — unpause only, recycle deferred"
                results.append(res)
                if not dry_run:
                    record_heal(action, detail={"mode": "unpause_only", "port": port}, root=root)
                continue
            skipped.append({**plan, "skip_reason": "books_not_flat"})
            escalations.append(
                f"{action} deferred — books not flat (live cutover needed for recycle)"
            )
            continue

        if action == HEAL_UI_DOWN:
            res = heal_ui_only(dry_run=dry_run, root=root)
        elif action == HEAL_ENSURE_BLEED_HALT:
            res = ensure_operator_bleed_halt(
                ports=[int(p) for p in (plan.get("ports") or [8080, 8081])],
                dry_run=dry_run,
                root=root,
                reason=str(plan.get("reason") or OPERATOR_BLEED_LOCK_REASON_DEFAULT),
                detail={"plan_reason": plan.get("reason")},
            )
        elif action == HEAL_REAPPLY_A2:
            port = int((plan.get("ports") or [8080])[0])
            res = reapply_a2_cfd_pause(port=port, dry_run=dry_run, root=root)
        elif action == HEAL_SILENCE_SOFT_PAUSE:
            port = int((plan.get("ports") or [8081])[0])
            res = soft_pause_sb(port=port, dry_run=dry_run, root=root)
        elif action == HEAL_LOOPS_NOT_ARMING:
            port = int((plan.get("ports") or [8081])[0])
            res = unpause_port(port=port, dry_run=dry_run, root=root)
            if (
                not dry_run
                and res.get("ok")
                and not plan.get("blocked_by_open_book")
                and plan.get("books_flat")
            ):
                time.sleep(2.0)
                health, _ = _http_json(f"http://127.0.0.1:{port}/api/health")
                loops = {}
                if isinstance(health, dict):
                    bm = health.get("boot_metrics") if isinstance(health.get("boot_metrics"), dict) else {}
                    ss = bm.get("system_state") if isinstance(bm.get("system_state"), dict) else {}
                    loops = ss.get("loops") if isinstance(ss.get("loops"), dict) else {}
                    if not loops and isinstance(health.get("loops"), dict):
                        loops = health.get("loops") or {}
                if loops.get("accepting_ticks") is not True:
                    recycle = soft_recycle_port(
                        port,
                        dry_run=False,
                        root=root,
                        reapply_a2=port == 8080,
                    )
                    res = {"unpause": res, "recycle": recycle, "ok": recycle.get("ok")}
        elif action == HEAL_PORT_HUNG:
            port = int((plan.get("ports") or [8080])[0])
            res = soft_recycle_port(
                port,
                dry_run=dry_run,
                root=root,
                reapply_a2=bool(plan.get("reapply_a2")),
                start_sb_if_armed=bool(plan.get("restart_sb_if_armed")),
            )
        else:
            skipped.append({**plan, "skip_reason": "unhandled"})
            continue

        results.append(res if isinstance(res, dict) else {"result": res})
        if not dry_run:
            record_heal(action, detail={"ports": plan.get("ports"), "ok": (res or {}).get("ok")}, root=root)
            # Re-check budget after each
            allowed_ok, budget_info = budget_allows_heal(root=root, cap=cap)
            if not allowed_ok:
                escalations.append("heal cap reached mid-run — stopping further heals")
                break

    ok = not any(r.get("ok") is False for r in results) and not any(
        e.startswith("heal cap") for e in escalations
    )
    out = {
        "ok": ok if results or not escalations else True,
        "dry_run": dry_run,
        "executed": results,
        "skipped": skipped,
        "escalations": escalations,
        "budget": budget_info,
        "hard_fail": any("cap reached" in e for e in escalations),
    }
    _audit("heal_plan_done", {"dry_run": dry_run, "n": len(results), "ok": out["ok"]}, root=root)
    return out


def assert_no_forbidden_in_plan(plans: list[dict[str, Any]]) -> None:
    for p in plans:
        action = str(p.get("action") or "")
        if action not in ALLOWED_HEALS:
            raise AssertionError(f"forbidden/non-allowlisted heal: {action}")
        if action in FORBIDDEN_ACTIONS:
            raise AssertionError(f"plan action is forbidden: {action}")
        # Intent field may document policy; never schedule SIGKILL/kill -9 as the action
        if "kill" in action and ("9" in action or "sigkill" in action):
            raise AssertionError("plan mentions kill -9 / SIGKILL as action")
