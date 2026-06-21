"""
Process-isolated dual-track orchestrator — Live Vanguard + Shadow Simulator.

Spawns two entirely independent interpreter processes that communicate exclusively
via native ``multiprocessing.shared_memory`` segments. The native
``ParallelTrackSupervisor`` loop replaces external shell watchdog parsing for
multi-track PID health.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception

_PID_REGISTRY = Path("/tmp/ig_agent_parallel.pids.json")
_LIVE_LOG = Path("/tmp/ig_agent.live.log")
_SHADOW_LOG = Path("/tmp/ig_agent.shadow.log")
_SUPERVISOR_POLL_SEC = 5.0
_LIVE_PORT = 8080
_SHADOW_PORT = 9199
_COCKPIT_PORT = 8787

NIGHT_MATRIX_EPICS: tuple[str, ...] = (
    "CS.D.CFPGOLD.CFP.IP",
    "IX.D.DOW.IFM.IP",
    "IX.D.NIKKEI.IFM.IP",
    "CS.D.EURUSD.CFD.IP",
)


def _project_root() -> Path:
    from system.paths import project_root

    return project_root()


def _python_executable() -> str:
    venv = _project_root() / ".venv" / "bin" / "python3"
    if venv.is_file():
        return str(venv)
    return sys.executable


def pid_alive(pid: int) -> bool:
    """Non-blocking liveness probe — ``os.kill(pid, 0)``."""
    if pid <= 0:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def _port_listening(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        try:
            sock.connect(("127.0.0.1", int(port)))
            return True
        except OSError:
            return False


def configure_live_vanguard_env(*, cycle_sec: int) -> None:
    """Production live track — port 8080, live broker REST, no mock feed."""
    from system.identity.boot_profile import BootProfile, apply_boot_profile

    apply_boot_profile(BootProfile.for_live(cycle_sec=int(cycle_sec)))
    os.environ["IG_COCKPIT_ISOLATED_EXTERNAL"] = "1"


def configure_shadow_simulator_env(*, cycle_sec: int) -> None:
    """Isolated shadow track — port 9199, mock replay, weight training."""
    from system.identity.boot_profile import BootProfile, apply_boot_profile

    apply_boot_profile(BootProfile.for_shadow(cycle_sec=int(cycle_sec)))


def write_pid_registry(
    *,
    live_pid: int,
    shadow_pid: int,
    orchestrator_pid: int,
    cockpit_pid: int | None = None,
) -> None:
    payload: dict[str, Any] = {
        "live_pid": int(live_pid),
        "shadow_pid": int(shadow_pid),
        "orchestrator_pid": int(orchestrator_pid),
        "started_at_epoch": time.time(),
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if cockpit_pid is not None and int(cockpit_pid) > 0:
        payload["cockpit_pid"] = int(cockpit_pid)
    _PID_REGISTRY.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def read_pid_registry() -> dict[str, Any]:
    if not _PID_REGISTRY.is_file():
        return {}
    try:
        data = json.loads(_PID_REGISTRY.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _base_child_env() -> dict[str, str]:
    keep = ("HOME", "PATH", "USER", "SHELL", "LANG", "LC_ALL")
    env: dict[str, str] = {}
    for key in keep:
        val = os.environ.get(key, "")
        if val:
            env[key] = val
    env["PYTHONPATH"] = "src"
    env["IG_AGENT_ROOT"] = str(_project_root())
    return env


def spawn_isolated_track(*, track: str, cycle_sec: int, log_path: Path) -> subprocess.Popen[Any]:
    """Launch one detached isolated track process."""
    root = _project_root()
    entry = root / "src" / "main.py"
    env = _base_child_env()
    env["IG_ORCHESTRATOR_CHILD"] = "1"
    if track == "live":
        configure_live_vanguard_env(cycle_sec=cycle_sec)
    else:
        configure_shadow_simulator_env(cycle_sec=cycle_sec)
    env.update(
        {
            k: v
            for k, v in os.environ.items()
            if k.startswith("IG_") or k in ("NODE_ENV", "PYTHONPATH", "IG_AGENT_ROOT")
        }
    )

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fd = open(log_path, "a", encoding="utf-8", buffering=1)

    proc = subprocess.Popen(
        [
            _python_executable(),
            str(entry),
            f"--isolated-track={track}",
            f"--daemon-cycle={int(cycle_sec)}",
        ],
        cwd=str(root),
        env=env,
        stdout=log_fd,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log_engine(
        f"ProcessOrchestrator: spawned {track} track pid={proc.pid} "
        f"port={env.get('IG_API_PORT')} log={log_path}"
    )
    return proc


class ParallelTrackSupervisor:
    """
    Native multi-track supervisor — pure Python PID/port evaluation.

    Shadow death triggers shadow-only respawn. Live Vanguard is never signalled.
    """

    def __init__(
        self,
        *,
        cycle_sec: int,
        live_pid: int,
        shadow_pid: int,
        cockpit_pid: int | None = None,
        poll_sec: float = _SUPERVISOR_POLL_SEC,
    ) -> None:
        self._cycle_sec = int(cycle_sec)
        self._live_pid = int(live_pid)
        self._shadow_pid = int(shadow_pid)
        self._cockpit_pid = int(cockpit_pid) if cockpit_pid else None
        self._poll_sec = max(1.0, float(poll_sec))
        self._stop = threading.Event()
        self._shadow_respawn_count = 0
        self._cockpit_respawn_count = 0
        self._thread: threading.Thread | None = None

    @property
    def live_pid(self) -> int:
        return self._live_pid

    @property
    def shadow_pid(self) -> int:
        return self._shadow_pid

    @property
    def cockpit_pid(self) -> int | None:
        return self._cockpit_pid

    def snapshot(self) -> dict[str, Any]:
        return {
            "live_pid": self._live_pid,
            "shadow_pid": self._shadow_pid,
            "cockpit_pid": self._cockpit_pid,
            "live_alive": pid_alive(self._live_pid),
            "shadow_alive": pid_alive(self._shadow_pid),
            "live_port_listening": _port_listening(_LIVE_PORT),
            "shadow_port_listening": _port_listening(_SHADOW_PORT),
            "cockpit_port_listening": _port_listening(_COCKPIT_PORT),
            "shadow_respawn_count": self._shadow_respawn_count,
            "cockpit_respawn_count": self._cockpit_respawn_count,
        }

    def _emit_shadow_death_alert(self, *, dead_pid: int) -> None:
        log_engine(
            "ParallelTrackSupervisor: ALERT ShadowSimulator silent death detected "
            f"pid={dead_pid} port=:{_SHADOW_PORT} — spawning pristine replacement "
            "(Live Vanguard untouched)"
        )
        try:
            from system.telegram_notifier import send_critical_alert

            send_critical_alert(
                f"Shadow track died (pid={dead_pid}) — native supervisor respawning :9199"
            )
        except Exception:
            pass

    def _respawn_shadow(self) -> None:
        dead_pid = self._shadow_pid
        self._emit_shadow_death_alert(dead_pid=dead_pid)
        proc = spawn_isolated_track(
            track="shadow",
            cycle_sec=self._cycle_sec,
            log_path=_SHADOW_LOG,
        )
        self._shadow_pid = int(proc.pid)
        self._shadow_respawn_count += 1
        write_pid_registry(
            live_pid=self._live_pid,
            shadow_pid=self._shadow_pid,
            orchestrator_pid=os.getpid(),
            cockpit_pid=self._cockpit_pid,
        )
        log_engine(
            "ParallelTrackSupervisor: ShadowSimulator replacement online "
            f"new_pid={self._shadow_pid} respawns={self._shadow_respawn_count}"
        )

    def _respawn_cockpit(self) -> None:
        from api.isolated_cockpit_server import spawn_isolated_cockpit_process

        log_engine(
            "ParallelTrackSupervisor: isolated Flight Deck :8787 offline — respawning "
            "(read-only SHM consumer, Live Vanguard untouched)"
        )
        self._cockpit_pid = spawn_isolated_cockpit_process(port=_COCKPIT_PORT)
        self._cockpit_respawn_count += 1
        write_pid_registry(
            live_pid=self._live_pid,
            shadow_pid=self._shadow_pid,
            orchestrator_pid=os.getpid(),
            cockpit_pid=self._cockpit_pid,
        )

    def tick_once(self) -> dict[str, Any]:
        """Evaluate both tracks once — non-blocking."""
        if not pid_alive(self._live_pid) and not _port_listening(_LIVE_PORT):
            log_engine(
                f"ParallelTrackSupervisor: CRITICAL Live Vanguard pid={self._live_pid} "
                f"not alive on :{_LIVE_PORT} — supervisor will not auto-respawn live "
                "(launchd / operator restart required)"
            )

        if not pid_alive(self._shadow_pid) or not _port_listening(_SHADOW_PORT):
            self._respawn_shadow()

        if self._cockpit_pid is not None:
            if not pid_alive(self._cockpit_pid) or not _port_listening(_COCKPIT_PORT):
                self._respawn_cockpit()
        elif not _port_listening(_COCKPIT_PORT):
            self._respawn_cockpit()

        return self.snapshot()

    def _loop(self) -> None:
        log_engine(
            "ParallelTrackSupervisor: native loop armed "
            f"live_pid={self._live_pid} shadow_pid={self._shadow_pid} "
            f"cockpit_pid={self._cockpit_pid} poll={self._poll_sec}s"
        )
        while not self._stop.wait(self._poll_sec):
            try:
                self.tick_once()
            except Exception as exc:
                log_guarded_exception("parallel_track_supervisor_tick", exc)

    def start_background(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="ParallelTrackSupervisor",
            daemon=False,
        )
        self._thread.start()

    def run_forever(self) -> None:
        """Blocking supervisor loop for orchestrator grandchild process."""
        log_engine(
            "ParallelTrackSupervisor: entering monotonic native supervision "
            f"(live=:{_LIVE_PORT} shadow=:{_SHADOW_PORT} cockpit=:{_COCKPIT_PORT})"
        )
        while not self._stop.is_set():
            try:
                self.tick_once()
            except Exception as exc:
                log_guarded_exception("parallel_track_supervisor_tick", exc)
            if self._stop.wait(self._poll_sec):
                break

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)


def _sync_manual_stop_for_watchdog(*, source: str) -> None:
    """Hold launchd watchdog during parallel dual-track launch (legacy + apex paths)."""
    try:
        from system.shutdown_cleanup import mark_manual_stop

        mark_manual_stop(source=source)
    except Exception as exc:
        log_guarded_exception("parallel_launch_manual_stop", exc)

    try:
        payload = json.dumps({"ts": time.time(), "source": source})
        legacy = _project_root() / "src" / "data" / "state" / "manual_stop.json"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(payload, encoding="utf-8")
    except Exception as exc:
        log_guarded_exception("parallel_launch_manual_stop_legacy", exc)


def launch_dual_tracks_detached(*, cycle_sec: int) -> tuple[int, int]:
    """
    Spawn Live Vanguard (:8080) and Shadow Simulator (:9199) as sibling processes.

    Returns ``(live_pid, shadow_pid)``.
    """
    from system.boot.port_eviction import reclaim_and_wait
    from system.daemon_cycle_kernel import detach_daemon_runtime

    _sync_manual_stop_for_watchdog(source="parallel_dual_launch")

    os.environ.setdefault("IG_AGENT_ROOT", str(_project_root()))
    detach_daemon_runtime(log_path=_LIVE_LOG)

    for port in (_LIVE_PORT, _SHADOW_PORT):
        reclaimed = reclaim_and_wait(port, force=True)
        log_engine(
            f"ProcessOrchestrator: port :{port} reclaimed={reclaimed} before dual spawn"
        )

    live_proc = spawn_isolated_track(track="live", cycle_sec=cycle_sec, log_path=_LIVE_LOG)
    time.sleep(0.5)
    shadow_proc = spawn_isolated_track(
        track="shadow", cycle_sec=cycle_sec, log_path=_SHADOW_LOG
    )

    from api.isolated_cockpit_server import spawn_isolated_cockpit_process

    cockpit_pid = spawn_isolated_cockpit_process(port=_COCKPIT_PORT)

    write_pid_registry(
        live_pid=live_proc.pid,
        shadow_pid=shadow_proc.pid,
        orchestrator_pid=os.getpid(),
        cockpit_pid=cockpit_pid,
    )
    log_engine(
        "ProcessOrchestrator: dual tracks armed "
        f"live_pid={live_proc.pid} shadow_pid={shadow_proc.pid} "
        f"cockpit_pid={cockpit_pid} cycle={cycle_sec}s"
    )
    return live_proc.pid, shadow_proc.pid


def run_parallel_supervisor_forever(*, cycle_sec: int, live_pid: int, shadow_pid: int) -> None:
    """Entry for orchestrator grandchild — native supervision until SIGTERM."""
    registry = read_pid_registry()
    cockpit_raw = registry.get("cockpit_pid")
    cockpit_pid = int(cockpit_raw) if cockpit_raw is not None else None
    supervisor = ParallelTrackSupervisor(
        cycle_sec=int(cycle_sec),
        live_pid=int(live_pid),
        shadow_pid=int(shadow_pid),
        cockpit_pid=cockpit_pid,
    )
    supervisor.run_forever()


def start_shadow_historical_replayer(*, loop: bool = True) -> None:
    """Shadow-only continuous 5-day archive ingestion — separate thread, not shared with live."""
    import threading

    def _runner() -> None:
        try:
            from simulation.historical_replayer import default_replay_path, start_background_replay
            from system.market_data_hub import get_market_data_hub

            path = default_replay_path()
            hub = get_market_data_hub()
            replayer = start_background_replay(
                path,
                speed=float(os.environ.get("IG_REPLAY_SPEED", "20")),
                hub=hub,
                loop=loop,
            )
            log_engine(
                f"ShadowSimulator: HistoricalReplayer loop armed path={path} "
                f"ticks={len(getattr(replayer, '_ticks', []) or [])}"
            )
            while True:
                time.sleep(3600.0)
        except Exception as exc:
            log_guarded_exception("shadow_historical_replayer", exc)

    threading.Thread(
        target=_runner,
        name="shadow-historical-replayer",
        daemon=True,
    ).start()


def apply_live_weight_transfer_if_approved() -> bool:
    """Live vanguard — consume shadow-approved weights from shared memory."""
    try:
        from system.identity.weight_transfer_bridge import get_weight_transfer_bridge
        from system.ml.twin_engine_core import ModelWeights, get_twin_engine_core

        candidate = get_weight_transfer_bridge(create=False).read_candidate()
        if candidate is None:
            return False
        weights_raw = candidate.get("weights") or {}
        model = ModelWeights(
            bias=float(weights_raw.get("bias") or 0.0),
            coeffs={
                k: float((weights_raw.get("coeffs") or {}).get(k, 0.0))
                for k in ("adjusted_score", "rsi", "atr_ratio")
            },
            version=int(weights_raw.get("version") or 0),
            trained_at=float(weights_raw.get("trained_at") or time.time()),
        )
        get_twin_engine_core().live.atomic_swap(model)
        log_engine(
            "ProcessOrchestrator: live applied shadow weight transfer "
            f"edge={float(candidate.get('edge') or 0.0):.4f}"
        )
        return True
    except Exception as exc:
        log_guarded_exception("live_weight_transfer_apply", exc)
        return False


def emergency_kill_all_tracks() -> dict[str, Any]:
    """SIGTERM both tracks, unlink locks, wipe shared RAM segments."""
    registry = read_pid_registry()
    killed: list[int] = []
    for key in ("live_pid", "shadow_pid", "cockpit_pid"):
        raw = registry.get(key)
        if raw is None:
            continue
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except OSError:
            pass

    try:
        from system.identity.instance_lock import force_release_instance_lock

        force_release_instance_lock()
    except Exception:
        pass

    for port in (_LIVE_PORT, _SHADOW_PORT):
        try:
            from system.identity.app_identity import RuntimeIdentity

            lock = RuntimeIdentity.get_lock_path(port)
            if lock.is_file():
                lock.unlink(missing_ok=True)
        except Exception as exc:
            log_guarded_exception("emergency_lock_unlink", exc)

    try:
        from system.identity.shared_memory_bridge import reset_shared_memory_bridge
        from system.identity.state_cache import reset_live_state_cache
        from system.identity.weight_transfer_bridge import reset_weight_transfer_bridge

        reset_live_state_cache()
        reset_shared_memory_bridge(unlink=True, track="live")
        reset_shared_memory_bridge(unlink=True, track="shadow")
        reset_weight_transfer_bridge(unlink=True)
    except Exception as exc:
        log_guarded_exception("emergency_shm_wipe", exc)

    try:
        _PID_REGISTRY.unlink(missing_ok=True)
    except OSError:
        pass

    return {"killed_pids": killed, "registry_cleared": True}
