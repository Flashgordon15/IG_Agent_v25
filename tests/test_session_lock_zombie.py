"""Zombie-aware session lock — regression for the 76-minute restart-fail loop.

A hung boot leaves a defunct (zombie) ``main.py`` holding a ``status: HEALTHY``
session lock. ``os.kill(pid, 0)`` succeeds for zombies, so the old
``session_is_healthy`` treated that lock as live and ``clear_stale_lock``
refused to reap it — every subsequent restart collided on the lock and the
agent stayed down. These tests pin the fix: a zombie holder is never healthy,
and the lock is reaped.
"""

from __future__ import annotations

import json
from pathlib import Path

from runtime import session_lock


def _write_lock(path: Path, pid: int, status: str = "HEALTHY") -> None:
    path.write_text(
        json.dumps(
            {
                "pid": pid,
                "port": 8080,
                "account_scope": "ig:test",
                "status": status,
                "session_status": status,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_zombie_holder_not_healthy(tmp_path, monkeypatch):
    path = tmp_path / "session_ig_test.lock"
    _write_lock(path, pid=4242, status="HEALTHY")

    # pid appears alive (os.kill 0 succeeds for zombies) but ps reports "Z".
    monkeypatch.setattr(session_lock, "pid_alive", lambda pid: True)
    monkeypatch.setattr(session_lock, "pid_is_zombie", lambda pid: True)

    record = session_lock.read_session_lock(path)
    assert session_lock.session_is_healthy(record) is False


def test_clear_stale_lock_reaps_zombie_held_lock(tmp_path, monkeypatch):
    path = tmp_path / "session_ig_test.lock"
    _write_lock(path, pid=4242, status="HEALTHY")

    monkeypatch.setattr(session_lock, "pid_alive", lambda pid: True)
    monkeypatch.setattr(session_lock, "pid_is_zombie", lambda pid: True)

    assert session_lock.clear_stale_lock(path) is True
    assert not path.is_file()


def test_live_functional_holder_is_healthy(tmp_path, monkeypatch):
    path = tmp_path / "session_ig_test.lock"
    _write_lock(path, pid=4242, status="HEALTHY")

    monkeypatch.setattr(session_lock, "pid_alive", lambda pid: True)
    monkeypatch.setattr(session_lock, "pid_is_zombie", lambda pid: False)
    # Non-self pid with no reachable health endpoint still counts as alive.
    monkeypatch.setattr(session_lock, "health_endpoint_ok", lambda *a, **k: False)

    record = session_lock.read_session_lock(path)
    assert session_lock.session_is_healthy(record) is True
    assert session_lock.clear_stale_lock(path) is False
    assert path.is_file()


def test_pid_alive_and_functional_rejects_zombie(monkeypatch):
    monkeypatch.setattr(session_lock, "pid_alive", lambda pid: True)
    monkeypatch.setattr(session_lock, "pid_is_zombie", lambda pid: True)
    assert session_lock.pid_alive_and_functional(123) is False

    monkeypatch.setattr(session_lock, "pid_is_zombie", lambda pid: False)
    assert session_lock.pid_alive_and_functional(123) is True
