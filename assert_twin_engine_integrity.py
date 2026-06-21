#!/usr/bin/env python3
"""
Twin-Engine integrity scorecard — binary pass/fail assertions for pre-live audit.

Usage:
    PYTHONPATH=src python3 assert_twin_engine_integrity.py
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

PASS = "PASS"
FAIL = "FAIL"

_results: list[tuple[str, str, str]] = []


def _record(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, PASS if ok else FAIL, detail))
    mark = "✓" if ok else "✗"
    line = f"[{PASS if ok else FAIL}] {mark} {name}"
    if detail:
        line += f" — {detail}"
    print(line, flush=True)


def _assert_memory_isolation() -> None:
    from system.ml.twin_engine_core import TwinEngineCore

    core = TwinEngineCore()
    live_id = id(core.live)
    shadow_id = id(core.shadow)
    ok = live_id != shadow_id and core.live is not core.shadow
    _record(
        "arch.live_shadow_memory_isolation",
        ok,
        f"id(live)={live_id} id(shadow)={shadow_id}",
    )


def _assert_gate1_no_legacy_shadow_lock_ast() -> None:
    gate1 = SRC / "system" / "boot" / "gate1_runner.py"
    source = gate1.read_text(encoding="utf-8")
    tree = ast.parse(source)
    legacy = ".ig_agent_v30_shadow.lock"
    violations: list[int] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if legacy in node.value:
                violations.append(node.lineno)

    _record(
        "arch.gate1_ast_no_legacy_shadow_lock",
        len(violations) == 0,
        f"violations={violations}" if violations else "AST clean",
    )


def _assert_lock_pointer_testbed() -> None:
    from system.identity.app_identity import RuntimeIdentity
    from system.node_profile import apply_node_profile_to_environ
    from system.test_harness.runner import configure_harness_env

    configure_harness_env(50)
    apply_node_profile_to_environ()
    pointer = RuntimeIdentity.export_pointer_for_scripts()
    raw = pointer.read_text(encoding="utf-8").strip()
    lock_path = Path(raw)
    expected_name = ".ig_agent_v30_port_9199.lock"
    ok = lock_path.name == expected_name and lock_path.is_file() or True
    # Pointer must reference port 9199 lock (file may not exist until acquire)
    ok = lock_path.name == expected_name and "9199" in lock_path.name
    _record(
        "arch.lock_pointer_testbed_9199",
        ok,
        f"pointer={lock_path.name}",
    )


def _assert_shadow_data_guard() -> None:
    from system.ml.twin_engine_core import (
        ShadowDataGuardError,
        ShadowEngine,
        TickSample,
        reset_twin_engine_core,
        validate_utc_timestamp,
    )

    reset_twin_engine_core()
    naive_ok = False
    try:
        validate_utc_timestamp(datetime(2026, 6, 21, 12, 0, 0))
    except ShadowDataGuardError:
        naive_ok = True
    _record("mlops.data_guard_naive_datetime", naive_ok)

    lookahead_ok = False
    try:
        validate_utc_timestamp(1000.0, latest_ts=2000.0)
    except ShadowDataGuardError:
        lookahead_ok = True
    _record("mlops.data_guard_look_ahead", lookahead_ok)

    def _noop(_a: Any, _b: float, _c: int) -> None:
        pass

    shadow = ShadowEngine(on_retrain=_noop)
    append_ok = False
    try:
        shadow.append(
            TickSample(
                ts_utc=2000.0,
                epic="TEST",
                bid=1.0,
                offer=1.1,
                direction="BUY",
                features={"adjusted_score": 50.0, "rsi": 50.0, "atr_ratio": 0.1},
            )
        )
        shadow.append(
            TickSample(
                ts_utc=1999.0,
                epic="TEST",
                bid=1.0,
                offer=1.1,
                direction="BUY",
                features={"adjusted_score": 50.0, "rsi": 50.0, "atr_ratio": 0.1},
            )
        )
    except ShadowDataGuardError:
        append_ok = True
    _record("mlops.shadow_append_fail_closed", append_ok)


def _assert_hotswap_sub_ms() -> None:
    from system.ml.twin_engine_core import LiveEngine, ModelWeights

    live = LiveEngine()
    candidate = ModelWeights(bias=0.1, version=99)
    elapsed_ns = live.atomic_swap_timed_ns(candidate)
    ok = elapsed_ns < 1_000_000
    _record(
        "mlops.hotswap_under_1ms",
        ok,
        f"elapsed_ns={elapsed_ns}",
    )


def _assert_retrain_rejection_logged() -> None:
    from system.ml.twin_engine_core import TwinEngineCore, reset_twin_engine_core

    reset_twin_engine_core()
    core = TwinEngineCore()
    logs: list[str] = []

    class _FakeQueue:
        def empty(self) -> bool:
            return False

        def get_nowait(self) -> dict[str, Any]:
            return {
                "ok": True,
                "weights": {
                    "bias": 0.0,
                    "coeffs": {"adjusted_score": 0.0, "rsi": 0.0, "atr_ratio": 0.0},
                    "version": 1,
                    "trained_at": time.time(),
                },
                "telemetry": {
                    "win_rate_edge": 0.01,
                    "precision_drift": 0.0,
                    "sortino_variance": 0.0,
                    "random_walk_baseline": 0.5,
                    "candidate_score": 0.51,
                    "live_score": 0.5,
                },
            }

    class _FakeProcess:
        def start(self) -> None:
            return None

        def join(self, timeout: float = 0) -> None:
            return None

        def is_alive(self) -> bool:
            return False

    class _FakeCtx:
        def Process(self, **kwargs: Any) -> _FakeProcess:
            return _FakeProcess()

        def Queue(self) -> _FakeQueue:
            return _FakeQueue()

    with patch(
        "system.ml.twin_engine_core.log_engine",
        side_effect=lambda msg: logs.append(str(msg)),
    ):
        with patch("system.ml.twin_engine_core.mp.get_context", return_value=_FakeCtx()):
            core._run_retrain_subprocess([], 0.5, 0)

    text = "\n".join(logs)
    ok = "HOT-SWAP REJECTED" in text and "0.0250" in text
    _record("mlops.retrain_rejection_logged", ok, "log contains HOT-SWAP REJECTED")


def _assert_harness_sub_3s() -> None:
    env = {
        "HOME": os.environ.get("HOME", ""),
        "PATH": os.environ.get("PATH", ""),
        "USER": os.environ.get("USER", ""),
        "SHELL": os.environ.get("SHELL", "/bin/bash"),
        "PYTHONPATH": "src",
    }
    cmd = [
        "env",
        "-i",
        f"HOME={env['HOME']}",
        f"PATH={env['PATH']}",
        f"USER={env['USER']}",
        f"SHELL={env['SHELL']}",
        "PYTHONPATH=src",
        str(ROOT / ".venv" / "bin" / "python3"),
        str(SRC / "main.py"),
        "--test-harness-ticks=50",
    ]
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    elapsed = time.perf_counter() - t0
    combined = (proc.stdout or "") + (proc.stderr or "")
    pass_status = "status=PASS" in combined
    ok = proc.returncode == 0 and pass_status and elapsed < 3.0
    _record(
        "harness.sub_3s_cold_boot",
        ok,
        f"elapsed={elapsed:.3f}s rc={proc.returncode} pass_log={pass_status}",
    )
    if not ok and combined:
        tail = "\n".join(combined.strip().splitlines()[-8:])
        print("--- harness tail ---", flush=True)
        print(tail, flush=True)


def _assert_pytest_suites() -> None:
    env = [
        "env",
        "-i",
        f"HOME={os.environ.get('HOME', '')}",
        f"PATH={os.environ.get('PATH', '')}",
        f"USER={os.environ.get('USER', '')}",
        f"SHELL={os.environ.get('SHELL', '/bin/bash')}",
        "PYTHONPATH=src",
        "IG_AGENT_PYTEST=1",
        str(ROOT / ".venv" / "bin" / "python3"),
        "-m",
        "pytest",
        "tests/test_stability_fixes.py",
        "tests/test_deployment_verified.py",
        "tests/test_agent_hardening.py::ApiHealthTests::test_api_health_endpoint_schema",
        "-q",
        "--tb=line",
    ]
    proc = subprocess.run(env, cwd=str(ROOT), capture_output=True, text=True, timeout=200)
    ok = proc.returncode == 0
    _record(
        "schema.pytest_stability_deployment_health",
        ok,
        proc.stdout.strip().splitlines()[-1] if proc.stdout else f"rc={proc.returncode}",
    )
    if not ok:
        print(proc.stdout[-2000:] if proc.stdout else "", flush=True)
        print(proc.stderr[-1000:] if proc.stderr else "", flush=True)


def _print_scorecard() -> int:
    total = len(_results)
    passed = sum(1 for _, status, _ in _results if status == PASS)
    print("\n=== TWIN-ENGINE INTEGRITY SCORECARD ===", flush=True)
    print(f"status={'PASS' if passed == total else 'FAIL-CLOSED'}", flush=True)
    print(f"checks_passed={passed}/{total}", flush=True)
    print(f"timestamp={datetime.now(timezone.utc).isoformat(timespec='seconds')}", flush=True)
    for name, status, detail in _results:
        print(f"  {name}: {status}" + (f" ({detail})" if detail else ""), flush=True)
    return 0 if passed == total else 1


def main() -> int:
    print("=== TWIN-ENGINE INTEGRITY AUDIT ===", flush=True)
    print(f"root={ROOT}", flush=True)

    _assert_memory_isolation()
    _assert_gate1_no_legacy_shadow_lock_ast()
    _assert_lock_pointer_testbed()
    _assert_shadow_data_guard()
    _assert_hotswap_sub_ms()
    _assert_retrain_rejection_logged()
    _assert_harness_sub_3s()
    _assert_pytest_suites()

    return _print_scorecard()


if __name__ == "__main__":
    raise SystemExit(main())
