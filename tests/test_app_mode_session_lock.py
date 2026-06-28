"""APP_MODE + account-scoped session lock contract tests."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from runtime.app_mode import (
    AppMode,
    apply_app_mode_to_environ,
    parse_app_mode,
    reset_app_mode_for_tests,
    validate_live_armed,
)
from runtime.session_lock import (
    TESTBED_ACCOUNT_SCOPE,
    acquire_session_lock,
    find_active_session,
    lock_path_for_scope,
    preflight_startup,
    read_session_lock,
    release_session_lock,
    reset_session_lock_state_for_tests,
    resolve_account_scope,
    session_is_healthy,
    write_session_lock,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    reset_app_mode_for_tests()
    reset_session_lock_state_for_tests()
    for key in (
        "APP_MODE",
        "IG_ALLOW_LIVE",
        "IG_ACCOUNT_SCOPE",
        "IG_ACCOUNT_ID",
        "IG_DATA_ROOT",
        "IG_API_PORT",
        "IG_AGENT_ALLOW_MULTI_INSTANCE",
    ):
        monkeypatch.delenv(key, raising=False)


def test_parse_app_mode_validates():
    assert parse_app_mode("demo") is AppMode.DEMO
    with pytest.raises(ValueError):
        parse_app_mode("")


def test_live_rejected_unless_armed(monkeypatch):
    monkeypatch.setenv("APP_MODE", "LIVE")
    reset_app_mode_for_tests()
    with pytest.raises(RuntimeError, match="IG_ALLOW_LIVE"):
        validate_live_armed()

    monkeypatch.setenv("IG_ALLOW_LIVE", "1")
    reset_app_mode_for_tests()
    validate_live_armed()


def test_demo_cannot_start_twice_same_account_scope(tmp_path, monkeypatch):
    scope = "ig:TESTACCT001"
    data_root = tmp_path / "production"
    data_root.mkdir()
    lock = lock_path_for_scope(scope, data_root)
    write_session_lock(lock, pid=424242, port=8080, account_scope=scope)

    monkeypatch.setenv("APP_MODE", "DEMO")
    monkeypatch.setenv("IG_ACCOUNT_SCOPE", scope)
    monkeypatch.setenv("IG_DATA_ROOT", str(data_root))
    monkeypatch.setenv("IG_API_PORT", "8080")

    with patch("runtime.session_lock.pid_alive", return_value=True), patch(
        "runtime.session_lock.health_endpoint_ok", return_value=True
    ):
        assert session_is_healthy(read_session_lock(lock)) is True
        code, msg = preflight_startup(app_mode=AppMode.DEMO, port=8080, account_scope=scope, data_root=data_root)
        assert code == 3
        assert "session already active" in msg

        reset_session_lock_state_for_tests()
        monkeypatch.delenv("IG_AGENT_PYTEST", raising=False)
        ok, msg2 = acquire_session_lock()
        assert ok is False
        assert "healthy session" in msg2


def test_testbed_runs_alongside_demo_without_shared_locks(tmp_path, monkeypatch):
    demo_root = tmp_path / "production"
    testbed_root = tmp_path / "testbed"
    demo_root.mkdir()
    testbed_root.mkdir()

    demo_scope = "ig:DEMOACCT"
    demo_lock = lock_path_for_scope(demo_scope, demo_root)
    write_session_lock(demo_lock, pid=111, port=8080, account_scope=demo_scope)

    testbed_lock = lock_path_for_scope(TESTBED_ACCOUNT_SCOPE, testbed_root)
    write_session_lock(
        testbed_lock,
        pid=222,
        port=9199,
        account_scope=TESTBED_ACCOUNT_SCOPE,
    )

    with patch("runtime.session_lock.pid_alive", return_value=True), patch(
        "runtime.session_lock.health_endpoint_ok", return_value=True
    ):
        demo_active, _ = find_active_session(demo_scope, demo_root)
        tb_active, _ = find_active_session(TESTBED_ACCOUNT_SCOPE, testbed_root)
        assert demo_active == demo_lock
        assert tb_active == testbed_lock
        assert demo_lock != testbed_lock

    monkeypatch.setenv("APP_MODE", "TESTBED")
    monkeypatch.setenv("IG_DATA_ROOT", str(testbed_root))
    monkeypatch.setenv("IG_ACCOUNT_SCOPE", TESTBED_ACCOUNT_SCOPE)
    reset_app_mode_for_tests()

    with patch("runtime.session_lock.pid_alive", return_value=True), patch(
        "runtime.session_lock.health_endpoint_ok", return_value=True
    ), patch("runtime.session_lock.port_bound_by_foreign", return_value=None):
        code, msg = preflight_startup(
            app_mode=AppMode.TESTBED,
            port=9199,
            account_scope=TESTBED_ACCOUNT_SCOPE,
            data_root=testbed_root,
        )
        assert code == 3
        assert TESTBED_ACCOUNT_SCOPE in msg

    testbed2 = tmp_path / "testbed2"
    testbed2.mkdir()
    with patch("runtime.session_lock.port_bound_by_foreign", return_value=None):
        code2, _ = preflight_startup(
            app_mode=AppMode.TESTBED,
            port=9199,
            account_scope=TESTBED_ACCOUNT_SCOPE,
            data_root=testbed2,
        )
        assert code2 == 0


def test_apply_app_mode_sets_broker_plane(monkeypatch):
    monkeypatch.setenv("APP_MODE", "DEMO")
    reset_app_mode_for_tests()
    apply_app_mode_to_environ()
    assert os.environ["IG_BROKER_PLANE"] == "DEMO"
    assert os.environ["IG_APEX_RUNTIME_MODE"] == "PRODUCTION"

    monkeypatch.setenv("APP_MODE", "TESTBED")
    reset_app_mode_for_tests()
    apply_app_mode_to_environ()
    assert os.environ["IG_BROKER_PLANE"] == "MOCK"
    assert os.environ["IG_APEX_RUNTIME_MODE"] == "HARDENED_TESTBED"


def test_resolve_account_scope_testbed():
    assert resolve_account_scope(AppMode.TESTBED) == TESTBED_ACCOUNT_SCOPE


def test_live_preflight_exit_code(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_MODE", "LIVE")
    reset_app_mode_for_tests()
    code, msg = preflight_startup(
        app_mode=AppMode.LIVE,
        port=8080,
        account_scope="ig:LIVE1",
        data_root=tmp_path,
    )
    assert code == 2
    assert "IG_ALLOW_LIVE" in msg
