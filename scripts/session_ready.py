#!/usr/bin/env python3
"""
Today's trading session prep — implements operator recommendations in one pass.

Steps (offline-safe unless --live):
  1. Purge stale runtime_state pending exits
  2. Reconcile phantom learning-db opens (broker-authoritative)
  3. Verify demo throughput config + position size floors
  4. Pre-flight static checks
  5. Optional: start agent + live smoke test

Usage:
  IG_AGENT_CONFIG=config/config_v31_demo_throughput.json \\
    PYTHONPATH=src python3 scripts/session_ready.py

  IG_AGENT_CONFIG=config/config_v31_demo_throughput.json \\
    PYTHONPATH=src python3 scripts/session_ready.py --start-agent
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("APP_MODE", "DEMO")

SESSION_CONFIG = "config/config_v31_demo_throughput.json"

# Hard ceiling for the whole start-agent run (offline prep + boot to healthy).
# A boot that has not bound the API and reported healthy within this window is
# treated as hung: we kill the spawned agent tree, clear locks, and exit
# non-zero so no half-dead process is left holding a HEALTHY-looking session
# lock (the failure mode that caused the 76-minute restart-fail loop).
_DEFAULT_BOOT_DEADLINE_SEC = 300.0
_agent_proc: subprocess.Popen[Any] | None = None
HOT_EPICS = ("IX.D.DOW.IFM.IP", "IX.D.NIKKEI.IFM.IP")
EXPECTED_SIZES = {
    "IX.D.DOW.IFM.IP": 0.5,
    "IX.D.NIKKEI.IFM.IP": 0.5,
    "CS.D.CFPGOLD.CFP.IP": 10.0,
}


def _log(msg: str) -> None:
    print(msg, flush=True)


def _child_pids(parent: int) -> list[int]:
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


def _api_healthy(port: int = 8080, timeout: float = 2.0) -> bool:
    """True when local API answers healthy — never SIGKILL that listener."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{int(port)}/api/health", timeout=timeout
        ) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return bool(body.get("ok") or body.get("agent_alive") or body.get("trade_ready"))
    except (OSError, urllib.error.URLError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _listener_pids(port: int = 8080) -> set[int]:
    try:
        result = subprocess.run(
            ["lsof", f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return {int(x) for x in (result.stdout or "").split() if x.strip().isdigit()}
    except (OSError, subprocess.SubprocessError, ValueError):
        return set()


def _pid_serves_healthy_api(pid: int, port: int = 8080) -> bool:
    """Never escalate to SIGKILL for a PID that currently serves healthy :8080."""
    if pid <= 0:
        return False
    if pid in _listener_pids(port) and _api_healthy(port):
        return True
    # Also protect parent trees of the healthy listener (caffeinate wrappers).
    for listener in _listener_pids(port):
        if listener == pid:
            return _api_healthy(port)
        # If pid is an ancestor of the listener, protect it when API is healthy.
        try:
            cur = listener
            for _ in range(8):
                result = subprocess.run(
                    ["/bin/ps", "-o", "ppid=", "-p", str(cur)],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
                ppid = int((result.stdout or "0").strip() or 0)
                if ppid <= 1:
                    break
                if ppid == pid and _api_healthy(port):
                    return True
                cur = ppid
        except (OSError, subprocess.SubprocessError, ValueError):
            break
    return False


def _kill_process_tree(pid: int, *, allow_healthy_api: bool = False) -> None:
    """SIGTERM then SIGKILL a process and its descendants (depth-first).

    Never SIGKILL a PID that already serves healthy :8080 unless explicitly
    allowed (should never be needed for session_ready abort paths).
    """
    if pid <= 0:
        return
    if not allow_healthy_api and _pid_serves_healthy_api(pid):
        _log(
            f"  REFUSING kill of pid={pid} — already serves healthy :8080 "
            "(leaving live agent intact)"
        )
        return
    children = _child_pids(pid)
    for child in children:
        _kill_process_tree(child, allow_healthy_api=allow_healthy_api)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if sig is signal.SIGKILL and not allow_healthy_api and _pid_serves_healthy_api(pid):
            _log(f"  REFUSING SIGKILL pid={pid} — healthy :8080 listener")
            return
        try:
            os.kill(pid, sig)
        except OSError:
            return
        if sig is signal.SIGTERM:
            for _ in range(20):
                try:
                    os.kill(pid, 0)
                except OSError:
                    return
                time.sleep(0.25)


def _clear_boot_locks() -> None:
    """Clear session + instance locks so a hung boot never blocks the next start."""
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
        path = lock_path_for_scope(scope, root)
        # A hung boot leaves a zombie/defunct holder — clear_stale_lock now reaps
        # that (zombie-aware). Fall back to unlink for a genuinely stuck file.
        if not clear_stale_lock(path) and path.is_file():
            path.unlink(missing_ok=True)
    except Exception as exc:
        _log(f"  lock clear skipped: {type(exc).__name__}: {exc}")
    try:
        from system.identity.instance_lock import force_release_instance_lock

        force_release_instance_lock()
    except Exception:
        pass


def _abort_hung_boot(reason: str, exit_code: int = 3) -> None:
    """Fail loud: kill the spawned agent tree, clear locks, exit non-zero.

    If :8080 is already healthy, refuse to kill the live agent — the hung
    starter may be a duplicate; leave the serving process alone.
    """
    global _agent_proc
    _log(f"STARTUP BLOCKED — {reason}")
    if _api_healthy():
        _log(
            "  :8080 already healthy — refusing SIGKILL of live agent; "
            "clearing only our spawned starter if distinct"
        )
        listeners = _listener_pids(8080)
        if _agent_proc is not None and _agent_proc.pid not in listeners:
            try:
                _kill_process_tree(_agent_proc.pid)
            except Exception:
                pass
        try:
            from system.telegram_notifier import send_critical_alert

            send_critical_alert(
                f"Agent startup blocked — {reason}. Live :8080 left intact.",
                dedupe_key="session_ready_boot_blocked_healthy",
            )
        except Exception:
            pass
        os._exit(exit_code)

    if _agent_proc is not None:
        try:
            _kill_process_tree(_agent_proc.pid)
        except Exception:
            pass
    # Kill any lingering project main.py that we may have spawned — never a
    # healthy listener (guard inside _kill_process_tree).
    try:
        result = subprocess.run(
            ["/usr/bin/pgrep", "-f", "src/main.py"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        for x in (result.stdout or "").split():
            if x.strip().isdigit():
                _kill_process_tree(int(x))
    except (OSError, subprocess.SubprocessError):
        pass
    _clear_boot_locks()
    try:
        from system.telegram_notifier import send_critical_alert

        send_critical_alert(
            f"Agent startup blocked — {reason}. Locks cleared; retry pending.",
            dedupe_key="session_ready_boot_blocked",
        )
    except Exception:
        pass
    # os._exit avoids atexit hooks that could re-touch locks during a failed boot.
    os._exit(exit_code)


def _install_deadline(deadline_sec: float) -> None:
    """Arm a hard SIGALRM ceiling covering offline prep + boot-to-healthy."""
    if deadline_sec <= 0:
        return

    def _on_alarm(_signum: int, _frame: Any) -> None:
        _abort_hung_boot(f"hard deadline {int(deadline_sec)}s exceeded", exit_code=3)

    try:
        signal.signal(signal.SIGALRM, _on_alarm)
        signal.setitimer(signal.ITIMER_REAL, float(deadline_sec))
    except (ValueError, OSError):
        pass


def _cancel_deadline() -> None:
    try:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
    except (ValueError, OSError):
        pass


def _audit_block() -> None:
    import subprocess as sp

    port = bool(sp.run(["lsof", "-iTCP:8080", "-sTCP:LISTEN"], capture_output=True).stdout.strip())
    try:
        from system.shutdown_cleanup import manual_stop_active

        hold = manual_stop_active()
    except Exception:
        hold = "unknown"
    _log(f"[Audit] port8080_listening={port} watchdog_hold={hold}")


def _verify_config(cfg: Any) -> list[str]:
    issues: list[str] = []
    dual = cfg.get("dual_core") or {}
    if not dual.get("multi_source_auto_rotation"):
        issues.append("dual_core.multi_source_auto_rotation not enabled")
    explore = cfg.get("portfolio_exploration") or {}
    if not explore.get("enabled"):
        issues.append("portfolio_exploration.enabled false")
    pm = cfg.get("position_management") or {}
    if not pm.get("manager_enabled"):
        issues.append("position_management.manager_enabled false")
    runner = cfg.get("long_trade_runner") or {}
    if not runner.get("enabled"):
        issues.append("long_trade_runner.enabled false")
    excluded = set(dual.get("exclude_from_hot_path") or [])
    # DOW must remain on hot path; Nikkei may be excluded until JPY PnL fix is certified live.
    if "IX.D.DOW.IFM.IP" in excluded:
        issues.append("hot epic excluded: IX.D.DOW.IFM.IP")
    if "CS.D.CFPGOLD.CFP.IP" not in excluded:
        issues.append("Gold should stay excluded from hot path")
    floors = cfg.get("ig_deal_size_floors") or {}
    for epic, want in EXPECTED_SIZES.items():
        got = floors.get(epic)
        if got != want:
            issues.append(f"size floor {epic}: want {want} got {got}")
    return issues


def _verify_executable_sizes(cfg: Any) -> list[str]:
    from execution.ig_size_validator import resolve_executable_lot_size

    issues: list[str] = []
    for epic, want in EXPECTED_SIZES.items():
        res = resolve_executable_lot_size(epic, want, "BUY", cfg, None)
        if not res.ok or float(res.size) != float(want):
            issues.append(f"{epic}: executable={res.size} want={want} ok={res.ok}")
    return issues


def _live_smoke(timeout_sec: float = 90.0) -> dict[str, Any]:
    import urllib.request

    base = "http://127.0.0.1:8080"
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/api/health_light", timeout=3) as resp:
                if resp.status == 200:
                    break
        except Exception:
            time.sleep(2)
    else:
        return {"ok": False, "error": "health_light timeout"}

    out: dict[str, Any] = {"ok": True}
    for path in (
        "/api/health_light",
        "/api/rotation_state",
        "/api/positions/live",
        "/api/position_manager/status",
        "/api/learning-health",
    ):
        try:
            with urllib.request.urlopen(f"{base}{path}", timeout=5) as resp:
                out[path] = json.loads(resp.read().decode())
        except Exception as exc:
            out[path] = {"error": f"{type(exc).__name__}: {exc}"}
    hl = out.get("/api/health_light") or {}
    rot = out.get("/api/rotation_state") or {}
    pos = out.get("/api/positions/live") or {}
    out["summary"] = {
        "exec_active": (hl.get("execution") or {}).get("loop_active"),
        "rotation_sweep": (rot.get("rotation") or rot).get("rotation_sweep_count")
        if isinstance(rot, dict)
        else None,
        "positions": pos.get("count"),
        "position_verdict": pos.get("verdict"),
    }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Session-ready prep for today's demo trading")
    parser.add_argument("--start-agent", action="store_true", help="Start agent after offline prep")
    parser.add_argument("--skip-phantom", action="store_true")
    parser.add_argument("--dry-run-phantom", action="store_true", default=False)
    parser.add_argument(
        "--boot-deadline-sec",
        type=float,
        default=float(os.environ.get("IG_BOOT_DEADLINE_SEC", _DEFAULT_BOOT_DEADLINE_SEC)),
        help="Hard ceiling for start-agent run; hung boots are killed + locks cleared",
    )
    args = parser.parse_args()

    _log("=== IG Agent session ready ===")
    _audit_block()

    # Arm the hard deadline as early as possible so even a hung offline-prep step
    # (e.g. a broker REST call with no timeout) cannot leave this process wedged.
    if args.start_agent:
        _install_deadline(float(args.boot_deadline_sec))

    from purge_stale_runtime_pending import purge_stale_pending

    pending = purge_stale_pending(dry_run=False)
    _log(f"[1/5] Stale pending purged: removed={pending['removed']} kept={pending['kept']}")

    if not args.skip_phantom:
        from reconcile_phantom_opens import reconcile_phantom_opens

        try:
            phantom = reconcile_phantom_opens(dry_run=bool(args.dry_run_phantom))
            _log(
                f"[2/5] Phantom opens: broker={phantom['broker_open']} "
                f"db_open={phantom['db_open_before']} closed={phantom['closed']}"
            )
        except Exception as exc:
            _log(f"[2/5] Phantom reconcile skipped: {type(exc).__name__}: {exc}")

    from system.config_loader import get_config

    cfg = get_config()
    cfg_issues = _verify_config(cfg)
    size_issues = _verify_executable_sizes(cfg)
    if cfg_issues or size_issues:
        for issue in cfg_issues + size_issues:
            _log(f"  CONFIG ISSUE: {issue}")
        _log("[3/5] Config verification: FAIL")
        return 1
    _log("[3/5] Config + size floors: PASS")

    from system.pre_flight_checks import pre_flight_summary, run_all_pre_flight_checks

    pf = pre_flight_summary(run_all_pre_flight_checks(require_live_agent=False))
    _log(f"[4/5] Pre-flight static: {pf['passed']}/{pf['total']} pass")

    if args.start_agent:
        global _agent_proc

        env = os.environ.copy()
        env.setdefault("APP_MODE", "DEMO")
        env.setdefault("IG_AGENT_CONFIG", SESSION_CONFIG)
        env["PYTHONPATH"] = str(ROOT / "src")
        _log("[5/5] Starting agent...")
        _agent_proc = subprocess.Popen(
            ["bash", str(ROOT / "scripts/start_agent_background.sh")],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _log(f"  agent pid={_agent_proc.pid}")

        # Reserve a margin so, if health never comes, we still abort loudly a bit
        # before the SIGALRM ceiling and produce a clean diagnostic.
        smoke_budget = max(60.0, float(args.boot_deadline_sec) - 30.0)
        # Arm post-boot REST storm guard so positions polls serialize while
        # trade_support / OPM fanout settles (entries also paused via budget).
        try:
            from system.rest_api_budget import mark_post_boot_rest_guard

            mark_post_boot_rest_guard(grace_sec=90.0)
            _log("[5/5] post-boot REST guard armed 90s")
        except Exception as exc:
            _log(f"[5/5] post-boot REST guard skipped: {type(exc).__name__}")

        smoke = _live_smoke(timeout_sec=min(smoke_budget, 75.0))
        _log(f"[5/5] Live smoke: {json.dumps(smoke.get('summary', smoke), indent=2)}")
        if not smoke.get("ok"):
            _abort_hung_boot(
                f"agent did not reach health within {int(smoke_budget)}s "
                f"({smoke.get('error', 'unhealthy')})",
                exit_code=3,
            )
        _cancel_deadline()
        _log("SESSION READY — profile: config_v31_demo_throughput.json")
        # Hard exit — importing REST/triage workers can wedge CPython on
        # Py_Finalize and leave desk_deploy hung forever.
        os._exit(0)
    else:
        _log("[5/5] Agent start skipped (pass --start-agent to launch)")

    _log("SESSION READY — profile: config_v31_demo_throughput.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
