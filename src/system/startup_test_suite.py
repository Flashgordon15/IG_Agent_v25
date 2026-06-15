"""
Final startup gate — run the full pytest suite before marking the agent ready.

Failures are logged, persisted for operational AI review, and surfaced on the
startup splash via startup_tracker.set_error().
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.paths import data_dir, project_root

_FAILURE_PATH = data_dir() / "state" / "startup_test_failure.json"


@dataclass(frozen=True)
class StartupTestSuiteResult:
    ok: bool
    note: str
    passed: int | None = None
    failed: int | None = None
    errors: int | None = None
    duration_sec: float | None = None
    output_tail: str = ""


def _skip_reason() -> str | None:
    if os.environ.get("IG_AGENT_PYTEST") == "1":
        return "skipped in test context"
    if os.environ.get("IG_AGENT_SKIP_DEPLOY_CHECK") == "1":
        return "skipped watchdog restart"
    if os.environ.get("IG_AGENT_SKIP_STARTUP_TEST_SUITE") == "1":
        return "skipped by env"
    try:
        from system.config_loader import get_config

        cfg = get_config()
        block = cfg.get("startup_test_suite", {})
        if isinstance(block, dict) and block.get("enabled") is False:
            return "disabled in config"
    except Exception:
        pass
    return None


def _suite_settings() -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "enabled": True,
        "timeout_sec": 600.0,
        "block_trading_on_failure": True,
    }
    try:
        from system.config_loader import get_config

        raw = get_config().get("startup_test_suite", {})
        if isinstance(raw, dict):
            return {**defaults, **raw}
    except Exception:
        pass
    return defaults


def _parse_pytest_summary(text: str) -> tuple[int | None, int | None, int | None]:
    passed = failed = errors = None
    for line in reversed(text.splitlines()):
        if "passed" not in line and "failed" not in line and "error" not in line:
            continue
        m_passed = re.search(r"(\d+) passed", line)
        m_failed = re.search(r"(\d+) failed", line)
        m_error = re.search(r"(\d+) error", line)
        if m_passed:
            passed = int(m_passed.group(1))
        if m_failed:
            failed = int(m_failed.group(1))
        if m_error:
            errors = int(m_error.group(1))
        if passed is not None or failed is not None or errors is not None:
            break
    return passed, failed, errors


def _write_failure_report(payload: dict[str, Any]) -> None:
    path = _FAILURE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def clear_failure_report() -> None:
    try:
        _FAILURE_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def read_failure_report() -> dict[str, Any] | None:
    try:
        if not _FAILURE_PATH.is_file():
            return None
        return json.loads(_FAILURE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def run_startup_test_suite() -> StartupTestSuiteResult:
    """Execute tests/ via pytest. Returns ok=False when the suite does not pass."""
    skip = _skip_reason()
    if skip:
        log_engine(f"startup test suite: {skip}")
        return StartupTestSuiteResult(ok=True, note=skip)

    settings = _suite_settings()
    timeout = float(settings.get("timeout_sec") or 600.0)
    root = project_root()
    tests_dir = root / "tests"
    if not tests_dir.is_dir():
        msg = f"tests directory missing: {tests_dir}"
        log_engine(f"startup test suite FAILED — {msg}")
        return StartupTestSuiteResult(ok=False, note=msg)

    log_engine("startup test suite: running full pytest suite (final launch gate)")
    started = time.monotonic()
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/",
        "-q",
        "--tb=line",
    ]
    env = {
        **os.environ,
        "PYTHONPATH": str(root / "src"),
        "IG_AGENT_PYTEST": "1",
        "CI": os.environ.get("CI", "true"),
    }
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        tail = (e.stdout or "")[-2000:] + (e.stderr or "")[-2000:]
        note = f"timeout after {int(timeout)}s"
        _persist_failure(note=note, tail=tail, duration_sec=timeout)
        log_engine(f"startup test suite FAILED — {note}")
        return StartupTestSuiteResult(
            ok=False,
            note=note,
            duration_sec=timeout,
            output_tail=tail[-800:],
        )
    except Exception as e:
        note = f"{type(e).__name__}: {e}"
        log_engine(f"startup test suite FAILED — {note}")
        return StartupTestSuiteResult(ok=False, note=note)

    duration = time.monotonic() - started
    combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    tail = combined[-4000:]
    passed, failed, errors = _parse_pytest_summary(combined)
    ok = proc.returncode == 0

    if ok:
        clear_failure_report()
        summary = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "ok"
        note = summary if summary else "all passed"
        log_engine(
            f"startup test suite passed in {duration:.1f}s — {note}"
        )
        return StartupTestSuiteResult(
            ok=True,
            note=note,
            passed=passed,
            failed=failed,
            errors=errors,
            duration_sec=duration,
            output_tail=tail[-400:],
        )

    note = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "pytest failed"
    _persist_failure(
        note=note,
        tail=tail,
        duration_sec=duration,
        passed=passed,
        failed=failed,
        errors=errors,
        returncode=proc.returncode,
    )
    log_engine(
        f"startup test suite FAILED in {duration:.1f}s — {note}\n{tail[-1200:]}"
    )
    try:
        from system.telegram_notifier import send_critical_alert

        send_critical_alert(
            f"Startup test suite FAILED — trading blocked until resolved\n{note}",
            dedupe_key="startup_test_suite_fail",
        )
    except Exception as e:
        log_engine(
            f"startup test suite telegram alert failed: {type(e).__name__}: {e}"
        )
    try:
        from ai.operational.system_monitor import get_system_monitor

        get_system_monitor().record_startup_test_failure(note, tail=tail[-2000:])
    except Exception:
        pass

    return StartupTestSuiteResult(
        ok=False,
        note=note,
        passed=passed,
        failed=failed,
        errors=errors,
        duration_sec=duration,
        output_tail=tail[-800:],
    )


def _persist_failure(
    *,
    note: str,
    tail: str,
    duration_sec: float,
    passed: int | None = None,
    failed: int | None = None,
    errors: int | None = None,
    returncode: int | None = None,
) -> None:
    payload = {
        "note": note,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "returncode": returncode,
        "duration_sec": round(duration_sec, 2),
        "output_tail": tail[-8000:],
        "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        _write_failure_report(payload)
    except Exception as e:
        log_engine(f"startup test suite failure report not written: {e}")
