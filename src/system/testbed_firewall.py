"""
HARDENED_TESTBED firewall — deterministic replay with zero production contamination.

When ``ApexRuntimeMode.HARDENED_TESTBED`` is active:
  - All state → ``testbed_state.json``
  - All ledger SQLite → ``testbed_ledger.db``
  - Any production / shadow-live DB path access → instant process panic (exit 99)
  - Live IG/Yahoo network transport is replaced by loopback replay (see testbed_loopback_transport)
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Never

from system.engine_log import log_engine


class TestbedFirewallPanic(RuntimeError):
    """Raised instead of os._exit when ``IG_TESTBED_PANIC_RAISE=1`` (unit tests)."""


_ARMED = False
_ARM_LOCK = threading.RLock()
_TESTBED_ROOT: Path | None = None

TESTBED_STATE_NAME = "testbed_state.json"
TESTBED_LEDGER_NAME = "testbed_ledger.db"
TESTBED_REPLAY_SOCK_NAME = "testbed_replay.sock"
TESTBED_REPLAY_FEED_NAME = "testbed_replay.jsonl"

# Production / shadow-live filenames that must never be opened in testbed mode.
_FORBIDDEN_DB_NAMES = frozenset(
    {
        "learning_db.sqlite3",
        "learning_db_shadow.sqlite3",
        "triage_v30.db",
        "production.db",
    }
)
_FORBIDDEN_STATE_NAMES = frozenset(
    {
        "runtime_state.json",
        "runtime_state_shadow.json",
        "dashboard_snapshot.json",
    }
)


def testbed_root() -> Path:
    global _TESTBED_ROOT
    with _ARM_LOCK:
        if _TESTBED_ROOT is not None:
            return _TESTBED_ROOT
        env = os.environ.get("IG_TESTBED_ROOT", "").strip()
        if env:
            root = Path(env).resolve()
        else:
            from system.paths import apex_isolated_root

            root = (apex_isolated_root() / "testbed" / "hardened").resolve()
        for sub in ("data", "data/state", "data/logs", "analytics", "replay"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        _TESTBED_ROOT = root
        return root


def testbed_state_path() -> Path:
    return testbed_root() / "data" / "state" / TESTBED_STATE_NAME


def testbed_ledger_path() -> Path:
    return testbed_root() / "data" / TESTBED_LEDGER_NAME


def testbed_replay_socket_path() -> Path:
    return testbed_root() / "data" / TESTBED_REPLAY_SOCK_NAME


def testbed_replay_feed_path() -> Path:
    return testbed_root() / "replay" / TESTBED_REPLAY_FEED_NAME


def is_testbed_firewall_active() -> bool:
    return _ARMED


def testbed_panic(reason: str) -> Never:
    """Critical security halt — production path touched under HARDENED_TESTBED."""
    msg = f"TESTBED FIREWALL PANIC: {reason}"
    try:
        log_engine(msg)
    except Exception:
        pass
    if os.environ.get("IG_TESTBED_PANIC_RAISE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        raise TestbedFirewallPanic(msg)
    print(msg, file=sys.stderr, flush=True)
    os._exit(99)


def _path_is_under_testbed(path: Path) -> bool:
    try:
        path.resolve().relative_to(testbed_root().resolve())
        return True
    except ValueError:
        return False


def is_production_contamination_path(path: Path | str) -> bool:
    """True when ``path`` resolves to a forbidden production/shadow-live artifact."""
    if not is_testbed_firewall_active():
        return False
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError):
        return False
    if _path_is_under_testbed(resolved):
        return False
    name = resolved.name.lower()
    if name in _FORBIDDEN_DB_NAMES or name in _FORBIDDEN_STATE_NAMES:
        return True
    if name.endswith(".sqlite3") and name not in (TESTBED_LEDGER_NAME,):
        return True
    try:
        from system.paths import is_legacy_data_path

        if is_legacy_data_path(resolved):
            return True
    except Exception:
        pass
    blob = str(resolved).lower()
    if "ig agent apex" in blob and "testbed" not in blob:
        if any(x in blob for x in ("learning_db", "triage_v30", "runtime_state")):
            return True
    return False


def guard_path(path: Path | str, *, operation: str = "access") -> Path:
    """Assert path is testbed-safe; panic on production contamination."""
    p = Path(path)
    if is_production_contamination_path(p):
        testbed_panic(f"blocked production {operation}: {p}")
    if is_testbed_firewall_active() and not _path_is_under_testbed(p):
        # Allow read-only repo code paths; block writable data outside testbed root.
        if p.suffix.lower() in (".db", ".sqlite3", ".json", ".jsonl", ".wal", ".shm"):
            testbed_panic(f"writable {operation} outside testbed root: {p}")
    return p


def guard_database_path(db_path: str | Path, *, operation: str = "open") -> None:
    guard_path(db_path, operation=operation)


def guard_state_path(state_path: str | Path, *, operation: str = "read") -> None:
    guard_path(state_path, operation=operation)


def arm_testbed_firewall() -> Path:
    """Pin all runtime I/O to isolated testbed files and disable outbound broker I/O."""
    global _ARMED
    with _ARM_LOCK:
        root = testbed_root()
        state = testbed_state_path()
        ledger = testbed_ledger_path()
        state.parent.mkdir(parents=True, exist_ok=True)
        ledger.parent.mkdir(parents=True, exist_ok=True)

        os.environ["IG_AGENT_DATA_DIR"] = str(root / "data")
        os.environ["IG_LEARNING_DB"] = str(ledger)
        os.environ["IG_TRIAGE_DB"] = str(ledger)
        os.environ["IG_RUNTIME_STATE_FILE"] = str(state)
        os.environ["IG_ANALYTICS_DB"] = str(ledger)
        os.environ["IG_API_PORT"] = "9199"
        os.environ["IG_APEX_RUNTIME_MODE"] = "HARDENED_TESTBED"
        os.environ["IG_NODE_PROFILE"] = "testbed"
        os.environ["NODE_ENV"] = "testbed"
        os.environ["IG_MULTI_API_BROKER"] = "0"
        os.environ["IG_MOCK_FEED_ACTIVE"] = "0"
        os.environ["IG_MOCK_FEED"] = "0"
        os.environ["IG_AGENT_MODE"] = ""
        os.environ.setdefault("IG_TESTBED_LOOPBACK", "1")

        _ARMED = True
        log_engine(
            f"TESTBED FIREWALL armed — state={state.name} ledger={ledger.name} "
            f"root={root}"
        )
        return root


def ensure_testbed_firewall_armed() -> None:
    """Idempotent — safe from microkernel / hub init."""
    from system.apex_runtime_mode import ApexRuntimeMode, get_apex_runtime_mode

    if get_apex_runtime_mode() is ApexRuntimeMode.HARDENED_TESTBED:
        if not _ARMED:
            arm_testbed_firewall()


def reset_testbed_firewall_for_tests() -> None:
    global _ARMED, _TESTBED_ROOT
    with _ARM_LOCK:
        _ARMED = False
        _TESTBED_ROOT = None
