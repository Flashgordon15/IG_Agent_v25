"""Desktop productization — splash, anti-zombie guard, native shell validation."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_guard():
    from cockpit.desktop_process_guard import reset_desktop_process_guard_for_tests

    reset_desktop_process_guard_for_tests()
    yield
    reset_desktop_process_guard_for_tests()


def test_splash_html_nine_stage_checklist():
    from cockpit.desktop_splash_assets import LAUNCHER_STAGES, build_splash_html

    html = build_splash_html()
    assert len(LAUNCHER_STAGES) == 9
    assert "Iron Cage — Flight Deck Control Center" in html
    assert "stage-checklist" in html
    assert "shell-exit-btn" in html
    assert "boot-terminal" in html
    assert "#0D0E12" in html
    assert "#10B981" in html
    assert "#F59E0B" in html
    for row in LAUNCHER_STAGES:
        assert row["id"] in html


def test_launcher_stage_index_maps_nine_steps():
    from cockpit.desktop_splash_assets import launcher_stage_index

    assert launcher_stage_index("shutdown") == 0
    assert launcher_stage_index("tests") == 2
    assert launcher_stage_index("verify") == 7
    assert launcher_stage_index("ready") == 8
    assert launcher_stage_index("unknown", step=5) == 4


def test_orchestrator_segment_states_active_and_warming():
    from cockpit.desktop_splash_assets import orchestrator_segment_states

    states = orchestrator_segment_states(
        {
            "STAGE_1_CONFIG_SANITY": "SUCCESS",
            "STAGE_2_GUARDIAN_WAKE": "WARMING_HEALTHY",
            "STAGE_3_REGIME_HYDRATION": "PENDING",
        }
    )
    assert states[0] == "active"
    assert states[1] == "warming"
    assert states[2] == "pending"


def test_anti_zombie_kills_registered_children():
    from cockpit.desktop_process_guard import (
        execute_anti_zombie_shutdown,
        register_child_pid,
    )

    register_child_pid(4242)
    killed: list[tuple[int, int]] = []

    def _fake_kill(pid: int, sig: int) -> None:
        killed.append((pid, sig))

    with patch("cockpit.desktop_process_guard.os.kill", side_effect=_fake_kill), patch(
        "cockpit.desktop_process_guard._pids_on_port", return_value=[]
    ), patch("cockpit.desktop_process_guard._kill_pattern", return_value=[]), patch(
        "system.shutdown_cleanup.mark_manual_stop"
    ):
        summary = execute_anti_zombie_shutdown(skip_mark_manual_stop=True)

    assert summary.get("ok") is True
    assert (4242, signal.SIGKILL) in killed


def test_anti_zombie_clears_ports_8080_and_8787():
    from cockpit.desktop_process_guard import execute_anti_zombie_shutdown

    port_map = {8080: [111, 222], 8787: [333]}

    def _fake_pids(port: int) -> list[int]:
        return list(port_map.get(port, []))

    kills: list[tuple[int, int]] = []

    def _fake_kill(pid: int, sig: int) -> None:
        kills.append((pid, sig))

    with patch("cockpit.desktop_process_guard._pids_on_port", side_effect=_fake_pids), patch(
        "cockpit.desktop_process_guard.os.kill", side_effect=_fake_kill
    ), patch("cockpit.desktop_process_guard._kill_pattern", return_value=[]), patch(
        "cockpit.desktop_process_guard.time.sleep"
    ):
        summary = execute_anti_zombie_shutdown(skip_mark_manual_stop=True)

    assert 111 in summary["ports_cleared"]["8080"]["killed"]
    assert 333 in summary["ports_cleared"]["8787"]["killed"]
    assert any(sig == signal.SIGKILL for pid, sig in kills if pid in (111, 222, 333))


def test_window_close_hook_invokes_anti_zombie():
    from cockpit import desktop_app_shell as shell

    with patch.object(shell, "execute_sequential_desktop_teardown", return_value={"ok": True}) as mock_shutdown:
        assert shell._on_closing() is True
        mock_shutdown.assert_called_once()


def test_graceful_exit_requests_window_destroy():
    from cockpit import desktop_app_shell as shell

    mock_win = MagicMock()
    with patch.object(shell, "_WINDOW", mock_win), patch.object(
        shell, "_append_terminal"
    ), patch.object(shell, "log_engine"):
        shell._request_graceful_exit(source="test")
        time.sleep(0.15)
    mock_win.destroy.assert_called_once()


def test_desktop_shell_api_graceful_exit():
    from cockpit import desktop_app_shell as shell

    with patch.object(shell, "_request_graceful_exit") as mock_exit:
        api = shell.DesktopShellApi()
        assert api.graceful_exit() == "exiting"
        mock_exit.assert_called_once_with(source="pywebview_api")


def test_desktop_shell_smoke_test_cli():
    from cockpit.desktop_app_shell import run_smoke_test

    assert run_smoke_test() == 0


def test_read_launcher_status_json(tmp_path: Path):
    from cockpit import desktop_app_shell as shell

    logs = tmp_path / "logs"
    logs.mkdir()
    status = {
        "stage": "tests",
        "step": 3,
        "detail": "Running smoke tests",
        "boot_tier": "amber",
    }
    (logs / "launcher_status.json").write_text(json.dumps(status), encoding="utf-8")
    with patch.object(shell, "_project_root", return_value=tmp_path):
        data = shell._read_launcher_status()
    assert data is not None
    assert data["stage"] == "tests"


def test_spawn_launcher_registers_child_process(tmp_path: Path):
    from cockpit import desktop_app_shell as shell
    from cockpit.desktop_process_guard import registered_child_pids

    script = tmp_path / "macos" / "launcher" / "igagent_launcher.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)

    proc = MagicMock()
    proc.pid = 98765
    proc.stdout = iter([])

    with patch.object(shell, "_project_root", return_value=tmp_path), patch(
        "cockpit.desktop_app_shell.subprocess.Popen", return_value=proc
    ):
        shell._spawn_launcher_supervisor()

    assert 98765 in registered_child_pids()


def test_launcher_core_open_desktop_flight_deck():
    from macos.launcher.launcher_core import open_desktop_flight_deck

    root = Path(__file__).resolve().parents[1]
    with patch("macos.launcher.launcher_core.subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.pid = 24680
        mock_popen.return_value = mock_proc
        pid = open_desktop_flight_deck(root, launch_supervisor=False)
        mock_popen.assert_called_once()
        assert pid == 24680
        args = mock_popen.call_args[0][0]
        assert "cockpit.desktop_app_shell" in " ".join(str(a) for a in args)


def test_launcher_core_desktop_mode_blocks_browser_open():
    from macos.launcher import launcher_core

    root = Path(__file__).resolve().parents[1]
    opened: list[str] = []

    with patch.dict(os.environ, {"LAUNCHER_DESKTOP": "1"}, clear=False), patch.object(
        launcher_core, "open_desktop_flight_deck", return_value=12345
    ) as mock_desktop:
        launcher_core.open_dashboard(root, open_fn=lambda u: opened.append(u))
        mock_desktop.assert_called_once()
        assert opened == []


def test_cold_start_port_collision_detect_purge_bypass():
    """Orphan listener on :8080 must be detected and purged before shell boot."""
    import sys
    import time

    from cockpit.desktop_process_guard import audit_and_purge_bound_ports, port_is_bound

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import socket,time; s=socket.socket(); "
            "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); "
            "s.bind(('127.0.0.1', 8080)); s.listen(1); time.sleep(120)",
        ],
        start_new_session=True,
    )
    try:
        time.sleep(0.25)
        if not port_is_bound(8080):
            pytest.skip("could not establish orphan listener on 8080 (port busy)")

        summary = audit_and_purge_bound_ports(preserve_pid=os.getpid())
        killed = summary["ports"]["8080"]["killed"]
        assert holder.pid in killed or holder.pid in summary["purged_pids"]

        time.sleep(0.25)
        assert holder.poll() is not None or not port_is_bound(8080)
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=3)


def test_port_is_bound_socket_probe():
    import socket
    import time

    from cockpit.desktop_process_guard import port_is_bound

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", 18787))
        sock.listen(1)
        assert port_is_bound(18787) is True
    finally:
        sock.close()
        time.sleep(0.05)
        assert port_is_bound(18787) is False


def test_micro_health_ping_retries_then_warming_fallback():
    from cockpit import desktop_app_shell as shell

    calls = {"n": 0}

    def _flaky_fetch(url: str):
        calls["n"] += 1
        if calls["n"] <= 5:
            raise ConnectionRefusedError("port collision")
        return {"ok": True}

    with patch.object(shell, "_fetch_json", side_effect=_flaky_fetch), patch(
        "cockpit.desktop_app_shell.time.sleep"
    ), patch.object(shell, "_evaluate"), patch.object(shell, "_append_terminal"):
        outcome = shell._micro_health_ping_with_retry()

    assert outcome["ok"] is True
    assert outcome["tier"] == "LIVE"
    assert outcome["attempts"] == 2


def test_micro_health_ping_exhaust_retries_returns_warming():
    from cockpit import desktop_app_shell as shell

    with patch.object(shell, "_fetch_json", return_value=None), patch(
        "cockpit.desktop_app_shell.time.sleep"
    ), patch.object(shell, "_evaluate") as mock_eval, patch.object(shell, "_append_terminal"), patch(
        "cockpit.desktop_process_guard.port_is_bound", return_value=False
    ):
        outcome = shell._micro_health_ping_with_retry()

    assert outcome["ok"] is False
    assert outcome["tier"] == "WARMING"
    assert outcome["attempts"] == shell._HEALTH_RETRY_MAX
    mock_eval.assert_called()


def test_micro_health_port_bound_marks_hydrating():
    from cockpit import desktop_app_shell as shell

    with patch.object(shell, "_fetch_json", return_value=None), patch(
        "cockpit.desktop_app_shell.time.sleep"
    ), patch.object(shell, "_evaluate"), patch.object(shell, "_append_terminal"), patch(
        "cockpit.desktop_process_guard.port_is_bound", return_value=True
    ):
        outcome = shell._micro_health_ping_with_retry()

    assert outcome["ok"] is False
    assert outcome["port_bound"] is True
    assert outcome["tier"] == "HYDRATING"


def test_api_ready_for_cockpit_accepts_port_bound_health():
    from cockpit import desktop_app_shell as shell

    assert shell._api_ready_for_cockpit({"port_bound": True, "ok": False}) is True


def test_cockpit_coupled_when_trade_ready_without_gate_rows():
    from cockpit import desktop_app_shell as shell

    trade_ready = {"trade_ready": True, "gates": None}

    with patch.object(shell, "_fetch_json", side_effect=[trade_ready, trade_ready]):
        assert shell._cockpit_coupled_to_agent() is True


def test_cockpit_coupled_when_agent_trade_ready_local_gates_pending():
    from cockpit import desktop_app_shell as shell

    local = {"trade_ready": False, "gates": {"G1": {"status": "pending"}}}
    agent = {"trade_ready": True, "gates": None}

    with patch.object(shell, "_fetch_json", side_effect=[local, agent]):
        assert shell._cockpit_coupled_to_agent() is True


def test_handoff_contract_satisfied_when_launcher_ready():
    from cockpit import desktop_app_shell as shell

    status = {"stage": "ready", "step": 9}
    health = {"ok": True, "routes": {"health": True}}
    assert shell._handoff_contract_satisfied(status=status, health=health, orch=None) is True


def test_try_embed_cockpit_bypasses_coupling_when_contract_forced():
    from cockpit import desktop_app_shell as shell

    with patch.object(shell, "_ensure_cockpit_web_server", return_value=True), patch.object(
        shell, "_url_alive", return_value=True
    ), patch.object(shell, "_cockpit_coupled_to_agent", return_value=False), patch.object(
        shell, "_transition_to_cockpit"
    ) as mock_transition:
        ok = shell._try_embed_cockpit(
            "http://127.0.0.1:8787/",
            force=True,
            reason="launcher_contract",
        )

    assert ok is True
    mock_transition.assert_called_once()


def test_try_embed_cockpit_blocked_without_force_when_decoupled():
    from cockpit import desktop_app_shell as shell

    with patch.object(shell, "_ensure_cockpit_web_server", return_value=True), patch.object(
        shell, "_url_alive", return_value=True
    ), patch.object(shell, "_cockpit_coupled_to_agent", return_value=False), patch.object(
        shell, "_transition_to_cockpit"
    ) as mock_transition:
        ok = shell._try_embed_cockpit("http://127.0.0.1:8787/", force=False)

    assert ok is False
    mock_transition.assert_not_called()


def test_boot_acceptance_tier_only_tcp_fallback():
    import socket
    import subprocess
    import threading

    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "boot_acceptance.sh"

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 18080))
    sock.listen(5)
    stop = threading.Event()

    def _accept_loop() -> None:
        while not stop.is_set():
            try:
                sock.settimeout(0.2)
                conn, _ = sock.accept()
                conn.close()
            except OSError:
                continue

    threading.Thread(target=_accept_loop, daemon=True).start()
    try:
        env = {**os.environ, "IG_API_PORT": "18080", "PYTHONPATH": str(root / "src")}
        proc = subprocess.run(
            ["/bin/bash", str(script), "--tier-only"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(root),
        )
        assert proc.stdout.strip() == "amber", proc.stderr
    finally:
        stop.set()
        sock.close()


def test_run_gui_cold_start_port_audit_before_window():
    from cockpit import desktop_app_shell as shell

    with patch.object(shell, "audit_and_purge_bound_ports", return_value={"purged_pids": [999]}) as mock_audit, patch(
        "webview.create_window"
    ), patch("webview.start"), patch.object(shell, "_spawn_launcher_supervisor"):
        shell.run_gui(launch_supervisor=True)
        mock_audit.assert_called_once()


def test_agent_gui_desktop_mode_exits_without_browser_launch():
    root = Path(__file__).resolve().parents[1]
    script = root / "macos" / "launcher" / "agent_gui.sh"
    env = {
        **os.environ,
        "LAUNCHER_DESKTOP": "1",
        "IG_DESKTOP_SHELL_ACTIVE": "1",
        "IG_AGENT_ROOT": str(root),
    }
    result = subprocess.run(
        ["/bin/bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(root),
    )
    assert result.returncode == 0
    combined = (result.stdout or "") + (result.stderr or "")
    assert "open http" not in combined.lower()


def test_install_igagent_app_launches_flight_deck_shell():
    root = Path(__file__).resolve().parents[1]
    script = root / "macos" / "install_igagent_app.sh"
    text = script.read_text(encoding="utf-8")
    assert "desktop_flight_deck.sh" in text
    assert "cockpit.desktop_app_shell" in text or "desktop_flight_deck.sh" in text
    assert "CFBundleDisplayName" in text
    assert "Iron Cage Flight Deck" in text


def test_setup_desktop_shortcut_targets_flight_deck():
    root = Path(__file__).resolve().parents[1]
    script = root / "macos" / "setup_desktop_shortcut.sh"
    text = script.read_text(encoding="utf-8")
    assert "Iron Cage Flight Deck.app" in text
    assert "install_igagent_app.sh" in text


def test_desktop_flight_deck_script_runs_port_audit():
    root = Path(__file__).resolve().parents[1]
    script = root / "macos" / "launcher" / "desktop_flight_deck.sh"
    text = script.read_text(encoding="utf-8")
    assert "audit_and_purge_bound_ports" in text
    assert "IG_DESKTOP_SHELL_ACTIVE=1" in text


def test_cache_bust_cockpit_url_includes_monotonic_tokens():
    from cockpit import desktop_app_shell as shell

    url = shell._cache_bust_cockpit_url("http://127.0.0.1:8787/")
    assert "v=" in url
    assert "_cb=" in url
    assert "t=" in url
    assert url.startswith("http://127.0.0.1:8787/")


def test_read_cache_busted_cockpit_html_injects_build_nonce():
    from cockpit import desktop_app_shell as shell

    root = Path(__file__).resolve().parents[1]
    html = shell._read_cache_busted_cockpit_html(base_url="http://127.0.0.1:8787/")
    assert html is not None
    assert "window.__COCKPIT_BUILD__ =" in html
    assert "window.__COCKPIT_CACHE_NONCE__" in html
    assert "_cb=" in html
    assert "Date.now()" not in html.split("window.__COCKPIT_BUILD__")[1][:80]


def test_stage5_launch_ready_from_orchestrator_tokens():
    from cockpit import desktop_app_shell as shell

    orch = {"stage_tokens": {"STAGE_5_LAUNCH": "SUCCESS"}}
    assert shell._stage5_launch_ready(orch, None) is True
    assert shell._stage5_launch_ready({"stage_tokens": {"STAGE_5_LAUNCH": "PENDING"}}, None) is False


def test_synthetic_hydration_active_nested_recovery():
    from cockpit import desktop_app_shell as shell

    assert shell._synthetic_hydration_active({"synthetic_hydration_active": True}) is True
    assert shell._synthetic_hydration_active(
        {"transport_recovery": {"synthetic_hydration_active": True}}
    ) is True
    assert shell._synthetic_hydration_active({}) is False


def test_tick_cockpit_clearance_override_triggers_after_hold(monkeypatch):
    from cockpit import desktop_app_shell as shell

    shell._CLEARANCE_STATE["cleared"] = False
    shell._CLEARANCE_STATE["stage5_since"] = None
    shell._CLEARANCE_STATE["synthetic_since"] = None
    t0 = 1000.0
    times = iter([t0, t0 + 4.0])

    monkeypatch.setattr(shell.time, "time", lambda: next(times))
    with patch.object(shell, "_force_cockpit_live_layout_native") as mock_force, patch.object(
        shell, "_append_terminal"
    ):
        assert shell._tick_cockpit_clearance_override(
            orch={"stage_tokens": {"STAGE_5_LAUNCH": "SUCCESS"}},
            diag=None,
            status=None,
        ) is False
        assert shell._tick_cockpit_clearance_override(
            orch={"stage_tokens": {"STAGE_5_LAUNCH": "SUCCESS"}},
            diag=None,
            status=None,
        ) is True
        mock_force.assert_called_once()


def test_force_cockpit_live_layout_native_evaluates_js():
    from cockpit import desktop_app_shell as shell

    with patch.object(shell, "_evaluate") as mock_eval:
        shell._force_cockpit_live_layout_native()
        mock_eval.assert_called_once_with(shell._FORCE_COCKPIT_LIVE_JS)
        assert "cockpit-live" in shell._FORCE_COCKPIT_LIVE_JS
        assert "__forceCockpitLive" in shell._FORCE_COCKPIT_LIVE_JS


def test_transition_to_cockpit_starts_clearance_monitor():
    from cockpit import desktop_app_shell as shell

    mock_win = MagicMock()
    shell._WINDOW = mock_win
    shell._COCKPIT_EMBEDDED = False
    shell._CLEARANCE_MONITOR_STARTED = False
    shell._CLEARANCE_STATE["cleared"] = False

    with patch.object(shell, "_read_cache_busted_cockpit_html", return_value="<html></html>"), patch.object(
        shell, "_ensure_cockpit_clearance_monitor"
    ) as mock_monitor, patch.object(shell, "_evaluate"), patch.object(
        shell, "_append_terminal"
    ), patch("cockpit.desktop_app_shell.threading.Thread"):
        shell._transition_to_cockpit("http://127.0.0.1:8787/")

    assert shell._COCKPIT_EMBEDDED is True
    mock_win.load_html.assert_called_once()
    mock_monitor.assert_called_once()


def test_launcher_desktop_mode_starts_web_server_not_browser():
    from unittest.mock import MagicMock

    from cockpit.launcher import launch_flight_deck_after_gate4, reset_flight_deck_for_tests

    reset_flight_deck_for_tests()
    cfg = MagicMock()
    cfg.get.side_effect = lambda key, default=None: {
        "intelligence_layer": {
            "cockpit": {
                "enabled": True,
                "auto_launch_after_gate4": True,
                "telemetry_hz": 2.5,
                "web_port": 8787,
                "auto_open_browser": True,
            }
        }
    }.get(key, default)
    cfg.as_dict.return_value = {"instruments": {}}

    with patch.dict(os.environ, {"IG_APEX_DESKTOP": "1"}, clear=False), patch(
        "cockpit.launcher._resolve_epics", return_value=()
    ), patch("cockpit.web_server.start_cockpit_web_server", return_value=True) as mock_web, patch(
        "cockpit.launcher._open_cockpit_browser"
    ) as mock_browser:
        launch_flight_deck_after_gate4(cfg)

    mock_web.assert_called_once()
    mock_browser.assert_not_called()
    reset_flight_deck_for_tests()


def test_should_transition_when_verify_complete_and_api_healthy():
    from cockpit import desktop_app_shell as shell

    status = {"stage": "verify", "step": 8, "detail": "GUI verified"}
    health = {"ok": True, "routes": {"health": True}}
    assert shell._should_transition_to_cockpit(
        status=status,
        health=health,
        orch=None,
        diag=None,
    )


def test_should_transition_when_stage6_complete_amber():
    from cockpit import desktop_app_shell as shell

    status = {
        "stage": "post_ready",
        "step": 6,
        "detail": "Stage 6 complete — Execution plane amber_api_live",
        "boot_tier": "amber",
    }
    health = {"ok": True, "routes": {"health_light": True}}
    assert shell._should_transition_to_cockpit(
        status=status,
        health=health,
        orch=None,
        diag=None,
    )


def test_launcher_ready_for_cockpit_ready_stage():
    from cockpit import desktop_app_shell as shell

    assert shell._launcher_ready_for_cockpit({"stage": "ready", "step": 9}) is True
    assert shell._launcher_ready_for_cockpit({"stage": "verify", "step": 8}) is True
    assert shell._launcher_ready_for_cockpit({"stage": "warmup", "step": 7}) is False
