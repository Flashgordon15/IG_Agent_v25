"""Regression tests for stability hardening (port eviction, watchdog, locks)."""

from __future__ import annotations

import os
import signal
from unittest.mock import MagicMock, patch

import pytest


def test_port_eviction_term_before_kill(monkeypatch):
    from system.boot import port_eviction

    calls: list[int] = []

    def fake_kill(pid: int, sig: int) -> None:
        calls.append(sig)
        if sig == signal.SIGTERM:
            return
        raise ProcessLookupError()

    monkeypatch.setattr(port_eviction.os, "getpid", lambda: 9999)
    monkeypatch.setattr(port_eviction.os, "getppid", lambda: 9998)
    monkeypatch.setattr(port_eviction.os, "kill", fake_kill)
    monkeypatch.setattr(
        port_eviction.subprocess,
        "run",
        lambda *a, **k: MagicMock(stdout="1234\n", returncode=0),
    )
    monkeypatch.setattr(port_eviction, "should_protect_production_port", lambda _p: False)

    killed = port_eviction.reclaim_api_port(9199, force=True)
    assert 1234 in killed
    assert signal.SIGTERM in calls


def test_watchdog_recovery_suppressed_on_testbed():
    from system.apex_runtime_mode import ApexRuntimeMode
    from system.watchdog_sentinel import WatchdogSelfHealer

    healer = WatchdogSelfHealer()
    with patch(
        "system.apex_runtime_mode.get_apex_runtime_mode",
        return_value=ApexRuntimeMode.HARDENED_TESTBED,
    ):
        assert healer._recovery_suppressed() == "hardened_testbed"


def test_shutdown_port_resolves_profile():
    from system.shutdown_cleanup import _resolve_shutdown_port

    with patch("system.boot.preflight_helpers.resolve_api_port", return_value=9090):
        assert _resolve_shutdown_port() == 9090


def test_runtime_guard_swallows_callback_errors():
    from system.runtime_guard import guard_call

    def boom() -> str:
        raise RuntimeError("listener failed")

    assert guard_call("test_sub", boom, default="ok") == "ok"


def test_testbed_loopback_skips_file_replay_when_historical_env():
    from ig_api.testbed_loopback_transport import TestbedLoopbackTransport

    transport = TestbedLoopbackTransport()
    with patch.dict(os.environ, {"IG_HISTORICAL_REPLAY": "/tmp/archive.jsonl"}):
        with patch.object(transport, "_fill_socket_loop"):
            transport.connect()
    assert transport._thread is None
    transport.disconnect()


def test_runtime_identity_port_routing(monkeypatch):
    """Env pollution isolated — each profile resolves its own port."""
    from unittest.mock import patch

    from system.apex_runtime_mode import ApexRuntimeMode
    from system.identity.app_identity import RuntimeIdentity

    monkeypatch.delenv("IG_API_PORT", raising=False)
    monkeypatch.delenv("IG_APEX_DESKTOP", raising=False)
    monkeypatch.delenv("IG_NODE_PROFILE", raising=False)
    monkeypatch.delenv("NODE_ENV", raising=False)

    with patch(
        "system.apex_runtime_mode.get_apex_runtime_mode",
        return_value=ApexRuntimeMode.HARDENED_TESTBED,
    ):
        assert RuntimeIdentity.resolve_api_port() == 9199

    monkeypatch.setenv("IG_NODE_PROFILE", "shadow")
    assert RuntimeIdentity.resolve_api_port() == 9090

    monkeypatch.delenv("IG_NODE_PROFILE", raising=False)
    assert RuntimeIdentity.resolve_api_port() == 8080

    monkeypatch.setenv("IG_API_PORT", "7777")
    assert RuntimeIdentity.resolve_api_port() == 7777


def test_instance_lock_idempotent_acquire(tmp_path, monkeypatch):
    from system.identity import instance_lock as il

    lock = tmp_path / ".ig_agent_v30_port_8080.lock"
    monkeypatch.setattr(il, "lock_path", lambda port=None: lock)
    monkeypatch.setattr(
        il.RuntimeIdentity,
        "legacy_lock_paths",
        staticmethod(lambda: []),
    )
    monkeypatch.setattr(
        il.RuntimeIdentity,
        "export_pointer_for_scripts",
        staticmethod(lambda: tmp_path / "pointer"),
    )

    ok1, _ = il.acquire_instance_lock()
    ok2, _ = il.acquire_instance_lock()
    assert ok1 and ok2
    assert lock.read_text(encoding="utf-8").strip() == str(os.getpid())
