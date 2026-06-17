"""
Autonomous sandbox deployment supervisor — patch_crash_* branch gate + hot reload.

When an isolated ``patch_crash_*`` branch is detected:
  1. Run the full deploy regression gate (``test_deployed_fixes.py``) with ``.env`` auth.
  2. On 100% pass: merge to ``main``, clear ports 8080/8787, restart agent, broadcast UI reload.
  3. On any failure: freeze deployment and leave the branch parked.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.env_loader import load_dotenv
from system.paths import find_python_executable, logs_dir, project_root
from system.supervisor_history import record_supervisor_event

PATCH_BRANCH_PREFIX = "patch_crash_"
DEFAULT_MAIN_BRANCH = "main"
GATE_SUITE_FILES: tuple[str, ...] = ("tests/test_deployed_fixes.py",)
_POLL_INTERVAL_SEC = 60.0
_HEALTH_TIMEOUT_SEC = 180.0

_supervisor_thread: threading.Thread | None = None
_supervisor_stop = threading.Event()
_frozen = False
_last_result: dict[str, Any] = {}


@dataclass
class GateSuiteResult:
    ok: bool
    passed: int = 0
    failed: int = 0
    total: int = 0
    output_tail: str = ""
    error: str = ""


@dataclass
class SelfHealingSupervisor:
    """Deployment authorization gate for AI-authored crash patches."""

    repo_root: Path = field(default_factory=project_root)
    main_branch: str = DEFAULT_MAIN_BRANCH
    gate_files: tuple[str, ...] = GATE_SUITE_FILES

    def discover_patch_branches(self) -> list[str]:
        try:
            result = subprocess.run(
                [
                    "git",
                    "for-each-ref",
                    "--format=%(refname:short)",
                    f"refs/heads/{PATCH_BRANCH_PREFIX}*",
                ],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log_engine(f"self_healing: branch discovery failed: {exc}")
            return []
        branches = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return sorted(branches)

    def run_gate_suite(self) -> GateSuiteResult:
        """Execute the full deploy regression gate with sandbox ``.env`` context."""
        load_dotenv()
        py = find_python_executable()
        args = [py, "-m", "pytest", *self.gate_files, "-q", "--tb=no"]
        env = {
            **os.environ,
            "PYTHONPATH": str(self.repo_root / "src"),
            "IG_AGENT_PYTEST": "1",
        }
        try:
            proc = subprocess.run(
                args,
                cwd=str(self.repo_root),
                env=env,
                capture_output=True,
                text=True,
                timeout=900,
            )
        except subprocess.TimeoutExpired:
            return GateSuiteResult(ok=False, error="gate suite timed out")
        except OSError as exc:
            return GateSuiteResult(ok=False, error=str(exc))

        combined = (proc.stdout or "") + (proc.stderr or "")
        passed = failed = total = 0
        for line in combined.splitlines():
            stripped = line.strip()
            if stripped.endswith(" passed") and " in " in stripped:
                # e.g. "67 passed in 12.34s"
                try:
                    passed = int(stripped.split()[0])
                    total = passed
                except ValueError:
                    pass
            if stripped.endswith(" failed") and stripped[0].isdigit():
                try:
                    failed = int(stripped.split()[0])
                except ValueError:
                    pass

        ok = proc.returncode == 0
        if ok and total == 0:
            # pytest -q with no summary line — treat exit code as authority
            total = passed or 1

        return GateSuiteResult(
            ok=ok,
            passed=passed,
            failed=failed,
            total=total,
            output_tail=combined.strip()[-400:],
            error="" if ok else f"exit={proc.returncode}",
        )

    def merge_branch_to_main(self, branch: str) -> bool:
        try:
            subprocess.run(
                ["git", "checkout", self.main_branch],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "merge",
                    "--no-ff",
                    branch,
                    "-m",
                    f"self_healing: autonomous merge {branch}",
                ],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=120,
                check=True,
            )
            log_engine(f"self_healing: merged {branch} -> {self.main_branch}")
            return True
        except subprocess.CalledProcessError as exc:
            tail = (exc.stderr or exc.stdout or str(exc)).strip()[-300:]
            log_engine(f"self_healing: merge aborted for {branch} — {tail}")
            return False

    def cleanup_service_ports(self) -> dict[str, Any]:
        """User-level cleanup for agent (:8080) and cockpit (:8787)."""
        from cockpit.port_cleanup import clear_port_8080

        killed_8080 = clear_port_8080(port=8080)
        killed_8787: list[int] = []
        try:
            out = subprocess.check_output(
                ["lsof", "-nP", "-iTCP:8787", "-sTCP:LISTEN", "-t"],
                stderr=subprocess.DEVNULL,
                text=True,
            )
            for line in out.strip().splitlines():
                if line.strip().isdigit():
                    pid = int(line.strip())
                    if pid != os.getpid():
                        try:
                            os.kill(pid, 15)
                            killed_8787.append(pid)
                        except OSError:
                            pass
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

        record_supervisor_event(
            "port_flush",
            detail=f"8080 killed={killed_8080} 8787 killed={killed_8787}",
            payload={"killed_8080": killed_8080, "killed_8787": killed_8787},
        )

        lock = self.repo_root / "src" / "data" / ".ig_agent_v29.lock"
        lock.unlink(missing_ok=True)
        return {"killed_8080": killed_8080, "killed_8787": killed_8787}

    def hot_reload_agent(self) -> bool:
        """Restart agent headlessly after port cleanup."""
        py = find_python_executable()
        launcher = self.repo_root / "scripts" / "start_agent_launchd.py"
        env = {
            **os.environ,
            "PYTHONPATH": str(self.repo_root / "src"),
            "IG_AGENT_ROOT": str(self.repo_root),
            "IG_AGENT_FROM_LAUNCHER": "1",
            "IG_AGENT_SKIP_DEPLOY_CHECK": "1",
            "IG_AGENT_SKIP_ORPHAN_KILL": "1",
            "IG_AGENT_OPEN_COCKPIT": "1",
        }
        log_path = logs_dir() / "self_healing_restart.log"
        try:
            with open(log_path, "a", encoding="utf-8") as logf:
                subprocess.Popen(
                    [py, str(launcher)],
                    cwd=str(self.repo_root),
                    env=env,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except OSError as exc:
            log_engine(f"self_healing: hot reload spawn failed: {exc}")
            return False

        if self._wait_for_health():
            self._broadcast_hot_reload()
            return True
        log_engine("self_healing: agent health timeout after hot reload")
        return False

    def _wait_for_health(self, *, timeout_sec: float = _HEALTH_TIMEOUT_SEC) -> bool:
        import urllib.error
        import urllib.request

        deadline = time.time() + timeout_sec
        url = "http://127.0.0.1:8080/health"
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=3) as resp:
                    if resp.status == 200:
                        return True
            except (urllib.error.URLError, TimeoutError, OSError):
                pass
            time.sleep(2.0)
        return False

    def _broadcast_hot_reload(self) -> None:
        try:
            from cockpit.web_server import broadcast_system_hot_reload

            broadcast_system_hot_reload(source="self_healing_supervisor")
            log_engine("self_healing: SYSTEM_HOT_RELOAD broadcast sent")
        except Exception as exc:
            log_engine(
                f"self_healing: hot reload broadcast failed: {type(exc).__name__}: {exc}"
            )

    def authorize_patch_deployment(self, branch: str) -> dict[str, Any]:
        """Run gate, merge, cleanup, and hot-reload — or freeze on failure."""
        global _frozen, _last_result

        if _frozen:
            return {
                "ok": False,
                "branch": branch,
                "frozen": True,
                "reason": "deployment frozen from prior gate failure",
            }

        log_engine(f"self_healing: evaluating patch branch {branch}")
        gate = self.run_gate_suite()
        if not gate.ok:
            _frozen = True
            result = {
                "ok": False,
                "branch": branch,
                "frozen": True,
                "gate": gate.__dict__,
                "reason": "gate suite failed — deployment frozen",
            }
            _last_result = result
            self._write_audit(result)
            log_engine(
                f"self_healing: FREEZE — {branch} gate failed "
                f"(passed={gate.passed} failed={gate.failed})"
            )
            return result

        if not self.merge_branch_to_main(branch):
            _frozen = True
            result = {
                "ok": False,
                "branch": branch,
                "frozen": True,
                "reason": "merge to main failed — deployment frozen",
            }
            _last_result = result
            self._write_audit(result)
            return result

        ports = self.cleanup_service_ports()
        reloaded = self.hot_reload_agent()
        result = {
            "ok": reloaded,
            "branch": branch,
            "frozen": False,
            "gate": gate.__dict__,
            "ports": ports,
            "hot_reload": reloaded,
        }
        _last_result = result
        self._write_audit(result)
        if reloaded:
            log_engine(f"self_healing: autonomous deployment complete for {branch}")
        return result

    def poll_once(self) -> dict[str, Any] | None:
        branches = self.discover_patch_branches()
        if not branches:
            return None
        return self.authorize_patch_deployment(branches[0])

    def _write_audit(self, payload: dict[str, Any]) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **payload,
        }
        audit_path = logs_dir() / "self_healing_audit.jsonl"
        try:
            with open(audit_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError:
            pass
        event_type = "patch_deploy_ok" if payload.get("ok") else "patch_deploy_freeze"
        if payload.get("frozen"):
            event_type = "deployment_frozen"
        record_supervisor_event(
            event_type,
            detail=str(payload.get("reason") or payload.get("branch") or ""),
            payload=payload,
        )


def co_pilot_vitals_snapshot() -> dict[str, Any]:
    """Flight Deck master vitals — supervisor freeze state and recent fault module."""
    degraded_module = ""
    recent_fault_module = ""
    if _frozen:
        degraded_module = "SelfHealingSupervisor"
        reason = str(_last_result.get("reason") or "").lower()
        if "gate" in reason:
            degraded_module = "DeployRegressionGate"

    try:
        from system.supervisor_history import read_history_last_24h

        rows = read_history_last_24h(max_lines=24)
        for row in reversed(rows):
            event_type = str(row.get("event_type") or "")
            if event_type in (
                "deployment_frozen",
                "patch_deploy_freeze",
                "supervisor_poll_error",
            ):
                recent_fault_module = "SelfHealingSupervisor"
                break
            if event_type == "patch_deploy_ok":
                break
    except Exception:
        pass

    return {
        "deployment_frozen": _frozen,
        "degraded_module": degraded_module,
        "recent_fault_module": recent_fault_module,
        "last_ok": bool(_last_result.get("ok")),
    }


def deployment_frozen() -> bool:
    return _frozen


def last_supervisor_result() -> dict[str, Any]:
    return dict(_last_result)


def start_self_healing_supervisor(*, poll_interval_sec: float = _POLL_INTERVAL_SEC) -> None:
    """Background poll for ``patch_crash_*`` branches (Gate 5 post-ready)."""
    global _supervisor_thread

    if os.environ.get("IG_AGENT_DISABLE_SELF_HEALING") == "1":
        log_engine("self_healing: disabled (IG_AGENT_DISABLE_SELF_HEALING=1)")
        return
    if _supervisor_thread is not None and _supervisor_thread.is_alive():
        return

    try:
        from system.thread_affinity import apply_process_affinity_bootstrap

        apply_process_affinity_bootstrap()
    except Exception:
        pass

    supervisor = SelfHealingSupervisor()

    def _loop() -> None:
        try:
            from system.thread_affinity import pin_current_thread

            pin_current_thread(role="self_healing_supervisor")
        except Exception:
            pass
        load_dotenv()
        log_engine("self_healing: supervisor started")
        while not _supervisor_stop.is_set():
            try:
                supervisor.poll_once()
            except Exception as exc:
                log_engine(
                    f"self_healing: poll error: {type(exc).__name__}: {exc}"
                )
                record_supervisor_event(
                    "supervisor_poll_error",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            _supervisor_stop.wait(poll_interval_sec)

    _supervisor_stop.clear()
    try:
        from system.thread_affinity import spawn_priority_thread

        _supervisor_thread = spawn_priority_thread(
            _loop,
            name="SelfHealingSupervisor",
            role="self_healing_supervisor",
            daemon=True,
        )
    except Exception:
        _supervisor_thread = threading.Thread(
            target=_loop,
            name="SelfHealingSupervisor",
            daemon=True,
        )
        _supervisor_thread.start()


def stop_self_healing_supervisor() -> None:
    _supervisor_stop.set()
