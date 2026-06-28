"""
IG Agent v31 macOS launcher core — testable orchestration helpers.

Application-level only; does not modify trading or execution logic.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

DEFAULT_PORT = 8080
DEFAULT_MODE = "DEMO"
HEALTH_PATH = "/api/health"
GUI_STATUS_PATH = "/api/gui_status"

REQUIRED_GUI_FIELDS: tuple[str, ...] = (
    "strategy_selector_advice",
    "strategy_controller_decisions",
    "strategy_transition_advice",
    "strategy_enforcement_decisions",
    "hard_enforcement_decisions",
    "adaptive_thresholds",
    "strategy_performance_memory",
    "strategy_weighting_advice",
    "regime_detection",
    "regime_strategy_alignment",
    "regime_aware_strategy_selector",
    "regime_risk_envelope",
    "regime_sizing_advice",
    "daily_pnl_targeting",
    "unified_execution_route",
    "strategy_governance",
    "session_review",
    "loosening_advice",
    "self_reflection",
    "trade_pipeline_health",
    "pipeline_governance",
)

LOCK_FILES: tuple[str, ...] = (
    "src/data/.ig_agent_v29.lock",
)


def project_root_from(path: Path) -> Path:
    """Resolve repo root from macos/launcher/ or app bundle path."""
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / "scripts" / "start.sh").is_file() and (candidate / "src" / "main.py").is_file():
            return candidate
    raise FileNotFoundError(f"IG Agent project root not found from {path}")


def port_is_bound(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def wait_port_free(port: int, *, timeout_sec: float = 30.0, poll_sec: float = 1.0) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if not port_is_bound(port):
            return True
        time.sleep(poll_sec)
    return not port_is_bound(port)


def pids_on_port(port: int) -> list[int]:
    try:
        out = subprocess.run(
            ["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            check=False,
        )
        pids: list[int] = []
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
        return pids
    except Exception:
        return []


def terminate_pids(pids: list[int], *, signal: str = "TERM") -> None:
    sig = "-TERM" if signal.upper() == "TERM" else "-KILL"
    for pid in pids:
        try:
            subprocess.run(["kill", sig, str(pid)], check=False, capture_output=True)
        except Exception:
            pass


def mark_manual_stop_hold(*, root: Path, source: str = "launcher_restart") -> None:
    py = _python_bin(root)
    env = {**os.environ, "PYTHONPATH": str(root / "src")}
    subprocess.run(
        [
            str(py),
            "-c",
            f"from system.shutdown_cleanup import mark_manual_stop; mark_manual_stop(source={source!r})",
        ],
        cwd=str(root),
        env=env,
        check=False,
        capture_output=True,
    )


def _python_bin(root: Path) -> Path:
    venv_py = root / ".venv" / "bin" / "python3"
    if venv_py.is_file():
        return venv_py
    return Path(os.environ.get("LAUNCHER_PYTHON", "python3"))


def run_stop_script(root: Path, *, mode: str = DEFAULT_MODE) -> int:
    script = root / "scripts" / "stop.sh"
    proc = subprocess.run(
        [str(script), "--mode", mode],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    return int(proc.returncode)


def purge_bytecode(root: Path) -> None:
    import shutil

    for pycache in root.glob("src/**/__pycache__"):
        if pycache.is_dir():
            try:
                shutil.rmtree(pycache)
            except Exception:
                pass
    for pyc in root.glob("src/**/*.pyc"):
        try:
            pyc.unlink()
        except Exception:
            pass


def remove_stale_locks(root: Path) -> list[str]:
    removed: list[str] = []
    for rel in LOCK_FILES:
        path = root / rel
        if path.is_file():
            try:
                path.unlink()
                removed.append(str(path))
            except Exception:
                pass
    return removed


def reset_demo_session_state(root: Path) -> dict[str, Any]:
    """
    DEMO-only launcher reset — runtime caches and P&L baseline, not SQLite history.
    """
    summary: dict[str, Any] = {"applied": False, "steps": []}
    if os.environ.get("LAUNCHER_SKIP_DEMO_RESET", "").strip() in ("1", "true", "yes"):
        summary["reason"] = "skipped_by_env"
        summary["applied"] = True
        return summary

    env = {**os.environ, "PYTHONPATH": str(root / "src"), "APP_MODE": "DEMO"}
    py = _python_bin(root)
    code = """
from runtime.strategy_controller import reset_strategy_controller_for_tests
from runtime.strategy_enforcement import reset_strategy_enforcement_for_tests
from system.shutdown_cleanup import reset_shutdown_verify_state

reset_strategy_controller_for_tests()
reset_strategy_enforcement_for_tests()
reset_shutdown_verify_state()
result = {"caches_cleared": True}
try:
    from system.config_loader import load_active_config
    from data.learning_store import LearningStore
    from system.v291_upgrade import refresh_today_daily_loss_baseline

    cfg = load_active_config(validate=False)
    db = str(getattr(cfg, "learning_db", "") or "")
    if db:
        store = LearningStore(db)
        store.connect()
        baseline = refresh_today_daily_loss_baseline(
            store, cfg=cfg, version="v31-launcher", reason="demo_launcher_reset"
        )
        result["daily_pnl_baseline_reset"] = baseline
except Exception as exc:
    result["daily_pnl_baseline_reset"] = {"skipped": str(exc)}
print(__import__("json").dumps(result))
"""
    proc = subprocess.run(
        [str(py), "-c", code],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    summary["applied"] = proc.returncode == 0
    if proc.stdout.strip():
        try:
            summary["detail"] = json.loads(proc.stdout.strip().splitlines()[-1])
        except json.JSONDecodeError:
            summary["detail"] = {"stdout": proc.stdout[-500:]}
    if proc.stderr.strip():
        summary["stderr"] = proc.stderr[-500:]
    summary["steps"].append("strategy_caches_and_demo_baseline")
    return summary


def run_start_script(root: Path, *, mode: str = DEFAULT_MODE) -> int:
    script = root / "scripts" / "start.sh"
    proc = subprocess.run(
        [str(script), "--mode", mode],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    return int(proc.returncode)


def fetch_json(url: str, *, timeout_sec: float = 5.0) -> dict[str, Any] | None:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def verify_health(
    *,
    port: int = DEFAULT_PORT,
    timeout_sec: float = 120.0,
    poll_sec: float = 3.0,
    fetch: Callable[[str], dict[str, Any] | None] | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    fetch_fn = fetch or (lambda url: fetch_json(url))
    deadline = time.time() + timeout_sec
    url = f"http://127.0.0.1:{port}{HEALTH_PATH}"
    last: dict[str, Any] | None = None
    while time.time() < deadline:
        last = fetch_fn(url)
        if last is not None:
            phase = (last.get("system_state") or {}).get("phase") or last.get("phase")
            if str(phase).upper() == "G5" or last.get("status") == "ok":
                return True, last
        time.sleep(poll_sec)
    return False, last


def verify_gui_status(
    *,
    port: int = DEFAULT_PORT,
    timeout_sec: float = 60.0,
    poll_sec: float = 3.0,
    required_fields: tuple[str, ...] = REQUIRED_GUI_FIELDS,
    fetch: Callable[[str], dict[str, Any] | None] | None = None,
) -> tuple[bool, list[str], dict[str, Any] | None]:
    fetch_fn = fetch or (lambda url: fetch_json(url))
    deadline = time.time() + timeout_sec
    url = f"http://127.0.0.1:{port}{GUI_STATUS_PATH}"
    last: dict[str, Any] | None = None
    while time.time() < deadline:
        last = fetch_fn(url)
        if isinstance(last, dict):
            missing = [f for f in required_fields if f not in last]
            if not missing:
                return True, [], last
        time.sleep(poll_sec)
    missing = [f for f in required_fields if not isinstance(last, dict) or f not in last]
    return False, missing, last


def dashboard_url(port: int = DEFAULT_PORT) -> str:
    return f"http://127.0.0.1:{port}/"


def should_start_npm_dev(root: Path) -> bool:
    dist_index = root / "dashboard" / "dist" / "index.html"
    return not dist_index.is_file()


def open_dashboard(
    root: Path,
    *,
    port: int = DEFAULT_PORT,
    open_fn: Callable[[str], None] | None = None,
    popen_fn: Callable[..., Any] | None = None,
) -> None:
    """Open browser to agent dashboard; optionally start npm dev if dist missing."""
    url = dashboard_url(port)
    if should_start_npm_dev(root) and os.environ.get("LAUNCHER_SKIP_NPM_DEV", "").strip() not in (
        "1",
        "true",
        "yes",
    ):
        npm = root / "dashboard"
        if (npm / "package.json").is_file():
            spawn = popen_fn or subprocess.Popen
            spawn(
                ["npm", "run", "dev"],
                cwd=str(npm),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            time.sleep(2.0)
    opener = open_fn or (lambda u: subprocess.run(["open", u], check=False))
    opener(url)


def stop_phase(
    root: Path,
    *,
    port: int = DEFAULT_PORT,
    mode: str = DEFAULT_MODE,
    term_wait_sec: float = 30.0,
) -> tuple[bool, str]:
    mark_manual_stop_hold(root=root)
    run_stop_script(root, mode=mode)
    if wait_port_free(port, timeout_sec=term_wait_sec):
        return True, "port free after stop.sh"
    pids = pids_on_port(port)
    if pids:
        terminate_pids(pids, signal="TERM")
        if not wait_port_free(port, timeout_sec=term_wait_sec):
            terminate_pids(pids_on_port(port), signal="KILL")
            wait_port_free(port, timeout_sec=10.0)
    remove_stale_locks(root)
    if port_is_bound(port):
        return False, f"port {port} still bound after stop escalation"
    return True, "stopped and port free"


def _cli() -> int:
    import argparse
    import sys

    launcher_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="IG Agent v31 launcher phase runner")
    parser.add_argument(
        "--phase",
        required=True,
        choices=("stop", "clean", "reset", "verify", "gui"),
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    root = project_root_from(launcher_dir)

    if args.phase == "stop":
        ok, detail = stop_phase(root, port=args.port)
        print(detail)
        return 0 if ok else 1

    if args.phase == "clean":
        purge_bytecode(root)
        removed = remove_stale_locks(root)
        print(f"removed locks: {removed}")
        return 0

    if args.phase == "reset":
        summary = reset_demo_session_state(root)
        print(summary)
        return 0 if summary.get("applied") else 1

    if args.phase == "verify":
        health_ok, health = verify_health(port=args.port)
        if not health_ok:
            print(f"health failed: {health}")
            return 1
        gui_ok, missing, _gui = verify_gui_status(port=args.port)
        if not gui_ok:
            print(f"gui_status missing fields: {missing}")
            return 1
        print("health and gui_status ok")
        return 0

    if args.phase == "gui":
        open_dashboard(root, port=args.port)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
