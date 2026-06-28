"""Shutdown contract + /api/health session identity tests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from api.gate_health_matrix import build_gate_health_response
from runtime.app_mode import AppMode, reset_app_mode_for_tests
from runtime.session_identity import build_session_identity_fields, mask_account_scope
from runtime.session_lock import (
    TESTBED_ACCOUNT_SCOPE,
    lock_path_for_scope,
    read_session_lock,
    reset_session_lock_state_for_tests,
    shutdown_session,
    write_session_lock,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    reset_app_mode_for_tests()
    reset_session_lock_state_for_tests()
    for key in (
        "APP_MODE",
        "IG_ACCOUNT_SCOPE",
        "IG_ACCOUNT_ID",
        "IG_DATA_ROOT",
        "IG_API_PORT",
        "IG_AGENT_CONFIG",
        "IG_ALLOW_LIVE",
    ):
        monkeypatch.delenv(key, raising=False)


def test_mask_account_scope_hides_ig_id():
    assert mask_account_scope("ig:ABC123456") == "ig:***"
    assert mask_account_scope(TESTBED_ACCOUNT_SCOPE) == TESTBED_ACCOUNT_SCOPE


def test_shutdown_terminates_pid_and_clears_lock(tmp_path, monkeypatch):
    scope = "ig:SHUTDOWN1"
    data_root = tmp_path / "production"
    data_root.mkdir()
    lock = lock_path_for_scope(scope, data_root)
    write_session_lock(lock, pid=99999, port=8080, account_scope=scope)

    monkeypatch.setenv("APP_MODE", "DEMO")
    monkeypatch.setenv("IG_ACCOUNT_SCOPE", scope)
    monkeypatch.setenv("IG_DATA_ROOT", str(data_root))
    reset_app_mode_for_tests()

    killed: list[int] = []

    def _fake_kill(pid, sig):
        killed.append(pid)

    with patch("runtime.session_lock.pid_alive", return_value=True), patch(
        "runtime.session_lock.os.kill", side_effect=_fake_kill
    ), patch("runtime.session_lock.time.sleep"):
        code, summary = shutdown_session(
            app_mode=AppMode.DEMO,
            account_scope=scope,
            data_root=data_root,
            term_wait_sec=0.1,
        )

    assert code == 0
    assert summary["pid"] == 99999
    assert summary["lock_cleared"] is True
    assert 99999 in killed
    assert not lock.is_file()


def test_shutdown_testbed_does_not_clear_demo_lock(tmp_path, monkeypatch):
    demo_root = tmp_path / "production"
    testbed_root = tmp_path / "testbed"
    demo_root.mkdir()
    testbed_root.mkdir()

    demo_scope = "ig:DEMOONLY"
    demo_lock = lock_path_for_scope(demo_scope, demo_root)
    write_session_lock(demo_lock, pid=111, port=8080, account_scope=demo_scope)

    tb_lock = lock_path_for_scope(TESTBED_ACCOUNT_SCOPE, testbed_root)
    write_session_lock(tb_lock, pid=222, port=9199, account_scope=TESTBED_ACCOUNT_SCOPE)

    monkeypatch.setenv("APP_MODE", "TESTBED")
    monkeypatch.setenv("IG_ACCOUNT_SCOPE", TESTBED_ACCOUNT_SCOPE)
    monkeypatch.setenv("IG_DATA_ROOT", str(testbed_root))
    reset_app_mode_for_tests()

    with patch("runtime.session_lock.pid_alive", return_value=False):
        code, summary = shutdown_session(
            app_mode=AppMode.TESTBED,
            account_scope=TESTBED_ACCOUNT_SCOPE,
            data_root=testbed_root,
        )

    assert code == 0
    assert summary["account_scope"] == TESTBED_ACCOUNT_SCOPE
    assert not tb_lock.is_file()
    assert demo_lock.is_file()
    assert read_session_lock(demo_lock) is not None


def test_shutdown_demo_does_not_clear_testbed_lock(tmp_path, monkeypatch):
    demo_root = tmp_path / "production"
    testbed_root = tmp_path / "testbed"
    demo_root.mkdir()
    testbed_root.mkdir()

    demo_scope = "ig:DEMO2"
    demo_lock = lock_path_for_scope(demo_scope, demo_root)
    write_session_lock(demo_lock, pid=333, port=8080, account_scope=demo_scope)

    tb_lock = lock_path_for_scope(TESTBED_ACCOUNT_SCOPE, testbed_root)
    write_session_lock(tb_lock, pid=444, port=9199, account_scope=TESTBED_ACCOUNT_SCOPE)

    monkeypatch.setenv("APP_MODE", "DEMO")
    monkeypatch.setenv("IG_ACCOUNT_SCOPE", demo_scope)
    monkeypatch.setenv("IG_DATA_ROOT", str(demo_root))
    reset_app_mode_for_tests()

    with patch("runtime.session_lock.pid_alive", return_value=False):
        code, _ = shutdown_session(
            app_mode=AppMode.DEMO,
            account_scope=demo_scope,
            data_root=demo_root,
        )

    assert code == 0
    assert not demo_lock.is_file()
    assert tb_lock.is_file()


def test_shutdown_no_lock_exits_with_message(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_MODE", "DEMO")
    monkeypatch.setenv("IG_ACCOUNT_SCOPE", "ig:NONE")
    monkeypatch.setenv("IG_DATA_ROOT", str(tmp_path))
    reset_app_mode_for_tests()

    code, summary = shutdown_session(
        app_mode=AppMode.DEMO,
        account_scope="ig:NONE",
        data_root=tmp_path,
    )
    assert code == 1
    assert "no active session" in summary["message"]


def test_health_identity_fields_demo_live_testbed(tmp_path, monkeypatch):
    cases = [
        ("DEMO", "ig:DEMOACCT", str(tmp_path / "prod"), "config/config_v31.json", "8080"),
        ("LIVE", "ig:LIVEACCT", str(tmp_path / "prod2"), "config/config_v31_live_canary.json", "8080"),
        ("TESTBED", TESTBED_ACCOUNT_SCOPE, str(tmp_path / "tb"), "config/config_v31_testbed.json", "9199"),
    ]
    for mode, scope, root, cfg, port in cases:
        reset_app_mode_for_tests()
        monkeypatch.setenv("APP_MODE", mode)
        monkeypatch.setenv("IG_ACCOUNT_SCOPE", scope)
        monkeypatch.setenv("IG_DATA_ROOT", root)
        monkeypatch.setenv("IG_AGENT_CONFIG", cfg)
        monkeypatch.setenv("IG_API_PORT", port)
        Path(root).mkdir(parents=True, exist_ok=True)
        lock = lock_path_for_scope(scope, root)
        write_session_lock(lock, pid=os.getpid(), port=int(port), account_scope=scope)

        fields = build_session_identity_fields()
        assert fields["app_mode"] == mode
        assert fields["config_overlay"] == cfg
        assert fields["data_root"] == root
        assert fields["port"] == int(port)
        assert fields["pid"] == os.getpid()
        assert "session_id" in fields
        assert fields["session_status"] in ("HEALTHY", "ZOMBIE")
        assert "engine_paths_armed" in fields
        assert set(fields["engine_paths_armed"].keys()) == {"path_a", "path_b", "micro"}

        if mode == "TESTBED":
            assert fields["account_scope"] == TESTBED_ACCOUNT_SCOPE
        else:
            assert fields["account_scope"] == "ig:***"
            assert "LIVEACCT" not in json.dumps(fields)
            assert "DEMOACCT" not in json.dumps(fields)


def test_health_endpoint_includes_identity_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_MODE", "DEMO")
    monkeypatch.setenv("IG_ACCOUNT_SCOPE", "ig:HEALTH1")
    monkeypatch.setenv("IG_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("IG_AGENT_CONFIG", "config/config_v31.json")
    monkeypatch.setenv("IG_API_PORT", "8080")
    reset_app_mode_for_tests()

    lock = lock_path_for_scope("ig:HEALTH1", tmp_path)
    write_session_lock(lock, pid=os.getpid(), port=8080, account_scope="ig:HEALTH1")

    with patch("api.gate_health_matrix.resolve_gate_health_matrix", return_value=(200, {"status": "OPERATIONAL", "ready": True})):
        code, body = build_gate_health_response(include_extended=True)

    assert code == 200
    assert body.get("app_mode") == "DEMO"
    assert body.get("account_scope") == "ig:***"
    assert body.get("config_overlay") == "config/config_v31.json"
    assert body.get("data_root") == str(tmp_path)
    assert "session_id" in body


def test_stop_sh_no_session(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("APP_MODE", "DEMO")
    monkeypatch.setenv("IG_ACCOUNT_SCOPE", "ig:STOPNONE")
    monkeypatch.setenv("IG_DATA_ROOT", str(tmp_path))
    reset_app_mode_for_tests()

    env = os.environ.copy()
    proc = subprocess.run(
        ["bash", str(root / "scripts" / "stop.sh"), "--mode", "DEMO"],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    assert "no active session" in proc.stdout.lower() or "no active session" in proc.stderr.lower()
