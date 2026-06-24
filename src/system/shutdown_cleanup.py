"""Graceful agent teardown and startup/stop deployment verification."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception
from system.paths import data_dir, find_python_executable, project_root

# Supervision / post-exit utilities — must stay executable for launchd + verify spawn.
_SUPERVISION_UTILITY_REL_PATHS: tuple[str, ...] = (
    "scripts/watchdog.sh",
    "scripts/install_launchd.sh",
    "scripts/ensure_overnight_ready.sh",
    "scripts/emergency_stop.sh",
    "scripts/confirm_stopped.py",
    "scripts/confirm_started.py",
    "scripts/supervision_check.py",
    "scripts/shutdown_verify_server.py",
    "scripts/watchdog_launchd.py",
    "scripts/safe_to_leave.py",
    "scripts/loop_monitor_tick.sh",
    "scripts/overnight_watch.sh",
)

_cleanup_done = False
_MANUAL_STOP_FILE = data_dir() / "state" / "manual_stop.json"
_MANUAL_STOP_MAX_AGE_SEC = 600.0
_TRADING_LEDGER_PATH = data_dir() / "state" / "trading_ledger.json"
_STATE_SYNC_THREAD_NAME = "ig-v6.1-ledger-sync"
_STATE_SYNC_DEBOUNCE_SEC = 0.75
_state_sync_lock = threading.Lock()
_state_sync_event: threading.Event | None = None
_state_sync_thread: threading.Thread | None = None
_state_sync_started = False
_last_position_fingerprint: str | None = None


def reset_shutdown_verify_state() -> None:
    """Clear stale verify snapshot so dashboard polling cannot read a prior run."""
    path = data_dir() / "state" / "last_shutdown_verify.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "ok": False,
                    "status": "pending",
                    "checks": [],
                    "issues": [],
                    "ts": time.time(),
                }
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def mark_manual_stop(*, source: str = "dashboard") -> None:
    """Signal watchdog/launchd not to auto-restart after deliberate Stop Agent."""
    try:
        reset_shutdown_verify_state()
        _MANUAL_STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MANUAL_STOP_FILE.write_text(
            json.dumps({"ts": time.time(), "source": source}),
            encoding="utf-8",
        )
        log_engine(f"manual_stop: flagged (source={source})")
        try:
            from system.overnight_supervision import clear_overnight_armed

            clear_overnight_armed()
        except Exception:
            pass
    except Exception as e:
        log_engine(f"manual_stop: flag failed: {type(e).__name__}: {e}")


def clear_manual_stop() -> None:
    try:
        _MANUAL_STOP_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def manual_stop_active(*, max_age_sec: float = _MANUAL_STOP_MAX_AGE_SEC) -> bool:
    if not _MANUAL_STOP_FILE.is_file():
        return False
    try:
        raw = json.loads(_MANUAL_STOP_FILE.read_text(encoding="utf-8"))
        age = time.time() - float(raw.get("ts") or 0)
        return age >= 0 and age < max_age_sec
    except Exception:
        return True


def _position_state_fingerprint() -> str:
    """Stable hash of open positions + protective stops + inflight orders."""
    try:
        from system.runtime_state_persist import _collect_state

        blob = _collect_state()
    except Exception:
        blob = {}
    open_rows: list[dict[str, Any]] = []
    try:
        from system.config_loader import get_config
        from data.learning_store import LearningStore

        store = LearningStore(str(get_config().learning_db))
        for row in store.active_trades() or []:
            d = dict(row)
            open_rows.append(
                {
                    "deal_id": d.get("ig_deal_id") or d.get("deal_id"),
                    "epic": d.get("epic"),
                    "size": d.get("size"),
                    "stop_level": d.get("stop_level") or d.get("stop"),
                    "limit_level": d.get("limit_level") or d.get("limit"),
                    "direction": d.get("direction"),
                }
            )
    except Exception:
        pass
    payload = {
        "runtime": blob,
        "open_positions": sorted(
            open_rows,
            key=lambda r: str(r.get("deal_id") or r.get("epic") or ""),
        ),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build_trading_ledger_snapshot() -> dict[str, Any]:
    from datetime import datetime, timezone

    from system.runtime_state_persist import _collect_state

    runtime = _collect_state()
    open_rows: list[dict[str, Any]] = []
    closed_rows: list[dict[str, Any]] = []
    try:
        from system.config_loader import get_config
        from data.learning_store import LearningStore

        store = LearningStore(str(get_config().learning_db))
        for row in store.active_trades() or []:
            open_rows.append(dict(row))
        closed_rows = store.recent_closed_trades(limit=128)
    except Exception:
        pass
    existing: dict[str, Any] = {}
    if _TRADING_LEDGER_PATH.is_file():
        try:
            loaded = json.loads(_TRADING_LEDGER_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            pass
    payload = dict(existing)
    payload.update(
        {
            "mode": payload.get("mode") or "V6_1_VOLATILE_STATE_SYNC",
            "sync_source": "position_state_change",
            "runtime_state": runtime,
            "open_positions": open_rows,
            "closed_trades": closed_rows,
            "volatile_tick_governor": _tick_governor_snapshot(),
        }
    )
    payload["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return payload


def _tick_governor_snapshot() -> dict[str, Any]:
    try:
        from trading.cache_reaper import tick_governor_telemetry

        return tick_governor_telemetry()
    except Exception:
        return {}


def _atomic_write_ledger(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".ledger_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _flush_trading_ledger_if_changed() -> bool:
    global _last_position_fingerprint
    if os.environ.get("IG_AGENT_PYTEST") == "1":
        return False
    fp = _position_state_fingerprint()
    with _state_sync_lock:
        if fp == _last_position_fingerprint:
            return False
    try:
        snapshot = _build_trading_ledger_snapshot()
        _atomic_write_ledger(_TRADING_LEDGER_PATH, snapshot)
        with _state_sync_lock:
            _last_position_fingerprint = fp
        log_engine(
            f"v6.1 state sync: trading_ledger.json flushed "
            f"(open={len(snapshot.get('open_positions') or [])})"
        )
        return True
    except Exception as exc:
        log_guarded_exception("v6.1_ledger_sync_flush", exc)
        return False


def _state_sync_worker() -> None:
    global _state_sync_event
    event = _state_sync_event
    if event is None:
        return
    while True:
        if event.wait(timeout=30.0):
            event.clear()
            deadline = time.time() + _STATE_SYNC_DEBOUNCE_SEC
            while time.time() < deadline:
                time.sleep(0.05)
            _flush_trading_ledger_if_changed()


def start_state_synchronization_pipeline() -> None:
    """Low-priority background worker — disk flush only on position mutations."""
    global _state_sync_started, _state_sync_event, _state_sync_thread
    if os.environ.get("IG_AGENT_PYTEST") == "1":
        return
    with _state_sync_lock:
        if _state_sync_started:
            return
        _state_sync_event = threading.Event()
        _state_sync_thread = threading.Thread(
            target=_state_sync_worker,
            name=_STATE_SYNC_THREAD_NAME,
            daemon=True,
        )
        _state_sync_thread.start()
        _state_sync_started = True
    log_engine("v6.1 state sync pipeline armed (position-change ledger flush)")


def force_flush_trading_ledger_checkpoint(snapshot: dict[str, Any]) -> bool:
    """Public API for V6.2 recovery — atomic ledger write with pre-built snapshot."""
    try:
        _atomic_write_ledger(_TRADING_LEDGER_PATH, snapshot)
        with _state_sync_lock:
            global _last_position_fingerprint
            _last_position_fingerprint = _position_state_fingerprint()
        return True
    except Exception as exc:
        log_guarded_exception("v6.2_force_ledger_flush", exc)
        return False


def notify_position_state_change(*, reason: str = "") -> None:
    """Signal the ledger sync worker after open / stop modify / close events."""
    if os.environ.get("IG_AGENT_PYTEST") == "1":
        try:
            from system.recovery_mgr import get_disaster_recovery_manager

            get_disaster_recovery_manager().notify_transaction_state_change(reason=reason)
        except Exception:
            pass
        return
    start_state_synchronization_pipeline()
    try:
        from system.recovery_mgr import get_disaster_recovery_manager

        get_disaster_recovery_manager().notify_transaction_state_change(reason=reason)
    except Exception:
        pass
    event = _state_sync_event
    if event is not None:
        event.set()
        if reason:
            log_engine(f"v6.1 state sync queued: {reason}")


def reset_state_sync_pipeline_for_tests() -> None:
    global _state_sync_started, _state_sync_event, _state_sync_thread, _last_position_fingerprint
    with _state_sync_lock:
        _state_sync_started = False
        _state_sync_event = None
        _state_sync_thread = None
        _last_position_fingerprint = None


def reset_shutdown_cleanup_for_tests() -> None:
    """Allow repeated cleanup in pytest."""
    global _cleanup_done
    _cleanup_done = False


def is_broker_connectivity_failure(exc: BaseException) -> bool:
    """True for broker/network dropouts that require supervised RAM + lock purge."""
    if isinstance(
        exc,
        (
            ConnectionError,
            ConnectionResetError,
            BrokenPipeError,
            TimeoutError,
            socket.timeout,
        ),
    ):
        return True
    if isinstance(exc, OSError):
        code = getattr(exc, "errno", None)
        if code in (
            errno.ECONNREFUSED,
            errno.ECONNRESET,
            errno.ETIMEDOUT,
            errno.EHOSTUNREACH,
            errno.ENETUNREACH,
            errno.ENETDOWN,
            errno.EPIPE,
        ):
            return True
    try:
        from ig_api.exceptions import IGAPIError, IGStreamError

        if isinstance(exc, (IGAPIError, IGStreamError)):
            status = getattr(exc, "status_code", None)
            if status in (502, 503, 504):
                return True
            msg = str(exc).lower()
            if any(
                token in msg
                for token in (
                    "connection reset",
                    "connection refused",
                    "network is unreachable",
                    "timed out",
                    "timeout",
                    "broken pipe",
                    "connection aborted",
                    "failed to establish",
                )
            ):
                return True
    except Exception:
        pass
    return False


def _network_teardown_capital_guard_flatten() -> None:
    """Best-effort flatten via CapitalGuard before lock + SHM purge."""
    rest_client = None
    try:
        import system.ig_rest_session as session_mod

        with session_mod._lock:
            rest_client = session_mod._client
    except Exception as exc:
        log_guarded_exception("network_teardown_rest_client", exc)
    try:
        from execution.capital_guard import CapitalGuard

        CapitalGuard._cancel_all_open_orders_and_positions(rest_client)
    except Exception as exc:
        log_guarded_exception("network_teardown_capital_guard", exc)


def force_release_parallel_port_locks(ports: tuple[int, ...] = (8080, 9199)) -> None:
    """Unlink port-anchored v30 lock markers for parallel live + shadow tracks."""
    from system.identity.app_identity import RuntimeIdentity

    for port in ports:
        path = RuntimeIdentity.get_lock_path(port)
        try:
            if path.is_file():
                path.unlink(missing_ok=True)
                log_engine(f"network teardown: unlinked port lock {path.name}")
        except OSError as exc:
            log_guarded_exception(f"network_teardown_lock_{port}", exc)
    try:
        from system.identity.instance_lock import force_release_instance_lock

        force_release_instance_lock()
    except Exception as exc:
        log_guarded_exception("network_teardown_instance_lock", exc)


def purge_shared_memory_segments(*, reinit: bool = True) -> None:
    """Close, unlink, and optionally re-seed native telemetry RAM segments."""
    try:
        from system.identity.shared_memory_bridge import (
            get_shared_memory_bridge,
            reset_shared_memory_bridge,
            track_label_for_key,
        )
        from system.identity.weight_transfer_bridge import (
            get_weight_transfer_bridge,
            reset_weight_transfer_bridge,
        )

        reset_shared_memory_bridge(unlink=True)
        reset_weight_transfer_bridge(unlink=True)
        if not reinit:
            return
        now = time.time()
        for track in ("live", "shadow"):
            port = 8080 if track == "live" else 9199
            payload = {
                "schema_version": "1.0",
                "track": track_label_for_key(track),
                "api_port": port,
                "updated_at_epoch": now,
                "trailing_stops": [],
                "ml_optimization": {},
                "system_health": {"network_teardown_epoch": now},
            }
            get_shared_memory_bridge(create=True, track=track).write_json(payload)
        get_weight_transfer_bridge(create=True).write_json(
            {"approved": False, "track": "shadow", "published_at": now}
        )
        _detach_shared_memory_handles()
        log_engine("network teardown: shared memory segments purged and re-seeded")
    except SystemExit:
        raise
    except Exception as exc:
        log_guarded_exception("network_teardown_shared_memory", exc)


def _unregister_shm_from_resource_tracker(segment_name: str) -> None:
    """Prevent ``os._exit`` subprocesses from unlinking re-seeded RAM segments."""
    try:
        from multiprocessing import resource_tracker

        key = segment_name if segment_name.startswith("/") else f"/{segment_name}"
        resource_tracker.unregister(key, "shared_memory")
    except Exception as exc:
        log_guarded_exception("network_teardown_shm_unregister", exc)


def _detach_shared_memory_handles() -> None:
    """Close producer handles without unlinking — segments must survive ``os._exit(0)``."""
    try:
        from system.identity import shared_memory_bridge as shm_mod
        from system.identity import weight_transfer_bridge as wt_mod

        for name in (
            "ig_agent_v30_live_state",
            "ig_agent_v30_shadow_state",
            "ig_agent_v30_weight_xfer",
        ):
            _unregister_shm_from_resource_tracker(name)

        with shm_mod._bridge_lock:
            for bridge in list(shm_mod._bridge_singletons.values()):
                bridge.close(unlink=False)
            shm_mod._bridge_singletons.clear()
        with wt_mod._bridge_lock:
            bridge = wt_mod._bridge_singleton
            if bridge is not None:
                bridge.close(unlink=False)
            wt_mod._bridge_singleton = None
    except Exception as exc:
        log_guarded_exception("network_teardown_shm_detach", exc)


def perform_network_failure_teardown(
    exc: BaseException,
    *,
    source: str = "network",
) -> None:
    """
    Supervised fail-closed purge on broker connectivity loss.

    Sequential law: CapitalGuard flatten → port locks → SHM unlink → ``os._exit(0)``.
    Never returns.
    """
    log_guarded_exception(
        "network_failure_teardown",
        exc,
        detail=f"source={source} {type(exc).__name__}",
    )
    log_engine(
        "network failure teardown: begin "
        f"({type(exc).__name__}: {exc}) source={source}"
    )

    _network_teardown_capital_guard_flatten()
    force_release_parallel_port_locks()
    purge_shared_memory_segments(reinit=True)

    print(f"PROPAGATED {type(exc).__name__} {exc}", flush=True)
    log_engine("network failure teardown: complete — os._exit(0)")
    os._exit(0)


def _should_skip_pid(pid: int, exclude_pid: int | None) -> bool:
    """Never signal the running process; also honour an explicit exclude PID."""
    if pid == os.getpid():
        return True
    if exclude_pid is not None and pid == exclude_pid:
        return True
    try:
        from simulation.testbed_daemon import is_protected_pid, zombie_protection_enabled

        if zombie_protection_enabled() and is_protected_pid(pid):
            return True
    except Exception:
        pass
    return False


def kill_other_agent_processes(
    *,
    exclude_pid: int | None = None,
    sigkill_survivors: bool = True,
    wait_sec: float = 3.0,
    log_label: str = "agent cleanup",
) -> list[int]:
    """SIGTERM (then optional SIGKILL) any other src/main.py processes."""
    if os.environ.get("IG_AGENT_PYTEST") == "1":
        return []
    if os.environ.get("IG_AGENT_SKIP_ORPHAN_KILL", "").strip() in ("1", "true", "yes"):
        return []
    if os.environ.get("IG_PARALLEL_TRACK", "").strip() in ("live", "shadow"):
        return []
    try:
        from system.identity.process_orchestrator import read_pid_registry

        registry = read_pid_registry()
        sibling_pids = {
            int(registry[k])
            for k in ("live_pid", "shadow_pid")
            if registry.get(k) is not None
        }
    except Exception:
        sibling_pids = set()
    killed: list[int] = []
    try:
        result = subprocess.run(
            ["/usr/bin/pgrep", "-f", "src/main.py"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for pid_str in result.stdout.strip().splitlines():
            try:
                pid = int(pid_str.strip())
            except ValueError:
                continue
            if _should_skip_pid(pid, exclude_pid):
                continue
            if pid in sibling_pids:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
                log_engine(f"{log_label}: SIGTERM orphan agent PID {pid}")
            except ProcessLookupError:
                pass
            except Exception as e:
                log_engine(f"{log_label}: could not SIGTERM PID {pid}: {e}")
    except Exception as e:
        log_engine(f"{log_label}: pgrep failed: {e}")
        return killed

    if not killed or not sigkill_survivors:
        return killed

    deadline = time.time() + wait_sec
    while time.time() < deadline:
        alive = []
        for pid in killed:
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except ProcessLookupError:
                pass
            except Exception:
                pass
        if not alive:
            break
        time.sleep(0.2)

    for pid in killed:
        if _should_skip_pid(pid, exclude_pid):
            continue
        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)
            log_engine(f"{log_label}: SIGKILL orphan agent PID {pid}")
        except ProcessLookupError:
            pass
        except Exception:
            pass
    return killed


def perform_shutdown_cleanup(
    *, source: str = "shutdown", skip_port_cleanup: bool = False
) -> None:
    """Full teardown so Stop Agent leaves no rogue sessions or duplicate processes."""
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True

    if os.environ.get("IG_AGENT_PYTEST") == "1":
        log_engine(
            f"shutdown cleanup: skipped live teardown (IG_AGENT_PYTEST=1, source={source})"
        )
        return

    log_engine(f"shutdown cleanup: begin (source={source})")

    try:
        from harmonization.clean_shutdown import write_crash_state

        write_crash_state(source=f"shutdown_cleanup:{source}")
    except Exception as e:
        log_engine(f"shutdown cleanup: crash_state error (continuing): {e}")

    try:
        from api.agent_control import stop_trading

        stop_trading()
    except Exception as e:
        log_engine(f"shutdown cleanup: stop_trading error (continuing): {e}")

    try:
        from runtime.agent_bootstrap import (
            stop_ig_position_sync,
            stop_market_stream,
            stop_order_reconciler_worker,
        )

        stop_market_stream()
        try:
            from feeder.yahoo_quote_poller import stop_yahoo_quote_poller

            stop_yahoo_quote_poller()
        except Exception:
            pass
        stop_order_reconciler_worker()
        stop_ig_position_sync()
        log_engine("shutdown cleanup: market stream, reconciler, and position sync stopped")
    except Exception as e:
        log_engine(f"shutdown cleanup: stream/sync stop error (continuing): {e}")

    try:
        from cockpit.launcher import stop_flight_deck

        stop_flight_deck()
    except Exception:
        pass

    try:
        from system.trading_health_monitor import stop_trading_health_monitor

        stop_trading_health_monitor()
    except Exception:
        pass

    try:
        from system.telegram_alerts import stop_hourly_executive_telegram_scheduler
        from system.telegram_notifier import stop_telegram_heartbeat

        stop_hourly_executive_telegram_scheduler()
        stop_telegram_heartbeat()
    except Exception:
        pass

    try:
        from data.learning_store import LearningStore
        from system.config_loader import get_config

        store = LearningStore(str(get_config().learning_db))
        store.checkpoint()
        store.close()
        log_engine("shutdown cleanup: learning store checkpointed")
    except Exception as e:
        log_engine(
            f"shutdown cleanup: learning store checkpoint error (continuing): {e}"
        )

    try:
        from system.runtime_state_persist import flush_disk_on_shutdown

        flush_disk_on_shutdown()
        log_engine("shutdown cleanup: volatile runtime caches flushed to disk")
    except Exception as e:
        log_engine(f"shutdown cleanup: volatile cache flush error (continuing): {e}")

    try:
        _flush_trading_ledger_if_changed()
    except Exception as e:
        log_engine(f"shutdown cleanup: trading ledger sync error (continuing): {e}")

    try:
        from system.ig_rest_session import shutdown_shared_ig_session

        shutdown_shared_ig_session()
    except Exception as e:
        log_engine(f"shutdown cleanup: IG session end error (continuing): {e}")

    try:
        from api.agent_health import stop_watchdog
        from system.overnight_supervision import launchd_watchdog_active

        launchd_wd = launchd_watchdog_active()
        # Preserve launchd only when actually loaded; otherwise stop standalone watchdog.
        stop_watchdog(preserve_launchd=launchd_wd)
        if launchd_wd:
            log_engine(
                "shutdown cleanup: launchd supervision preserved (manual_stop respected)"
            )
        else:
            log_engine("shutdown cleanup: standalone watchdog stopped (no launchd)")
    except Exception as e:
        log_engine(f"shutdown cleanup: watchdog stop error (continuing): {e}")

    orphans = kill_other_agent_processes(
        exclude_pid=os.getpid(), sigkill_survivors=True
    )
    if orphans:
        log_engine(
            f"shutdown cleanup: cleared {len(orphans)} duplicate agent process(es)"
        )

    try:
        from system.instance_lock import force_release_instance_lock

        force_release_instance_lock()
    except Exception as e:
        log_engine(f"shutdown cleanup: lock release error (continuing): {e}")

    if not skip_port_cleanup:
        try:
            import sys

            bind_port = _resolve_shutdown_port()
            mod = sys.modules.get("__main__")
            if mod is not None and hasattr(mod, "_force_cleanup_port"):
                mod._force_cleanup_port(bind_port)
            else:
                import main as _main

                _main._force_cleanup_port(bind_port)
        except Exception as e:
            log_engine(f"shutdown cleanup: port cleanup error (continuing): {e}")

    log_engine("shutdown cleanup: complete")


def _list_main_py_pids() -> list[int]:
    pids: list[int] = []
    try:
        result = subprocess.run(
            ["/usr/bin/pgrep", "-f", "src/main.py"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode != 0:
            return pids
        for pid_str in result.stdout.strip().splitlines():
            try:
                pids.append(int(pid_str.strip()))
            except ValueError:
                pass
    except Exception:
        pass
    return pids


def _resolve_shutdown_port() -> int:
    try:
        from system.boot.preflight_helpers import resolve_api_port

        return resolve_api_port()
    except Exception:
        import os

        try:
            return int(os.environ.get("IG_API_PORT", "8080"))
        except ValueError:
            return 8080


def _port_bound(port: int | None = None, *, timeout_sec: float = 1.0) -> bool:
    """True when something accepts TCP connections on loopback (agent API up)."""
    bind_port = _resolve_shutdown_port() if port is None else int(port)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(max(0.2, float(timeout_sec)))
            return s.connect_ex(("127.0.0.1", bind_port)) == 0
    except Exception:
        return False


def supervision_utility_paths() -> list[Path]:
    root = project_root()
    return [root / rel for rel in _SUPERVISION_UTILITY_REL_PATHS]


def supervision_utility_permission_issues() -> list[str]:
    """Return relative paths that exist but are not executable."""
    issues: list[str] = []
    for path in supervision_utility_paths():
        if not path.is_file():
            continue
        if not os.access(path, os.X_OK):
            issues.append(path.relative_to(project_root()).as_posix())
    return issues


def ensure_supervision_utilities_executable() -> tuple[bool, list[str]]:
    """
    chmod +x on supervision helpers so macOS launchd / spawn do not block them.
    Returns (all_ok, repaired_relative_paths).
    """
    repaired: list[str] = []
    for path in supervision_utility_paths():
        if not path.is_file():
            continue
        rel = path.relative_to(project_root()).as_posix()
        if os.access(path, os.X_OK):
            continue
        try:
            mode = path.stat().st_mode
            path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            repaired.append(rel)
        except OSError:
            pass
    remaining = supervision_utility_permission_issues()
    return (len(remaining) == 0, repaired)


def _instance_lock_holder_pid() -> int | None:
    from system.instance_lock import lock_path

    lock = lock_path()
    if not lock.is_file():
        return None
    try:
        raw = lock.read_text(encoding="utf-8").strip()
        holder = int(raw.split()[0]) if raw else 0
    except (ValueError, OSError):
        return None
    if holder <= 0:
        return None
    try:
        os.kill(holder, 0)
        return holder
    except OSError:
        return None


def _fetch_api_health(timeout: float = 3.0) -> dict | None:
    if os.environ.get("IG_AGENT_PYTEST") == "1":
        return None
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:8080/api/health",
            timeout=min(float(timeout), 5.0),
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, dict) else None
    except (
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
        OSError,
        ValueError,
    ):
        return None


def agent_fully_started(
    *,
    max_gate_age_sec: float = 120.0,
    stream_log_within_min: float = 10.0,
    require_trading_healthy: bool = True,
) -> tuple[bool, list[str]]:
    """Return (ok, issues) when startup completed and the agent is trading-ready."""
    issues: list[str] = []

    pids = _list_main_py_pids()
    if not pids:
        issues.append("main.py not running")
    elif len(pids) > 1:
        issues.append(f"duplicate main.py processes ({len(pids)})")

    if not _port_bound():
        issues.append(f"port {_resolve_shutdown_port()} not bound")

    lock_pid = _instance_lock_holder_pid()
    if lock_pid is None:
        issues.append("instance lock missing or stale")
    elif pids and lock_pid not in pids:
        issues.append("instance lock held by non-agent pid")

    try:
        from api.agent_health import _watchdog_active

        if not _watchdog_active():
            issues.append("watchdog not running")
    except Exception:
        issues.append("watchdog status unknown")

    health = _fetch_api_health()
    if health is None:
        issues.append("cannot reach /api/health")
    else:
        if not health.get("trading_loops_running"):
            issues.append("trading loops not running")
        if require_trading_healthy and not health.get("trading_healthy"):
            detail = ", ".join(health.get("issues") or []) or "unhealthy"
            issues.append(f"trading not healthy ({detail})")
        gate_age = health.get("last_gate_check_age_sec")
        if gate_age is None:
            issues.append("no gate check activity")
        elif float(gate_age) > max_gate_age_sec:
            issues.append(f"gate check stale ({int(float(gate_age))}s)")

    try:
        from system.pre_flight_checks import check_startup_stream_gate_log

        stream_result = check_startup_stream_gate_log(
            within_minutes=stream_log_within_min
        )
        if not stream_result.passed:
            issues.append("stream_ready not in recent engine.log")
    except Exception:
        issues.append("stream_ready log check failed")

    return (len(issues) == 0, issues)


def stopped_verification_checks(issues: list[str]) -> list[dict[str, object]]:
    """Structured checklist matching scripts/confirm_stopped.py."""
    try:
        from system.overnight_supervision import launchd_watchdog_active

        launchd_wd = launchd_watchdog_active()
    except Exception:
        launchd_wd = False

    mapping = [
        ("No main.py process", "main.py still running"),
        (
            "Launchd supervision preserved" if launchd_wd else "No watchdog process",
            "watchdog.sh still running",
        ),
        ("Port 8080 free", "port 8080 still bound"),
        ("No instance lock", "instance lock file present"),
        (
            "Watchdog supervising (launchd)" if launchd_wd else "No watchdog.pid",
            "watchdog.pid present",
        ),
    ]
    return [
        {
            "label": label,
            "ok": issue_key not in issues,
            "detail": issue_key if issue_key in issues else "",
        }
        for label, issue_key in mapping
    ]


def post_cleanup_shutdown_checks(*, exclude_pid: int) -> list[dict[str, object]]:
    """Immediate checks after perform_shutdown_cleanup (before process exit)."""
    checks: list[dict[str, object]] = []
    pids = _list_main_py_pids()
    only_self = not pids or pids == [exclude_pid]
    checks.append(
        {
            "label": "Cleanup completed",
            "ok": True,
            "detail": "streams, IG session, DB checkpoint",
        }
    )
    checks.append(
        {
            "label": "Trading process exiting",
            "ok": only_self,
            "detail": (
                f"pid {exclude_pid} shutting down"
                if exclude_pid in pids
                else "no unexpected main.py"
            ),
        }
    )
    try:
        from api.agent_health import _watchdog_active
        from system.overnight_supervision import launchd_watchdog_active

        launchd_wd = launchd_watchdog_active()
        watchdog_active = _watchdog_active()
    except Exception:
        launchd_wd = False
        watchdog_active = True
    if launchd_wd:
        checks.append(
            {
                "label": "Launchd supervision preserved",
                "ok": True,
                "detail": "watchdog job still loaded",
            }
        )
    else:
        checks.append(
            {
                "label": "Watchdog stopped",
                "ok": not watchdog_active,
                "detail": "" if not watchdog_active else "watchdog still active",
            }
        )
    port_bound = _port_bound()
    checks.append(
        {
            "label": "Port 8080 released",
            "ok": not port_bound,
            "detail": "" if not port_bound else "port still bound",
        }
    )
    from system.instance_lock import lock_path

    lock = lock_path()
    checks.append(
        {
            "label": "Instance lock released",
            "ok": not lock.is_file(),
            "detail": "" if not lock.is_file() else "lock file present",
        }
    )
    wd_pid = data_dir() / "watchdog.pid"
    try:
        from system.overnight_supervision import launchd_watchdog_active

        launchd_wd = launchd_watchdog_active()
    except Exception:
        launchd_wd = False
    if launchd_wd:
        checks.append(
            {
                "label": "Watchdog PID (launchd)",
                "ok": True,
                "detail": "expected while supervision loaded",
            }
        )
    else:
        checks.append(
            {
                "label": "Watchdog PID cleared",
                "ok": not wd_pid.is_file(),
                "detail": "" if not wd_pid.is_file() else "watchdog.pid present",
            }
        )
    return checks


def spawn_post_shutdown_verifier(parent_pid: int) -> None:
    """Detached process waits for agent exit then serves verify JSON on :8081."""
    root = project_root()
    script = root / "scripts" / "shutdown_verify_server.py"
    if not script.is_file():
        log_engine("shutdown verify: script missing — skipped")
        return
    ensure_supervision_utilities_executable()
    py = find_python_executable()
    try:
        log_path = data_dir() / "logs" / "shutdown_verify_spawn.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as spawn_log:
            spawn_log.write(
                f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} | spawn parent={parent_pid}\n"
            )
        err_log = data_dir() / "logs" / "shutdown_verify_stderr.log"
        err_fh = err_log.open("a", encoding="utf-8")
        subprocess.Popen(
            [
                py,
                str(script),
                "--parent-pid",
                str(parent_pid),
            ],
            cwd=str(root),
            env={
                **dict(os.environ),
                "IG_AGENT_ROOT": str(root),
                "PYTHONPATH": str(root / "src"),
            },
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=err_fh,
        )
        log_engine(f"shutdown verify: spawned post-exit checker (parent={parent_pid})")
    except Exception as e:
        log_engine(f"shutdown verify: spawn failed: {type(e).__name__}: {e}")


def repair_stale_watchdog_after_stop() -> tuple[bool, str]:
    """Kill orphaned watchdog.sh when launchd is not supervising."""
    try:
        from system.overnight_supervision import launchd_watchdog_active

        if launchd_watchdog_active():
            return True, "launchd supervision active — no repair needed"
    except Exception:
        pass
    try:
        from api.agent_health import stop_watchdog

        stop_watchdog(preserve_launchd=False)
    except Exception as e:
        return False, f"watchdog repair failed: {type(e).__name__}: {e}"
    ok, issues = agent_fully_stopped()
    if ok:
        return True, "watchdog stopped and pid cleared"
    return False, ", ".join(issues) if issues else "watchdog still present"


def agent_fully_stopped(
    *, preserve_launchd_supervision: bool = True
) -> tuple[bool, list[str]]:
    """Return (ok, issues) when the trading agent process has fully exited."""
    issues: list[str] = []

    if _list_main_py_pids():
        issues.append("main.py still running")

    launchd_wd = False
    if preserve_launchd_supervision:
        try:
            from system.overnight_supervision import launchd_watchdog_active

            launchd_wd = launchd_watchdog_active()
        except Exception:
            pass

    if not launchd_wd:
        try:
            result = subprocess.run(
                ["/usr/bin/pgrep", "-f", "scripts/watchdog.sh"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0 and result.stdout.strip():
                issues.append("watchdog.sh still running")
        except Exception:
            pass

    if _port_bound():
        issues.append("port 8080 still bound")

    from system.instance_lock import lock_path

    lock = lock_path()
    if lock.is_file():
        issues.append("instance lock file present")

    wd_pid = data_dir() / "watchdog.pid"
    if wd_pid.is_file() and not launchd_wd:
        issues.append("watchdog.pid present")

    return (len(issues) == 0, issues)
