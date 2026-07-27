"""
Runtime control hooks — trading loop start/stop from FastAPI (Step 13).

Dashboard WebSocket ticks attach supervision_drift via cached runtime fields
from api.agent_health (refreshed in the health-cache background thread).
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.paths import project_root

_loop: Any | None = None
_paused: bool = False
_lock = threading.Lock()

# Learning-loop Step 2 / A2 — CFD marker modes that mean "no new CFD entries".
_A2_CFD_PAUSE_MODES = frozenset({"A2_SB_ONLY", "OPERATOR_HALT_BLEED"})


def register_trading_loop(loop: Any | None) -> None:
    global _loop
    with _lock:
        _loop = loop


def reset_agent_control_for_tests() -> None:
    """Reset module state between pytest cases."""
    global _loop, _paused
    with _lock:
        _loop = None
        _paused = False


def get_trading_loop() -> Any | None:
    with _lock:
        return _loop


def is_paused() -> bool:
    with _lock:
        return _paused


def _bust_health_pause_cache() -> None:
    """Keep /api/health trading_paused truthful after stop/start (no 5s lie)."""
    try:
        from api import agent_health

        agent_health.overlay_pause_on_health_cache(is_paused())
    except Exception:
        pass


def _persist_process_pause(*, paused: bool, reason: str) -> None:
    """Persist process pause to this engine's state_dir trading_paused.json.

    Dual-port: each engine writes its own lane (state_cfd / state_sb).
    Shared desk_dev_pause dual-writes all lanes separately.
    """
    try:
        from system.paths import state_dir

        path = Path(state_dir()) / "trading_paused.json"
        if not paused:
            if path.is_file():
                path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "active": True,
                    "reason": str(reason or "api_stop"),
                    "ts": __import__("time").time(),
                    "source": "api.agent_control",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def this_process_is_cfd_sniper() -> bool:
    """True for QUANT_SNIPER / Z6BAH4 (:8080) — A2 CFD pause scope."""
    origin = os.environ.get("IG_ENGINE_ORIGIN", "").strip().upper()
    if origin == "QUANT_SNIPER":
        return True
    if origin == "MACRO_SENTINEL":
        return False
    acct = os.environ.get("IG_ACCOUNT_ID", "").strip().upper()
    if acct == "Z6BAH4":
        return True
    if acct == "Z6BAH3":
        return False
    try:
        from system.paths import engine_state_subdir

        return engine_state_subdir() == "state_cfd"
    except Exception:
        return False


def a2_cfd_entries_paused_marker_path() -> Path:
    from system.paths import data_dir

    return data_dir() / "state_cfd" / "a2_entries_paused.json"


def a2_cfd_entries_paused_marker_active() -> bool:
    """
    Operator marker for learning-loop Step 2 (A2 SB-only).

    Historically advisory/docs-only; callers of ``new_entries_hard_blocked`` now
    treat an active marker as a hard entry veto on the CFD port.
    """
    path = a2_cfd_entries_paused_marker_path()
    if not path.is_file():
        alt = path.parent.parent / "state" / "a2_entries_paused.json"
        path = alt if alt.is_file() else path
    if not path.is_file():
        return False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # Unreadable marker on disk → fail closed for CFD; SB ignores via caller.
        return True
    if not isinstance(raw, dict):
        return True
    if raw.get("active") is True:
        return True
    mode = str(raw.get("mode") or "").strip().upper()
    if mode in _A2_CFD_PAUSE_MODES and raw.get("active") is not False:
        return True
    return False


def new_entries_hard_blocked() -> tuple[bool, str]:
    """
    Fail-closed entry veto for this process/port.

    Blocks when:
      • ``/api/stop`` process pause is set (``is_paused``), or
      • this is CFD QUANT_SNIPER and ``state_cfd/a2_entries_paused.json`` is active.

    Exceptions while reading pause/marker state → blocked (never fail-open).
    """
    try:
        if is_paused():
            return True, "api_trading_paused"
    except Exception as exc:
        return True, f"api_trading_paused_fail_closed:{type(exc).__name__}"

    try:
        if this_process_is_cfd_sniper() and a2_cfd_entries_paused_marker_active():
            return True, "a2_entries_paused"
    except Exception as exc:
        try:
            if this_process_is_cfd_sniper():
                return True, f"a2_entries_paused_fail_closed:{type(exc).__name__}"
        except Exception:
            return True, f"a2_entries_paused_fail_closed:{type(exc).__name__}"
    return False, ""


def engage_pause_from_a2_marker_if_needed() -> dict[str, Any]:
    """
    Persist A2 intent into process-local pause after CFD restart.

    ``is_paused`` resets to False on boot by design; the marker file is the
    durable operator intent. Re-arm ``_paused`` so ``trading_paused`` health
    stays true without requiring a manual ``POST /api/stop`` race.
    """
    global _paused
    if not this_process_is_cfd_sniper():
        return {"ok": True, "action": "not_cfd", "paused": is_paused()}
    if not a2_cfd_entries_paused_marker_active():
        return {"ok": True, "action": "marker_inactive", "paused": is_paused()}
    with _lock:
        already = _paused
        _paused = True
        loop = _loop
    if already:
        return {"ok": True, "action": "already_paused", "paused": True}
    if loop is not None:
        try:
            if loop.is_running():
                loop.stop()
                log_engine("api: A2 marker → trading loop stopped (entries hard-blocked)")
        except Exception as exc:
            log_engine(
                f"api: A2 marker loop.stop failed: {type(exc).__name__}: {exc}"
            )
    log_engine("api: A2 marker → process pause engaged (learning_loop_step2_cfd)")
    return {"ok": True, "action": "paused_from_a2_marker", "paused": True}


def is_trading_running() -> bool:
    """True when real TradingLoop threads are active (not API-paused)."""
    from system.trading_plane_readiness import is_trading_plane_live

    return is_trading_plane_live()


def enrich_tick_runtime(tick: dict[str, Any]) -> dict[str, Any]:
    """Attach live trading-loop status for dashboard / WebSocket consumers."""
    from api.agent_health import get_runtime_tick_fields

    out = dict(tick)
    out.update(get_runtime_tick_fields())
    out["trading_paused"] = is_paused()
    loops = is_trading_running()
    out["trading_loops_running"] = loops
    try:
        from system.rest_api_budget import get_rest_api_budget
        from system.rest_poll_status import snapshot_fields as rest_poll_fields

        metrics = get_rest_api_budget().metrics()
        util = float(metrics.get("hard_cap_utilization_pct") or 0)
        out["rest_budget_pct"] = max(0, 100 - int(util))
        out["rest_calls_min"] = out.get("rest_calls_min") or metrics.get(
            "calls_last_minute", 0
        )
        out.update(rest_poll_fields())
        # Surface REST pressure for FE sniper-arm truth (CRITICAL → SUPPRESSED)
        level = str(metrics.get("pressure_level") or "").upper()
        if not level:
            level = (
                "CRITICAL"
                if util >= 95
                else "HIGH"
                if util >= 80
                else "ELEVATED"
                if util >= 60
                else "OK"
            )
        out["rest_pressure_level"] = level
        out["rest_pressure"] = level in ("ELEVATED", "HIGH", "CRITICAL")
    except Exception:
        pass
    try:
        from runtime.trading_path_readiness import compute_trading_path_readiness

        path = compute_trading_path_readiness()
        out["trading_path_live"] = bool(path.get("trading_path_live"))
        out["trading_path_badge"] = str(path.get("badge") or "")
        out["entries_blocked"] = not bool(path.get("trading_path_live")) or bool(
            out.get("rest_pressure")
        )
    except Exception:
        out.setdefault("trading_path_live", False)
        out.setdefault("entries_blocked", True)
    if "trading_healthy" not in out:
        out["trading_healthy"] = loops and not is_paused()
    try:
        from execution.risk_manager import get_volatility_bracket_snapshot

        vb = get_volatility_bracket_snapshot()
        if vb:
            out.setdefault("institutional", {})["volatility_bracket"] = vb
    except Exception:
        pass
    try:
        from datetime import datetime, timezone, timedelta

        _bst = timezone(timedelta(hours=1))
        now_bst = datetime.now(_bst)
        wd, hh = now_bst.weekday(), now_bst.hour
        weekend = (wd == 4 and hh >= 22) or wd == 5 or (wd == 6 and hh < 21)
        out["exchange_online"] = not weekend
    except Exception:
        out["exchange_online"] = True
    return out


def start_trading() -> dict[str, Any]:
    global _paused
    with _lock:
        was_paused = _paused
        _paused = False
        loop = _loop
    _persist_process_pause(paused=False, reason="api_start")
    _bust_health_pause_cache()
    if loop is None:
        return {
            "ok": False,
            "error": "trading loop not registered",
            "trading_paused": False,
        }
    if loop.is_running():
        return {
            "ok": True,
            "status": "already_running",
            "trading_paused": False,
            "was_paused": was_paused,
        }
    loop.start()
    log_engine("api: trading loop started")
    return {"ok": True, "status": "started", "trading_paused": False}


def stop_trading() -> dict[str, Any]:
    global _paused
    with _lock:
        was_paused = _paused
        _paused = True
        loop = _loop
    _persist_process_pause(paused=True, reason="api_stop")
    _bust_health_pause_cache()
    if loop is None:
        return {
            "ok": False,
            "error": "trading loop not registered",
            "trading_paused": True,
        }
    # Process pause is the SoT for trading_paused — even when the registered
    # orchestrator is already not running (status historically said
    # already_stopped while health could still cache trading_paused=false).
    if not loop.is_running():
        return {
            "ok": True,
            "status": "already_paused" if was_paused else "paused",
            "trading_paused": True,
            "loop_running": False,
        }
    loop.stop()
    log_engine("api: trading loop stopped")
    try:
        from system.telegram_notifier import send_critical_alert

        send_critical_alert("⚠️ Trading loops STOPPED — no trades firing")
    except Exception as e:
        log_engine(f"telegram loop-stop alert failed: {type(e).__name__}: {e}")
    return {
        "ok": True,
        "status": "stopped",
        "trading_paused": True,
        "loop_running": False,
    }


def run_emergency_stop() -> dict[str, Any]:
    script = project_root() / "scripts" / "emergency_stop.sh"
    if not script.is_file():
        return {"ok": False, "error": f"missing {script}"}
    try:
        proc = subprocess.run(
            ["/bin/bash", str(script)],
            cwd=str(project_root()),
            capture_output=True,
            text=True,
            timeout=120,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "")[-2000:],
            "stderr": (proc.stderr or "")[-2000:],
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
