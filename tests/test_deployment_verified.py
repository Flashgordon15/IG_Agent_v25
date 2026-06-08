"""
Deployment health check — verifies every critical change made today is live.

Run with:
    PYTHONPATH=src python3 -m pytest tests/test_deployment_verified.py -v

If ANY test fails the agent must NOT be trusted for live trading.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# Paths resolved relative to this file so the suite is location-independent
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
_MAIN_PY = _SRC / "main.py"
_GUARD_PY = _SRC / "execution" / "correlation_guard.py"
_APP_JSX = _ROOT / "dashboard" / "src" / "App.jsx"
_RUNTIME_STATE = _SRC / "data" / "runtime_state.json"


# ---------------------------------------------------------------------------
# 1. Correlation guard cap
# ---------------------------------------------------------------------------


def test_correlation_guard_cap_is_5() -> None:
    """MAX_NEW_PER_DIRECTION must be 5 (v25.6 profitability cap).

    Limits same-direction entries per calendar day to reduce correlated
    portfolio risk on a ~4-market book.
    """
    source = _GUARD_PY.read_text(encoding="utf-8")
    assert "MAX_NEW_PER_DIRECTION = 5" in source, (
        "correlation_guard.py must contain 'MAX_NEW_PER_DIRECTION = 5'; "
        "the limit has been changed or removed — do NOT deploy."
    )

    import execution.correlation_guard as cg

    assert cg.MAX_NEW_PER_DIRECTION == 5, (
        f"Imported value is {cg.MAX_NEW_PER_DIRECTION!r}; expected 5"
    )


# ---------------------------------------------------------------------------
# 2. Pycache cleared on startup
# ---------------------------------------------------------------------------


def test_pycache_clear_called_on_startup() -> None:
    """_clear_pycache() must be defined AND called inside _pre_startup_cleanup.

    Stale .pyc files can hide live code changes from the running interpreter.
    Clearing __pycache__ on every launch guarantees that any hotfix pushed to
    disk is actually executed rather than silently shadowed by old bytecode.
    """
    source = _MAIN_PY.read_text(encoding="utf-8")

    assert "def _clear_pycache(" in source, (
        "main.py must define a '_clear_pycache' function"
    )

    # Locate _pre_startup_cleanup and check _clear_pycache() is called inside it
    # Strategy: find the function body by slicing from its def line to the next
    # top-level def/class.
    lines = source.splitlines()
    in_func = False
    func_body_lines: list[str] = []
    for line in lines:
        if line.startswith("def _pre_startup_cleanup("):
            in_func = True
            continue
        if in_func:
            # A new top-level def/class ends the function body
            if (
                line
                and not line[0].isspace()
                and (line.startswith("def ") or line.startswith("class "))
            ):
                break
            func_body_lines.append(line)

    func_body = "\n".join(func_body_lines)
    assert "_clear_pycache()" in func_body, (
        "_clear_pycache() is not called inside _pre_startup_cleanup — "
        "pycache will not be cleared on startup"
    )


# ---------------------------------------------------------------------------
# 3. smoke_test phase present in startup tracker
# ---------------------------------------------------------------------------


def test_smoke_test_phase_in_startup_tracker() -> None:
    """'smoke_test' must be a registered startup phase between self_test and ohlc.

    The StartupSplash shows each phase in order; if smoke_test is missing the
    dashboard will either skip a visual step or the API will return an unknown
    phase ID.  The ordering (self_test → smoke_test → ohlc) reflects the
    actual boot sequence and must be preserved.
    """
    from system import startup_tracker as st

    phase_ids = [p[0] for p in st.PHASES]
    assert "smoke_test" in phase_ids, "'smoke_test' is not in startup_tracker.PHASES"

    idx_smoke = phase_ids.index("smoke_test")
    idx_self_test = phase_ids.index("self_test")
    idx_ohlc = phase_ids.index("ohlc")

    assert idx_self_test < idx_smoke < idx_ohlc, (
        f"smoke_test must appear between self_test and ohlc in PHASES; "
        f"got self_test={idx_self_test}, smoke_test={idx_smoke}, ohlc={idx_ohlc}"
    )

    # Also verify the progress percentage is between the two neighbours
    pct_map = {p[0]: p[2] for p in st.PHASES}
    assert pct_map["self_test"] < pct_map["smoke_test"] < pct_map["ohlc"], (
        "smoke_test progress % must be strictly between self_test and ohlc"
    )


# ---------------------------------------------------------------------------
# 4. runtime_state is redirected in tests
# ---------------------------------------------------------------------------


def test_runtime_state_isolated_in_tests() -> None:
    """In the test env, runtime_state_persist must NOT write to the live file.

    conftest.py redirects writes to a tmp path via set_state_path_for_tests().
    If this isolation is absent the test suite can corrupt live runtime state
    (e.g. clearing in-flight entries mid-session).  We verify the two control
    functions exist and that the module exposes them as the contract.
    """
    import system.runtime_state_persist as rsp

    assert hasattr(rsp, "set_state_path_for_tests"), (
        "runtime_state_persist must expose set_state_path_for_tests()"
    )
    assert hasattr(rsp, "reset_persist_state_for_tests"), (
        "runtime_state_persist must expose reset_persist_state_for_tests()"
    )
    assert callable(rsp.set_state_path_for_tests)
    assert callable(rsp.reset_persist_state_for_tests)

    # In this test the conftest fixture has already redirected the path;
    # confirm the live file is NOT the current target.
    live_path = (_SRC / "data" / "runtime_state.json").resolve()
    assert rsp._path().resolve() != live_path, (
        "runtime_state_persist is pointing at the LIVE runtime_state.json "
        "inside a pytest run — the conftest isolation fixture is broken."
    )


# ---------------------------------------------------------------------------
# 5. Splash shows on every load (guard removed)
# ---------------------------------------------------------------------------


def test_splash_shows_on_every_load() -> None:
    """App.jsx must NOT contain a 'shown once' guard for the startup splash.

    The guard (previously keyed on 'alreadyDismissed' or 'shown_for_version'
    in sessionStorage) caused the splash to skip on subsequent tab navigations
    in the same session.  Removing it ensures the user always sees the startup
    progress screen when clicking the desktop icon, giving confidence the agent
    is healthy before trading starts.
    """
    source = _APP_JSX.read_text(encoding="utf-8")

    for guard_string in ("alreadyDismissed", "shown_for_version"):
        assert guard_string not in source, (
            f"App.jsx still contains the once-only splash guard '{guard_string}'; "
            "the splash will be silently skipped on reload — remove the guard."
        )


# ---------------------------------------------------------------------------
# 6. Port cleanup registered on exit
# ---------------------------------------------------------------------------


def test_port_cleanup_registered_on_exit() -> None:
    """main.py must define _force_cleanup_port and register it with atexit.

    Without an atexit handler a crashed or force-quit agent can leave a
    zombie uvicorn process bound to :8080, causing every subsequent launch to
    immediately fail with 'port already in use'.  The handler kills lingering
    listeners on exit so a clean restart is always possible.
    """
    source = _MAIN_PY.read_text(encoding="utf-8")

    assert "def _force_cleanup_port(" in source, (
        "main.py must define '_force_cleanup_port'; port cleanup on exit is missing"
    )
    assert "atexit.register(_force_cleanup_port)" in source, (
        "main.py must call atexit.register(_force_cleanup_port); "
        "the cleanup handler will never fire without this registration"
    )


# ---------------------------------------------------------------------------
# 7. _force_cleanup_port skips own PID
# ---------------------------------------------------------------------------


def test_stale_port_killed_not_own_pid() -> None:
    """_force_cleanup_port must kill stale PIDs but must never kill itself.

    If the function accidentally sends SIGTERM to its own PID it will abort
    the normal shutdown sequence mid-flight, potentially leaving open positions
    unrecorded and the runtime state file in a corrupt state.
    """
    import main as _main

    mock_result = MagicMock()
    mock_result.stdout = "12345\n99999\n"

    with (
        patch("subprocess.run", return_value=mock_result),
        patch("os.getpid", return_value=12345),
        patch("os.kill") as mock_kill,
    ):
        _main._force_cleanup_port(port=8080)

    killed_pids = [c.args[0] for c in mock_kill.call_args_list]
    assert 99999 in killed_pids, "PID 99999 (stale listener) should have been killed"
    assert 12345 not in killed_pids, "Own PID 12345 must never be killed"


# ---------------------------------------------------------------------------
# 8. No test artifacts in live runtime_state.json
# ---------------------------------------------------------------------------


def test_no_live_artifacts_in_runtime_state() -> None:
    """runtime_state.json must not contain test fixture strings like 'EPIC1'.

    If the test suite ever writes to the live state file it can inject fake
    in-flight entries that confuse the broker reconciliation logic on the next
    real session, potentially blocking legitimate trades or triggering spurious
    stops.
    """
    if not _RUNTIME_STATE.exists():
        pytest.skip("runtime_state.json does not exist yet — skipping")

    content = _RUNTIME_STATE.read_text(encoding="utf-8")

    test_markers = ("EPIC1", "EPIC2", "TEST_EPIC", "pytest", "__test__")
    for marker in test_markers:
        assert marker not in content, (
            f"Live runtime_state.json contains test artifact string '{marker}'; "
            "the test isolation fixture has failed — do NOT start live trading."
        )


# ---------------------------------------------------------------------------
# 9. Deployment verification runs on startup
# ---------------------------------------------------------------------------


def test_deploy_check_called_on_startup() -> None:
    """_run_deployment_verification must be defined AND called in _pre_startup_cleanup.

    This guarantees that every time the agent boots it self-verifies all critical
    deployment checks before any trading loops start.  If this guard is missing a
    code regression could silently reach live trading without detection.
    """
    source = _MAIN_PY.read_text(encoding="utf-8")

    assert "def _run_deployment_verification(" in source, (
        "main.py must define '_run_deployment_verification'; "
        "startup self-verification is missing"
    )

    # Locate _pre_startup_cleanup body and confirm the call is inside it
    lines = source.splitlines()
    in_func = False
    func_body_lines: list[str] = []
    for line in lines:
        if line.startswith("def _pre_startup_cleanup("):
            in_func = True
            continue
        if in_func:
            if (
                line
                and not line[0].isspace()
                and (line.startswith("def ") or line.startswith("class "))
            ):
                break
            func_body_lines.append(line)

    func_body = "\n".join(func_body_lines)
    assert "_run_deployment_verification()" in func_body, (
        "_run_deployment_verification() is not called inside _pre_startup_cleanup — "
        "deployment self-check will be skipped on every startup"
    )


# ---------------------------------------------------------------------------
# 10. Watchdog script exists and is executable
# ---------------------------------------------------------------------------


def test_watchdog_script_exists() -> None:
    """scripts/watchdog.sh must exist and be executable.

    The watchdog is the self-healing layer that restarts the agent within 30 s
    of death.  If the file is missing or not executable the agent can go dark
    overnight without any automatic recovery.
    """
    watchdog = _ROOT / "scripts" / "watchdog.sh"
    assert watchdog.exists(), (
        f"scripts/watchdog.sh does not exist at {watchdog}; "
        "the self-healing watchdog is missing — agent cannot auto-restart."
    )
    assert os.access(watchdog, os.X_OK), (
        "scripts/watchdog.sh exists but is NOT executable; "
        "run: chmod +x scripts/watchdog.sh"
    )


# ---------------------------------------------------------------------------
# 11. Trading loops auto-start on launch (no Start button)
# ---------------------------------------------------------------------------


def test_trading_loops_auto_start_on_launch() -> None:
    """main.py must auto-start trading via start_trading() in the API startup hook.

    Requiring a manual dashboard Start after every launch was a major cause of
    zero-trade overnight sessions when the agent restarted without user present.
    """
    source = _MAIN_PY.read_text(encoding="utf-8")
    assert "register_api_startup(_start_live_engines)" in source
    assert "start_trading()" in source


# ---------------------------------------------------------------------------
# 12. /api/health operational endpoint
# ---------------------------------------------------------------------------


def test_api_health_endpoint_defined() -> None:
    """routes.py must expose GET /api/health with trading loop + watchdog fields."""
    routes = (_SRC / "api" / "routes.py").read_text(encoding="utf-8")
    health = (_SRC / "api" / "agent_health.py").read_text(encoding="utf-8")
    assert '@router.get("/api/health")' in routes
    assert "build_health_status" in routes
    for field in (
        "agent_alive",
        "trading_healthy",
        "trading_loops_running",
        "port_bound",
        "watchdog_active",
        "quotes_fresh",
        "issues",
        "last_log_age_sec",
        "markets",
        "last_gate_check_age_sec",
    ):
        assert field in health
    assert "last_gate_check" in health


def test_heartbeat_no_auto_shutdown() -> None:
    """_start_heartbeat_monitor must be a no-op — browser disconnect must not kill agent."""
    routes = (_SRC / "api" / "routes.py").read_text(encoding="utf-8")
    assert "auto-shutdown on browser disconnect is disabled" in routes

    lines = routes.splitlines()
    in_func = False
    body: list[str] = []
    for line in lines:
        if line.startswith("def _start_heartbeat_monitor("):
            in_func = True
            continue
        if in_func:
            if (
                line
                and not line[0].isspace()
                and (line.startswith("def ") or line.startswith("class "))
            ):
                break
            body.append(line)
    func_body = "\n".join(body)
    assert "threading.Thread" not in func_body
    assert "os.kill" not in func_body


# ---------------------------------------------------------------------------
# 13. Watchdog contains restart cap logic
# ---------------------------------------------------------------------------


def test_watchdog_has_restart_cap() -> None:
    """watchdog.sh must contain a restart-cap check (MAX_RESTARTS_PER_HOUR).

    Without a cap the watchdog can trigger an infinite restart storm when the
    agent has a persistent startup failure, filling disk with logs and
    hammering the IG API until the account is rate-limited or suspended.
    The cap must be present so the watchdog self-terminates after detecting
    a fundamental breakage.
    """
    watchdog = _ROOT / "scripts" / "watchdog.sh"
    if not watchdog.exists():
        pytest.skip("watchdog.sh not found — skipping cap check")

    source = watchdog.read_text(encoding="utf-8")

    assert "MAX_RESTARTS_PER_HOUR" in source, (
        "watchdog.sh does not define MAX_RESTARTS_PER_HOUR; "
        "restart storm protection is missing."
    )
    assert "FATAL" in source or "restart storm" in source or "STOPPING" in source, (
        "watchdog.sh does not log a FATAL/stop message when the cap is hit; "
        "operators will not know the watchdog gave up."
    )


def test_watchdog_detects_trading_zombie() -> None:
    """watchdog.sh must restart when /api/health reports trading unhealthy."""
    watchdog = _ROOT / "scripts" / "watchdog.sh"
    if not watchdog.exists():
        pytest.skip("watchdog.sh not found")
    source = watchdog.read_text(encoding="utf-8")
    assert "trading_healthy" in source
    assert "/api/health" in source
    assert "watchdog.pid" in source


def test_trading_health_monitor_exists() -> None:
    """Background monitor must detect zombie trading and alert via Telegram."""
    path = _SRC / "system" / "trading_health_monitor.py"
    assert path.is_file()
    source = path.read_text(encoding="utf-8")
    assert "start_trading_health_monitor" in source
    assert "trading_healthy" in source
    main = _MAIN_PY.read_text(encoding="utf-8")
    assert "start_trading_health_monitor" in main


def test_safe_to_leave_script_exists() -> None:
    """scripts/safe_to_leave.py must exist — operator trust gate before going away."""
    script = _ROOT / "scripts" / "safe_to_leave.py"
    assert script.is_file(), "safe_to_leave.py missing"
    source = script.read_text(encoding="utf-8")
    assert "SAFE TO LEAVE" in source
    assert "trading_healthy" in source or "/api/health" in source


def test_shutdown_cleanup_module_covers_full_teardown() -> None:
    """Stop Agent must tear down streams, IG session, watchdog, orphans, lock, and port."""
    path = _SRC / "system" / "shutdown_cleanup.py"
    assert path.is_file(), "shutdown_cleanup.py missing"
    source = path.read_text(encoding="utf-8")
    for needle in (
        "perform_shutdown_cleanup",
        "stop_market_stream",
        "stop_ig_position_sync",
        "shutdown_shared_ig_session",
        "stop_watchdog",
        "kill_other_agent_processes",
        "release_instance_lock",
        "_force_cleanup_port",
        "agent_fully_started",
        "agent_fully_stopped",
    ):
        assert needle in source, f"shutdown_cleanup missing {needle}"


def test_api_shutdown_delegates_to_shutdown_cleanup() -> None:
    """POST /api/shutdown must run centralized cleanup before process exit."""
    routes = (_SRC / "api" / "routes.py").read_text(encoding="utf-8")
    assert "from fastapi import APIRouter, BackgroundTasks, HTTPException" in routes
    assert "perform_shutdown_cleanup" in routes
    assert "shutdown: initiated via dashboard Stop button" in routes
    assert "127.0.0.1:8081/shutdown-verify" in routes


def test_confirm_stopped_script_exists() -> None:
    """scripts/confirm_stopped.py verifies Stop Agent left no rogue processes."""
    script = _ROOT / "scripts" / "confirm_stopped.py"
    assert script.is_file(), "confirm_stopped.py missing"


def test_shutdown_verify_server_and_dashboard_integration() -> None:
    """Dashboard Stop Agent must run post-exit verification like confirm_stopped."""
    verify = _ROOT / "scripts" / "shutdown_verify_server.py"
    cleanup = _SRC / "system" / "shutdown_cleanup.py"
    routes = _SRC / "api" / "routes.py"
    app = _ROOT / "dashboard" / "src" / "App.jsx"
    assert verify.is_file()
    cleanup_src = cleanup.read_text(encoding="utf-8")
    assert "spawn_post_shutdown_verifier" in cleanup_src
    assert "mark_manual_stop" in cleanup_src
    assert "manual_stop_active" in (_ROOT / "scripts" / "watchdog.sh").read_text(
        encoding="utf-8"
    )
    assert "post_cleanup_shutdown_checks" in cleanup.read_text(encoding="utf-8")
    routes_src = routes.read_text(encoding="utf-8")
    assert "verify_poll_url" in routes_src
    assert "skip_port_cleanup=True" in routes_src
    assert "BackgroundTasks" in routes_src
    main_src = _MAIN_PY.read_text(encoding="utf-8")
    assert "-sTCP:LISTEN" in main_src
    verify_src = verify.read_text(encoding="utf-8")
    assert '("0.0.0.0", VERIFY_PORT)' in verify_src
    app_src = app.read_text(encoding="utf-8")
    assert "pollShutdownVerify" in app_src
    assert "127.0.0.1:8081/shutdown-verify" in app_src
    assert "Fully stopped — verified" in app_src
    confirm = (_ROOT / "scripts" / "confirm_stopped.py").read_text(encoding="utf-8")
    assert "agent_fully_stopped" in confirm


def test_confirm_started_script_exists() -> None:
    """scripts/confirm_started.py verifies startup reached trading-ready state."""
    script = _ROOT / "scripts" / "confirm_started.py"
    assert script.is_file(), "confirm_started.py missing"
    source = script.read_text(encoding="utf-8")
    assert "CONFIRM STARTED" in source
    assert "agent_fully_started" in source
    for needle in (
        "trading_healthy",
        "stream_ready",
        "watchdog",
        "gate",
    ):
        assert needle in source.lower(), f"confirm_started missing {needle} check"


def test_startup_cleanup_symmetry_with_shutdown() -> None:
    """Startup cleanup must mirror shutdown: kill orphans, free port, clear lock."""
    main = _MAIN_PY.read_text(encoding="utf-8")
    shutdown = (_SRC / "system" / "shutdown_cleanup.py").read_text(encoding="utf-8")

    assert "kill_other_agent_processes" in main
    assert "_force_cleanup_port" in main
    assert "agent_fully_started" in shutdown
    assert "agent_fully_stopped" in shutdown
    for needle in (
        "kill_other_agent_processes",
        "perform_shutdown_cleanup",
        "agent_fully_started",
        "agent_fully_stopped",
    ):
        assert needle in shutdown, f"shutdown_cleanup missing {needle}"


def test_pre_startup_cleanup_kills_duplicate_processes() -> None:
    """_pre_startup_cleanup must SIGTERM duplicate main.py before acquiring lock."""
    source = _MAIN_PY.read_text(encoding="utf-8")
    lines = source.splitlines()
    in_func = False
    body: list[str] = []
    for line in lines:
        if line.startswith("def _pre_startup_cleanup("):
            in_func = True
            continue
        if in_func:
            if (
                line
                and not line[0].isspace()
                and (line.startswith("def ") or line.startswith("class "))
            ):
                break
            body.append(line)
    func_body = "\n".join(body)
    assert "kill_other_agent_processes" in func_body
    assert "_force_cleanup_port" in func_body


def test_ensure_watchdog_called_after_preflight() -> None:
    """Manual launches must start watchdog when launcher did not."""
    source = _MAIN_PY.read_text(encoding="utf-8")
    assert "def _ensure_watchdog_running(" in source
    run_idx = source.index("def run(self)")
    run_body = source[run_idx : run_idx + 2500]
    assert "_ensure_watchdog_running()" in run_body
    assert "run_preflight()" in run_body
    assert run_body.index("run_preflight()") < run_body.index(
        "_ensure_watchdog_running()"
    )


def test_agent_fully_started_detects_duplicate_processes() -> None:
    """agent_fully_started must fail when more than one main.py is running."""
    from system.shutdown_cleanup import agent_fully_started

    with (
        patch(
            "system.shutdown_cleanup._list_main_py_pids",
            return_value=[111, 222],
        ),
        patch("system.shutdown_cleanup._port_bound", return_value=True),
        patch(
            "system.shutdown_cleanup._instance_lock_holder_pid",
            return_value=111,
        ),
        patch("api.agent_health._watchdog_active", return_value=True),
        patch(
            "system.shutdown_cleanup._fetch_api_health",
            return_value={
                "trading_loops_running": True,
                "trading_healthy": True,
                "last_gate_check_age_sec": 5.0,
            },
        ),
        patch(
            "system.pre_flight_checks.check_startup_stream_gate_log",
            return_value=MagicMock(passed=True),
        ),
    ):
        ok, issues = agent_fully_started()
    assert not ok
    assert any("duplicate main.py" in i for i in issues)


def test_agent_fully_started_all_clear() -> None:
    """agent_fully_started passes when process, health, lock, and stream gate are OK."""
    from system.shutdown_cleanup import agent_fully_started

    with (
        patch("system.shutdown_cleanup._list_main_py_pids", return_value=[4242]),
        patch("system.shutdown_cleanup._port_bound", return_value=True),
        patch(
            "system.shutdown_cleanup._instance_lock_holder_pid",
            return_value=4242,
        ),
        patch("api.agent_health._watchdog_active", return_value=True),
        patch(
            "system.shutdown_cleanup._fetch_api_health",
            return_value={
                "trading_loops_running": True,
                "trading_healthy": True,
                "last_gate_check_age_sec": 12.0,
            },
        ),
        patch(
            "system.pre_flight_checks.check_startup_stream_gate_log",
            return_value=MagicMock(passed=True),
        ),
    ):
        ok, issues = agent_fully_started()
    assert ok, issues
    assert issues == []


def test_agent_fully_started_requires_stream_ready_log() -> None:
    """Missing stream_ready in engine.log must fail startup confirmation."""
    from system.shutdown_cleanup import agent_fully_started

    with (
        patch("system.shutdown_cleanup._list_main_py_pids", return_value=[4242]),
        patch("system.shutdown_cleanup._port_bound", return_value=True),
        patch(
            "system.shutdown_cleanup._instance_lock_holder_pid",
            return_value=4242,
        ),
        patch("api.agent_health._watchdog_active", return_value=True),
        patch(
            "system.shutdown_cleanup._fetch_api_health",
            return_value={
                "trading_loops_running": True,
                "trading_healthy": True,
                "last_gate_check_age_sec": 12.0,
            },
        ),
        patch(
            "system.pre_flight_checks.check_startup_stream_gate_log",
            return_value=MagicMock(passed=False),
        ),
    ):
        ok, issues = agent_fully_started()
    assert not ok
    assert any("stream_ready" in i for i in issues)


def test_confirm_started_script_exists() -> None:
    """scripts/confirm_started.py verifies desktop launch completed successfully."""
    script = _ROOT / "scripts" / "confirm_started.py"
    assert script.is_file(), "confirm_started.py missing"
    source = script.read_text(encoding="utf-8")
    assert "CONFIRM STARTED" in source
    assert "agent_fully_started" in source


def test_startup_shutdown_symmetry() -> None:
    """Startup and shutdown checks must mirror process, lock, port, and watchdog."""
    path = _SRC / "system" / "shutdown_cleanup.py"
    source = path.read_text(encoding="utf-8")
    assert "agent_fully_stopped" in source
    assert "agent_fully_started" in source
    for needle in (
        "main.py",
        "watchdog",
        "8080",
        ".ig_agent_v25.lock",
    ):
        assert needle in source


def test_ig_rest_client_has_end_session() -> None:
    """IG REST client must support DELETE /session on graceful shutdown."""
    source = (_SRC / "ig_api" / "rest_client.py").read_text(encoding="utf-8")
    assert "def end_session" in source
    assert 'DELETE", "/session"' in source or 'DELETE", "/session"' in source


def test_watchdog_uses_dynamic_agent_dir_and_grace() -> None:
    """watchdog.sh must not hardcode Desktop path; must grace startup and use launcher start script."""
    watchdog = _ROOT / "scripts" / "watchdog.sh"
    source = watchdog.read_text(encoding="utf-8")
    assert 'SCRIPT_DIR="$(cd "$(dirname "$0")"' in source
    assert "start_agent_background.sh" in source
    assert "STARTUP_GRACE_SEC" in source
    assert "/Users/chrisgordon/Desktop/IG_Agent_v25" not in source


def test_start_agent_background_script_exists() -> None:
    script = _ROOT / "scripts" / "start_agent_background.sh"
    assert script.is_file()
    assert os.access(script, os.X_OK)
    source = script.read_text(encoding="utf-8")
    assert "caffeinate" in source
    assert "IG_AGENT_FROM_LAUNCHER=1" in source


def test_evaluate_trading_health_closed_markets_skip_quotes() -> None:
    """Stale quotes must not fail health when no markets are open."""
    from unittest.mock import patch

    from api.agent_health import evaluate_trading_health

    with patch("api.agent_health._markets_open_count", return_value=0):
        health = evaluate_trading_health(
            loops_running=True,
            paused=False,
            gate_age=8.0,
            epics=["CS.D.CFPGOLD.CFP.IP"],
            quote_fresh={"CS.D.CFPGOLD.CFP.IP": False},
        )
    assert health["trading_healthy"] is True
    assert health["quotes_required_for_health"] is False
    assert not any("quotes_stale" in i for i in health["issues"])


def test_watchdog_restart_skips_deploy_check() -> None:
    """Watchdog restarts must not re-run pytest — burns restart budget and delays recovery."""
    main = _MAIN_PY.read_text(encoding="utf-8")
    assert "IG_AGENT_SKIP_DEPLOY_CHECK" in main
    start = (_ROOT / "scripts" / "start_agent_background.sh").read_text(
        encoding="utf-8"
    )
    assert "IG_AGENT_SKIP_DEPLOY_CHECK=1" in start


def test_watchdog_launchd_keeper_plist() -> None:
    """launchd must KeepAlive the watchdog so restart-storm FATAL is not permanent."""
    plist = _ROOT / "scripts" / "com.igagent.v25.watchdog.plist"
    assert plist.is_file()
    source = plist.read_text(encoding="utf-8")
    assert "<key>KeepAlive</key>" in source
    assert "<true/>" in source
    assert "watchdog.sh" in source
    install = (_ROOT / "scripts" / "install_launchd.sh").read_text(encoding="utf-8")
    assert "com.igagent.v25.watchdog.plist" in install


def test_dashboard_shows_agent_offline_banner() -> None:
    app = (_ROOT / "dashboard" / "src" / "App.jsx").read_text(encoding="utf-8")
    header = (_ROOT / "dashboard" / "src" / "components" / "Header.jsx").read_text(
        encoding="utf-8"
    )
    assert "/api/health" in app
    assert "agentAlive" in app
    assert "AGENT OFFLINE" in header


def test_dashboard_mutes_alarm_when_agent_dead() -> None:
    """Stale trading_paused/STOP must not beep after intentional shutdown or crash."""
    app = (_ROOT / "dashboard" / "src" / "App.jsx").read_text(encoding="utf-8")
    assert "!agentAlive" in app
    assert 'shutdownState !== "idle" || !agentAlive' in app


def test_dashboard_persists_deliberate_stop_in_session() -> None:
    """Stop Agent must survive tab refresh via sessionStorage (10 min TTL)."""
    app = (_ROOT / "dashboard" / "src" / "App.jsx").read_text(encoding="utf-8")
    assert "ig_agent_deliberate_stop_ts" in app
    assert "markDeliberateStop" in app
    assert "clearDeliberateStop" in app
    assert "isRecentDeliberateStop" in app
    assert "600_000" in app
    assert "markDeliberateStop();" in app


def test_dashboard_recovers_stopped_screen_on_stale_tab() -> None:
    """Stale tabs must auto-show the stopped screen after deliberate shutdown."""
    app = (_ROOT / "dashboard" / "src" / "App.jsx").read_text(encoding="utf-8")
    assert "recoverStoppedScreen" in app
    assert 'data?.status === "done" && data?.ok === true' in app
    assert "recovered_from_session" in app


def test_dashboard_offline_overlay_when_agent_down() -> None:
    """Crash or stale tab without deliberate stop shows calm offline message — no alarm."""
    app = (_ROOT / "dashboard" / "src" / "App.jsx").read_text(encoding="utf-8")
    assert "Agent is not running" in app
    assert "relaunch from the desktop icon" in app
    assert "agentOfflineChecked" in app


def test_shutdown_verify_server_serves_long_after_done() -> None:
    """Verifier must answer stale-tab polls for manual_stop TTL (600s)."""
    verify = (_ROOT / "scripts" / "shutdown_verify_server.py").read_text(
        encoding="utf-8"
    )
    assert "serve_deadline[0] = time.monotonic() + 600.0" in verify
    assert "thread.join(timeout=605.0)" in verify


def test_safe_to_leave_runtime_heartbeat_check() -> None:
    """safe_to_leave must verify heartbeat monitor at runtime, not source string match."""
    source = (_ROOT / "scripts" / "safe_to_leave.py").read_text(encoding="utf-8")
    assert "_heartbeat_auto_shutdown_disabled" in source
    assert "inspect.getsource" in source
    assert "_start_heartbeat_monitor" in source
    assert "_heartbeat_disabled" not in source


def test_safe_to_leave_requires_telegram() -> None:
    """Overnight trust gate must fail without Telegram — no WARN-only path."""
    source = (_ROOT / "scripts" / "safe_to_leave.py").read_text(encoding="utf-8")
    assert "Telegram alerts configured" in source
    assert '"WARN"' not in source
    assert "--require-telegram" not in source


def test_dashboard_safe_to_leave_button_and_api() -> None:
    header = (_ROOT / "dashboard" / "src" / "components" / "Header.jsx").read_text(
        encoding="utf-8"
    )
    routes = (_ROOT / "src" / "api" / "routes.py").read_text(encoding="utf-8")
    data = (_ROOT / "src" / "api" / "dashboard_data.py").read_text(encoding="utf-8")
    assert "Safe to Leave" in header
    assert "/api/safe-to-leave" in header
    assert "api_safe_to_leave" in routes
    assert "run_safe_to_leave" in routes
    assert "def run_safe_to_leave" in data


def test_safe_to_leave_skips_tick_freshness_when_markets_closed() -> None:
    """Hub tick freshness (7.4) must not fail safe_to_leave when no market is open."""
    source = (_ROOT / "scripts" / "safe_to_leave.py").read_text(encoding="utf-8")
    assert '"7.4"' in source
    assert "markets_open_count" in source
    assert "no markets open" in source


def test_agent_bootstrap_skips_deploy_self_test_on_watchdog_restart() -> None:
    source = (_ROOT / "src" / "runtime" / "agent_bootstrap.py").read_text(
        encoding="utf-8"
    )
    assert "IG_AGENT_SKIP_DEPLOY_CHECK" in source
    assert "test_deployed_fixes.py" in source


def test_install_launchd_substitutes_python_bin() -> None:
    install = (_ROOT / "scripts" / "install_launchd.sh").read_text(encoding="utf-8")
    agent_plist = (_ROOT / "scripts" / "com.igagent.v25.plist").read_text(
        encoding="utf-8"
    )
    assert "__PYTHON_BIN__" in agent_plist
    assert "__PYTHON_BIN__" in install
    assert "SuccessfulExit" in agent_plist


def test_correlation_guard_persists_state() -> None:
    source = _GUARD_PY.read_text(encoding="utf-8")
    assert "correlation_guard.json" in source
    assert "_persist_state" in source
    assert "_load_state" in source


def test_evaluate_trading_health_maintenance_quotes_exempt() -> None:
    from unittest.mock import patch

    from api.agent_health import evaluate_trading_health

    epic = "CS.D.CFJPY.CFJ.IP"
    with (
        patch("api.agent_health._markets_open_count", return_value=1),
        patch("api.agent_health._epic_quote_exempt", return_value=True),
    ):
        health = evaluate_trading_health(
            loops_running=True,
            paused=False,
            gate_age=8.0,
            epics=[epic],
            quote_fresh={epic: False},
        )
    assert health["trading_healthy"] is True
    assert health["quotes_fresh"] is True
    assert not any(i.startswith("quotes_stale:") for i in health["issues"])


def test_main_respawns_watchdog_when_silent() -> None:
    """Manual launches must start watchdog if it was stopped."""
    source = _MAIN_PY.read_text(encoding="utf-8")
    assert "_ensure_watchdog_running" in source
    assert "watchdog.sh" in source
    assert "_watchdog_active" in source
