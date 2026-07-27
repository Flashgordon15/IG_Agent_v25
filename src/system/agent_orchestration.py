"""
v33 Autonomous AI-Agent Self-Healing Orchestrator.

Polls dual-port health (:8080 / :8081), classifies log faults, and applies ONLY
allowlisted remediations — never auto-patches application source from log regex.

Allowlist:
  - Write diagnostics_fault.json (classification + recommended patch plan)
  - Clear stale session locks (Z6BAH3 / Z6BAH4)
  - Fail-closed health flag (in-process feed health + shared deploy_hold)
  - Optional v32_runtime_start.sh start OR single-engine relaunch (cooldown capped)
  - Optional patch *proposal* under diagnostics/ (no hot-path mutation)
  - Executive REST rate-smoothing (double rest_poll intervals + soft socket flush)
  - REST_PRESSURE_HIGH + entries_paused: safe token/lock purge + soft reauth

Never: kill -9, raise RestApiBudget hard caps / daily loss, arbitrary code edits.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from system.engine_log import log_engine
from system.paths import data_dir, project_root

POLL_SEC = 2.0
HEAL_WINDOW_SEC = 300.0
HEAL_MAX_ATTEMPTS = 2
CFD_PORT = 8080
SB_PORT = 8081
CFD_ACCOUNT = "Z6BAH4"
SB_ACCOUNT = "Z6BAH3"
CFD_ORIGIN = "QUANT_SNIPER"
SB_ORIGIN = "MACRO_SENTINEL"
LOG_TAIL_LINES = 120
BOOT_GRACE_SEC = 180.0
BOOT_STALE_SOT_OVERRIDE_SEC = 60.0
BOOT_STALE_OVERRIDE_COOLDOWN_SEC = 120.0
# Executive REST back-off after more than N consecutive HIGH/CRITICAL ticks.
REST_HIGH_TICKS_BEFORE_BACKOFF = 3
REST_PRESSURE_HIGH_LEVELS = frozenset({"HIGH", "CRITICAL", "REST_PRESSURE_HIGH"})
_CAP_BREACH_PAUSE_MARKERS = ("cap_breach", "stability_harness_cap_breach")
_BOOT_STAGE_PATTERN = re.compile(
    r"STAGE_[678]_|boot:stage_running|immutable_boot:|async_warmup|deferred_desktop_boot",
    re.I,
)
_INTERVAL_UNIT_RE = re.compile(
    r"^\s*(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>ms|s|sec|secs|seconds|m|min|mins)?\s*$",
    re.I,
)
# Safe token/session cache globs — never learning DB / SoT / journal.
_SAFE_TOKEN_CACHE_GLOBS = (
    "ig_session_tokens*.json",
    "ig_rest_session*.json",
)
_SAFE_STALE_LOCK_NAMES = (
    ".ig_agent_v29.lock",
    ".ig_agent_v31.lock",
    ".ig_agent_v25.lock",
)

_LOCK = threading.RLock()
_STOP = threading.Event()
_THREAD: threading.Thread | None = None
_STARTED = False

_heal_attempt_mono: list[float] = []
_healing_active = False
_last_fault: dict[str, Any] | None = None
_last_tick_at = 0.0
_port_health: dict[str, Any] = {}
_orchestrator_started_mono = time.monotonic()
_boot_stale_override_last_mono = 0.0
_rest_high_consecutive = 0
_rest_backoff_generation = 0
_rest_poll_intervals: dict[str, Any] = {}
_last_rest_heal: dict[str, Any] | None = None
_operational_status: str = "unknown"
_last_mutex_reconcile: dict[str, Any] | None = None
AMBIGUOUS_ORDER_MUTEX_SEC = 5.0


class FaultClass(str, Enum):
    ENGINE_DROP = "engine_drop"
    HTTP_429 = "http_429"
    STALE_LOCK_COLLISION = "stale_lock_collision"
    STAGE_4_BOOT_CLIFF = "stage_4_boot_cliff"
    PORT_OFFLINE = "port_offline"
    LOG_ERROR_SPIKE = "log_error_spike"
    UNKNOWN = "unknown"


@dataclass
class FaultReport:
    classification: FaultClass
    engine: str
    port: int
    detail: str
    log_excerpt: list[str] = field(default_factory=list)
    recommended_plan: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification.value,
            "engine": self.engine,
            "port": self.port,
            "detail": self.detail,
            "log_excerpt": self.log_excerpt[-8:],
            "recommended_plan": self.recommended_plan,
            "ts": time.time(),
            "auto_patch_allowed": False,
        }


def should_run_orchestrator() -> bool:
    """Run when explicitly armed or as CFD primary in v32 dual-port mode."""
    if os.environ.get("IG_AGENT_PYTEST", "").strip() == "1":
        return os.environ.get("IG_AGENT_ORCHESTRATOR", "").strip() == "1"
    if os.environ.get("IG_TEST_HARNESS", "").strip() == "1":
        return False
    if os.environ.get("IG_AGENT_ORCHESTRATOR", "").strip() == "1":
        return True
    if os.environ.get("IG_V32_DUAL_PORT", "").strip() != "1":
        return False
    port = int(os.environ.get("IG_API_PORT", os.environ.get("PORT", "8080")))
    return port == CFD_PORT


def _diagnostics_fault_path() -> Path:
    root = Path(data_dir())
    root.mkdir(parents=True, exist_ok=True)
    return root / "diagnostics_fault.json"


def _orchestrator_state_path() -> Path:
    root = Path(data_dir())
    root.mkdir(parents=True, exist_ok=True)
    return root / "state" / "orchestrator_state.json"


def _orchestrator_heal_paused_path() -> Path:
    return Path(data_dir()) / "state" / "orchestrator_heal_paused.json"


def orchestrator_heal_paused() -> bool:
    """Operator pause — skip fault/heal ticks (boot recovery, manual desk work)."""
    if os.environ.get("IG_AGENT_ORCHESTRATOR_HEAL_PAUSE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return True
    path = _orchestrator_heal_paused_path()
    if not path.is_file():
        return False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return bool(raw.get("active"))
    except (OSError, json.JSONDecodeError, TypeError):
        return False


def set_orchestrator_heal_pause(*, active: bool, reason: str = "operator") -> None:
    path = _orchestrator_heal_paused_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not active:
        path.unlink(missing_ok=True)
        return
    path.write_text(
        json.dumps({"active": True, "reason": reason, "ts": time.time()}, indent=2)
        + "\n",
        encoding="utf-8",
    )


def _port_listener_alive(port: int) -> bool:
    try:
        result = subprocess.run(
            ["lsof", "-iTCP", f":{int(port)}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return bool(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def _engine_in_boot_grace(
    *,
    port: int,
    online: bool,
    ok: bool,
    log_path: Path | None,
    health: dict[str, Any],
) -> bool:
    """Do not heal/relaunch while a twin is mid STAGE_6–8 or port-bound but warming."""
    if ok:
        return False
    payload = health.get("payload") or {}
    boot = payload.get("boot_metrics") or {}
    sys_state = payload.get("system_state") or boot.get("system_state") or {}
    phase = str(sys_state.get("phase") or "").upper()
    if phase and phase != "G5" and not sys_state.get("ready"):
        return True
    if boot.get("ready") is False and (online or _port_listener_alive(port)):
        return True
    if not online and _port_listener_alive(port):
        return True
    lines = tail_log_lines(log_path, max_lines=40)
    if lines and _BOOT_STAGE_PATTERN.search("\n".join(lines[-24:])):
        return True
    if (online or _port_listener_alive(port)) and not payload.get("trading_healthy"):
        started_at = sys_state.get("started_at_epoch")
        if isinstance(started_at, (int, float)) and (time.time() - float(started_at)) < BOOT_GRACE_SEC:
            return True
    return False


def _diagnostics_proposal_dir() -> Path:
    d = Path(data_dir()) / "diagnostics"
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_engine_log_paths() -> dict[str, Path | None]:
    """CFD/SB log paths — prefer v32_runtime_start data-root layout."""
    root = Path(data_dir())
    proj = project_root()
    candidates: dict[str, list[Path]] = {
        "cfd": [
            root / "logs" / "v32_cfd.log",
            proj / "logs" / "v32_cfd.log",
        ],
        "sb": [
            root / "logs" / "v32_sb.log",
            proj / "logs" / "v32_sb.log",
        ],
    }
    out: dict[str, Path | None] = {}
    for key, paths in candidates.items():
        chosen: Path | None = None
        for p in paths:
            if p.is_file():
                chosen = p
                break
        if chosen is None and paths:
            chosen = paths[0]
        out[key] = chosen
    return out


def tail_log_lines(path: Path | None, *, max_lines: int = LOG_TAIL_LINES) -> list[str]:
    if path is None or not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    return lines[-max_lines:] if len(lines) > max_lines else lines


_LOG_PATTERNS: list[tuple[FaultClass, re.Pattern[str]]] = [
    (FaultClass.HTTP_429, re.compile(r"\b429\b|rate.?limit|too many requests", re.I)),
    (
        FaultClass.STALE_LOCK_COLLISION,
        re.compile(
            r"session already active|healthy session holds|lock scope mismatch|"
            r"could not acquire session lock",
            re.I,
        ),
    ),
    (
        FaultClass.STAGE_4_BOOT_CLIFF,
        re.compile(r"STAGE_4_TUNER_PRIME.*(?:FAIL|ERROR|EXIT_FAIL|cliff)", re.I),
    ),
    (
        FaultClass.ENGINE_DROP,
        re.compile(
            r"agent process died|port conflict|Address already in use|Connection refused|"
            r"engine drop|main\.py.*exit",
            re.I,
        ),
    ),
    (FaultClass.LOG_ERROR_SPIKE, re.compile(r"\bCRITICAL\b|\bTraceback\b", re.I)),
]


def classify_log_lines(
    lines: list[str],
    *,
    engine: str,
    port: int,
    health_ok: bool,
) -> FaultReport | None:
    """Classify fault from log tail — never mutates source."""
    if health_ok and not lines:
        return None

    joined = "\n".join(lines[-40:])
    for fault_class, pattern in _LOG_PATTERNS:
        if pattern.search(joined):
            return _build_fault_report(fault_class, engine, port, lines, joined)

    if not health_ok:
        return _build_fault_report(
            FaultClass.PORT_OFFLINE,
            engine,
            port,
            lines,
            "health endpoint unreachable",
        )
    return None


def _build_fault_report(
    fault_class: FaultClass,
    engine: str,
    port: int,
    lines: list[str],
    detail: str,
) -> FaultReport:
    plans: dict[FaultClass, list[str]] = {
        FaultClass.STALE_LOCK_COLLISION: [
            "Run purge_stale_session_locks via session_lock.clear_stale_lock for ig:Z6BAH3/4",
            "Verify no zombie PID holds lock (session_is_healthy rejects zombies)",
            "Relaunch affected engine via v32_runtime_start or single-engine launch",
        ],
        FaultClass.HTTP_429: [
            "Observe RestApiBudget — do NOT raise caps",
            "Pause new entries until REST pressure clears (deploy_hold informational)",
            "Review ig_rest_traffic_governor pacing — manual tuning only",
        ],
        FaultClass.STAGE_4_BOOT_CLIFF: [
            "Inspect boot_stage_forensic.log for STAGE_4_TUNER_PRIME failures",
            "Proposed patch: master_orchestrator._stage4_tuner_prime retry/backoff (manual review)",
            "Do not auto-edit trading math or rest_client hot paths",
        ],
        FaultClass.ENGINE_DROP: [
            "Anti-zombie: mark_manual_stop + SIGTERM — never kill -9",
            "Clear stale locks then v32_runtime_start.sh start (flat book only)",
        ],
        FaultClass.PORT_OFFLINE: [
            "Confirm lsof :8080/:8081 — single-engine relaunch if peer healthy",
            "Full dual start only when both ports free and book flat",
        ],
        FaultClass.LOG_ERROR_SPIKE: [
            "Review log excerpt — classify root cause manually",
            "Write diagnostics/patch_proposal_*.md — no inline auto-patch",
        ],
        FaultClass.UNKNOWN: [
            "Manual operator review of diagnostics_fault.json",
        ],
    }
    return FaultReport(
        classification=fault_class,
        engine=engine,
        port=port,
        detail=detail[:500],
        log_excerpt=lines[-12:],
        recommended_plan=plans.get(fault_class, plans[FaultClass.UNKNOWN]),
    )


def heal_cooldown_allows_attempt(now_mono: float | None = None) -> bool:
    """Max HEAL_MAX_ATTEMPTS within HEAL_WINDOW_SEC."""
    now = now_mono if now_mono is not None else time.monotonic()
    with _LOCK:
        window_start = now - HEAL_WINDOW_SEC
        recent = [t for t in _heal_attempt_mono if t >= window_start]
        _heal_attempt_mono[:] = recent
        return len(recent) < HEAL_MAX_ATTEMPTS


def record_heal_attempt(now_mono: float | None = None) -> None:
    now = now_mono if now_mono is not None else time.monotonic()
    with _LOCK:
        _heal_attempt_mono.append(now)


def reset_orchestrator_for_tests() -> None:
    global _healing_active, _last_fault, _last_tick_at, _port_health
    global _orchestrator_started_mono, _boot_stale_override_last_mono
    global _rest_high_consecutive, _rest_backoff_generation, _rest_poll_intervals
    global _last_rest_heal, _operational_status, _last_mutex_reconcile
    with _LOCK:
        _heal_attempt_mono.clear()
        _healing_active = False
        _last_fault = None
        _last_tick_at = 0.0
        _port_health = {}
        _orchestrator_started_mono = time.monotonic()
        _boot_stale_override_last_mono = 0.0
        _rest_high_consecutive = 0
        _rest_backoff_generation = 0
        _rest_poll_intervals = {}
        _last_rest_heal = None
        _operational_status = "unknown"
        _last_mutex_reconcile = None
    _STOP.clear()
    try:
        from execution.order_in_flight_mutex import reset_order_mutex_for_tests

        reset_order_mutex_for_tests()
    except Exception:
        pass


def parse_interval_to_seconds(raw: Any) -> float | None:
    """Parse numeric seconds or strings like ``2s``, ``500ms``, ``1.5m``."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        return val if val > 0 else None
    text = str(raw or "").strip()
    if not text:
        return None
    m = _INTERVAL_UNIT_RE.match(text)
    if not m:
        return None
    num = float(m.group("num"))
    unit = (m.group("unit") or "s").lower()
    if unit == "ms":
        return num / 1000.0
    if unit in ("m", "min", "mins"):
        return num * 60.0
    return num


def format_interval_like(seconds: float, sample: Any) -> Any:
    """Format doubled seconds using the original value's style when possible."""
    if isinstance(sample, bool):
        return seconds
    if isinstance(sample, int) and not isinstance(sample, bool):
        return max(1, int(round(seconds)))
    if isinstance(sample, float):
        return float(seconds)
    text = str(sample or "").strip()
    m = _INTERVAL_UNIT_RE.match(text)
    if not m:
        return seconds
    unit = (m.group("unit") or "s").lower()
    if unit == "ms":
        return f"{max(1, int(round(seconds * 1000.0)))}ms"
    if unit in ("m", "min", "mins"):
        mins = seconds / 60.0
        if mins == int(mins):
            return f"{int(mins)}m"
        return f"{mins:g}m"
    if seconds == int(seconds):
        return f"{int(seconds)}s"
    return f"{seconds:g}s"


def double_interval_value(raw: Any) -> Any:
    """Double a rest_poll-style interval (numeric or ``Ns`` / ``Nms`` string)."""
    sec = parse_interval_to_seconds(raw)
    if sec is None:
        return raw
    doubled = max(sec * 2.0, sec + 0.5)
    return format_interval_like(doubled, raw)


def get_rest_poll_intervals() -> dict[str, Any]:
    """Orchestrator-owned poll interval snapshot (authoritative after back-off)."""
    with _LOCK:
        if _rest_poll_intervals:
            return dict(_rest_poll_intervals)
    seeded = _seed_rest_poll_intervals()
    with _LOCK:
        if not _rest_poll_intervals:
            _rest_poll_intervals.update(seeded)
        return dict(_rest_poll_intervals)


def _seed_rest_poll_intervals() -> dict[str, Any]:
    out: dict[str, Any] = {"rest_poll": "20s"}
    try:
        from system.config_loader import get_config

        cfg = get_config(reload=False)
        sec = float(getattr(cfg, "rest_min_interval_seconds", 20.0) or 20.0)
        out["rest_min_interval_seconds"] = sec
        out["rest_poll"] = format_interval_like(sec, "20s")
        out["stream_poll_seconds"] = float(
            getattr(cfg, "stream_poll_seconds", 8.0) or 8.0
        )
        data = getattr(cfg, "_data", None) or {}
        pricing = data.get("pricing") if isinstance(data, dict) else {}
        if isinstance(pricing, dict) and pricing.get("yahoo_poll_sec") is not None:
            out["yahoo_poll_sec"] = float(pricing["yahoo_poll_sec"])
    except Exception:
        out.setdefault("rest_min_interval_seconds", 20.0)
        out.setdefault("stream_poll_seconds", 8.0)
    return out


def _read_rest_pressure_level() -> str:
    """Best-effort REST pressure — in-process budget first, then shared ledger."""
    try:
        from system.rest_api_budget import get_rest_api_budget

        level = str(get_rest_api_budget().pressure_level() or "").upper()
        if level:
            return level
    except Exception:
        pass
    try:
        from system import shared_rest_budget

        # Extreme throttle proxy when budget metrics unavailable.
        if shared_rest_budget.recent_count("ig_positions", window_sec=60.0) >= 8:
            return "HIGH"
    except Exception:
        pass
    return "UNKNOWN"


def _entries_paused_flag() -> bool:
    try:
        from runtime.desk_dev_controls import entries_paused

        return bool(entries_paused())
    except Exception:
        return False


def soft_flush_network_buffers() -> dict[str, Any]:
    """Soft connection reset — flush sockets/caches without killing the process."""
    result: dict[str, Any] = {"flushed": [], "errors": []}
    try:
        from ig_api.streaming_factory import flush_streaming_session_handles

        detail = flush_streaming_session_handles()
        result["flushed"].append("streaming_session_handles")
        result["streaming"] = detail
    except Exception as exc:
        result["errors"].append(f"streaming:{type(exc).__name__}")
    try:
        from system.market_data_hub import flush_hub_streaming_session_cache

        flush_hub_streaming_session_cache()
        result["flushed"].append("hub_streaming_session_cache")
    except Exception as exc:
        result["errors"].append(f"hub:{type(exc).__name__}")
    try:
        from system.chaos_guardian import clear_token_queue_delays

        clear_token_queue_delays()
        result["flushed"].append("token_queue_delays")
    except Exception as exc:
        result["errors"].append(f"chaos:{type(exc).__name__}")
    try:
        client = None
        try:
            from runtime.session_registry import get_session_registry
            from system.credentials_loader import try_load_credentials

            cred = try_load_credentials()
            if cred.ok and cred.credentials is not None:
                client = get_session_registry().get_client_for_account(
                    CFD_ACCOUNT, cred.credentials
                )
        except Exception:
            client = None
        if client is not None and hasattr(client, "soft_flush_connection_buffers"):
            client.soft_flush_connection_buffers()
            result["flushed"].append("rest_client_adapters")
        elif client is not None:
            session = getattr(client, "_session", None)
            if session is not None:
                try:
                    session.cookies.clear()
                except Exception:
                    pass
                for adapter in list(getattr(session, "adapters", {}).values()):
                    close = getattr(adapter, "close", None)
                    if callable(close):
                        close()
                result["flushed"].append("rest_client_adapters")
    except Exception as exc:
        result["errors"].append(f"rest_client:{type(exc).__name__}")
    return result


def apply_executive_rest_backoff() -> dict[str, Any]:
    """
    Orchestrator authority over API polling intervals.

    Doubles rest_poll-style interval parameters and soft-flushes local sockets.
    """
    global _rest_backoff_generation, _rest_poll_intervals
    current = get_rest_poll_intervals()
    doubled: dict[str, Any] = {}
    for key, value in current.items():
        doubled[key] = double_interval_value(value)
    with _LOCK:
        _rest_poll_intervals = dict(doubled)
        _rest_backoff_generation += 1
        generation = _rest_backoff_generation

    applied: dict[str, Any] = {
        "event": "executive_rest_backoff",
        "generation": generation,
        "intervals_before": current,
        "intervals_after": doubled,
        "rest_budget": False,
        "flush": {},
    }
    new_min = parse_interval_to_seconds(
        doubled.get("rest_min_interval_seconds") or doubled.get("rest_poll")
    )
    if new_min is not None:
        try:
            from system.rest_api_budget import configure_rest_api_budget

            configure_rest_api_budget(min_interval_seconds=float(new_min))
            applied["rest_budget"] = True
            applied["rest_min_interval_seconds"] = float(new_min)
        except Exception as exc:
            applied["rest_budget_error"] = f"{type(exc).__name__}:{exc}"

    applied["flush"] = soft_flush_network_buffers()
    log_engine(
        "orchestrator: executive REST back-off "
        f"gen={generation} intervals={doubled} flush={applied['flush'].get('flushed')}"
    )
    return applied


def purge_stale_token_and_lock_caches() -> dict[str, Any]:
    """
    Non-disruptive cache clear under REST_PRESSURE_HIGH + entries_paused.

    Safety: only session/token caches + *stale* lock pointers. Never wipes
    learning DB, open-position SoT, journals, deploy_hold, or manual_stop.
    """
    result: dict[str, Any] = {
        "event": "rest_pressure_cache_purge",
        "tokens_removed": [],
        "locks_cleared": [],
        "skipped": [],
    }
    root = Path(data_dir())
    # Token / session caches under data root + logs (never learning/SoT).
    search_roots = [root]
    try:
        from system.paths import logs_dir

        search_roots.append(Path(logs_dir()))
    except Exception:
        pass
    for base in search_roots:
        if not base.is_dir():
            continue
        for pattern in _SAFE_TOKEN_CACHE_GLOBS:
            for path in base.glob(pattern):
                try:
                    if path.is_file():
                        path.unlink(missing_ok=True)
                        result["tokens_removed"].append(str(path.name))
                except OSError as exc:
                    result["skipped"].append(f"{path.name}:{type(exc).__name__}")

    # Stale session locks (Z6BAH3 / Z6BAH4) — clear_stale_lock is PID-safe.
    try:
        from runtime.session_lock import clear_stale_lock, lock_path_for_scope

        for scope in (f"ig:{CFD_ACCOUNT}", f"ig:{SB_ACCOUNT}"):
            path = lock_path_for_scope(scope, root)
            if clear_stale_lock(path):
                result["locks_cleared"].append(path.name)
            else:
                result["skipped"].append(f"lock_live_or_missing:{path.name}")
    except Exception as exc:
        result["skipped"].append(f"session_lock:{type(exc).__name__}")

    # Legacy agent port lock pointers — only when file exists (stale leftover).
    for name in _SAFE_STALE_LOCK_NAMES:
        path = root / name
        if not path.is_file():
            continue
        try:
            path.unlink(missing_ok=True)
            result["locks_cleared"].append(name)
        except OSError as exc:
            result["skipped"].append(f"{name}:{type(exc).__name__}")

    # Never touch these (document skips for audit).
    for protected in (
        "learning.db",
        "triage.db",
        "trade_support_status.json",
        "runtime_state.json",
        "metrics/daily_journal.csv",
        "state/deploy_hold.json",
        "state/manual_stop.json",
    ):
        result["skipped"].append(f"protected:{protected}")

    log_engine(
        "orchestrator: REST pressure cache purge "
        f"tokens={result['tokens_removed']} locks={result['locks_cleared']}"
    )
    return result


def soft_reauth_session() -> dict[str, Any]:
    """Soft re-auth / token refresh over isolated session path (no process kill)."""
    result: dict[str, Any] = {"event": "soft_reauth", "ok": False, "path": None}
    try:
        from runtime.session_registry import get_session_registry
        from system.credentials_loader import try_load_credentials

        cred = try_load_credentials()
        if not cred.ok or cred.credentials is None:
            result["reason"] = "credentials_unavailable"
            return result
        client = get_session_registry().get_client_for_account(
            CFD_ACCOUNT, cred.credentials
        )
        if client is None:
            result["reason"] = "client_unavailable"
            return result
        if hasattr(client, "refresh_session"):
            client.refresh_session()
            result["ok"] = True
            result["path"] = "refresh_session"
            return result
        if hasattr(client, "proactive_refresh_if_needed"):
            result["ok"] = bool(client.proactive_refresh_if_needed())
            result["path"] = "proactive_refresh_if_needed"
            return result
        if hasattr(client, "_refresh_session_tokens"):
            result["ok"] = bool(client._refresh_session_tokens())
            result["path"] = "_refresh_session_tokens"
            return result
        result["reason"] = "no_refresh_hook"
    except Exception as exc:
        result["reason"] = f"{type(exc).__name__}:{exc}"
    return result


def mark_operational_restored(*, reason: str = "rest_pressure_cleared") -> dict[str, Any]:
    """Return stability state to clean operational / emerald after healing."""
    global _healing_active, _operational_status, _rest_high_consecutive
    with _LOCK:
        _healing_active = False
        _rest_high_consecutive = 0
        _operational_status = "operational"
    payload = {
        "event": "operational_restored",
        "status": "operational",
        "desk_rag": "G",
        "label": "GREEN — path live",
        "reason": reason,
        "healing_active": False,
        "hold": False,
    }
    _persist_orchestrator_state(
        {
            "operational_status": "operational",
            "desk_rag": "G",
            "rest_heal": payload,
        }
    )
    log_engine(f"orchestrator: operational restored reason={reason}")
    return payload


def maybe_rest_pressure_executive_heal(
    *,
    pressure_level: str | None = None,
    entries_paused: bool | None = None,
) -> dict[str, Any] | None:
    """
    Tick-hook: REST HIGH for >3 consecutive polls → double rest_poll + flush.
    REST_PRESSURE_HIGH with entries_paused → token/lock purge + soft reauth.
    Pressure clear after heal → emerald / operational restore.
    """
    global _rest_high_consecutive, _healing_active, _last_rest_heal, _operational_status

    level = str(pressure_level if pressure_level is not None else _read_rest_pressure_level())
    level_u = level.upper()
    if level_u == "REST_PRESSURE_HIGH":
        level_u = "HIGH"
    paused = (
        bool(entries_paused)
        if entries_paused is not None
        else _entries_paused_flag()
    )

    high = level_u in REST_PRESSURE_HIGH_LEVELS or level_u == "HIGH"
    with _LOCK:
        if high:
            _rest_high_consecutive += 1
            consecutive = _rest_high_consecutive
        else:
            consecutive = _rest_high_consecutive
            _rest_high_consecutive = 0

    result: dict[str, Any] = {
        "event": "rest_pressure_executive_heal",
        "pressure_level": level_u,
        "entries_paused": paused,
        "consecutive_high_ticks": consecutive if high else 0,
        "actions": [],
    }

    # Fire once when consecutive first exceeds threshold (not every later tick).
    if high and consecutive == REST_HIGH_TICKS_BEFORE_BACKOFF + 1:
        with _LOCK:
            _healing_active = True
            _operational_status = "healing"
        backoff = apply_executive_rest_backoff()
        result["actions"].append("executive_rest_backoff")
        result["backoff"] = backoff

    if high and paused:
        with _LOCK:
            already_purged = bool(
                _last_rest_heal
                and "cache_purge" in (_last_rest_heal.get("actions") or [])
                and _operational_status == "healing"
            )
        if not already_purged:
            with _LOCK:
                _healing_active = True
                _operational_status = "healing"
            purge = purge_stale_token_and_lock_caches()
            reauth = soft_reauth_session()
            result["actions"].append("cache_purge")
            result["actions"].append("soft_reauth")
            result["purge"] = purge
            result["reauth"] = reauth

    if not high and _operational_status == "healing":
        restored = mark_operational_restored(reason="rest_pressure_cleared")
        result["actions"].append("operational_restored")
        result["restored"] = restored

    if not result["actions"]:
        return None

    with _LOCK:
        _last_rest_heal = result
    _persist_orchestrator_state({"rest_heal": result})
    return result


def _poll_health(port: int, *, timeout_sec: float = 2.5) -> dict[str, Any]:
    url = f"http://127.0.0.1:{int(port)}/api/health"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "IG-Agent-Orchestrator/1.0"})
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body) if body.strip() else {}
            health = {
                "online": True,
                "ok": bool(data.get("ok", True)),
                "status": resp.status,
                "agent_pid": data.get("agent_pid"),
                "payload": data,
            }
            health["operational"] = _engine_operational(health)
            return health
    except urllib.error.HTTPError as exc:
        health = {
            "online": True,
            "ok": False,
            "status": exc.code,
            "error": str(exc),
            "is_429": exc.code == 429,
            "payload": {},
        }
        health["operational"] = _engine_operational(health)
        return health
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        return {"online": False, "ok": False, "operational": False, "error": str(exc)}


def _v32_dual_supervision_expected() -> bool:
    """True when v32 dual-port supervision replaces legacy watchdog."""
    if os.environ.get("IG_V32_DUAL_PORT", "").strip() == "1":
        return True
    try:
        root = Path(data_dir())
        if (root / "state" / "v32_dual_supervision.json").is_file():
            return True
        if (root / "state" / "v32_legacy_watchdog_paused.json").is_file():
            return True
    except Exception:
        pass
    return False


def _engine_operational(health: dict[str, Any]) -> bool:
    """
    Dual-port tolerant engine health — HTTP 200 with live trading plane.

    Legacy ``ok:false`` from watchdog_inactive / supervision_drift must not
    classify an otherwise trading-healthy engine as port_offline.
    """
    if not health.get("online"):
        return False
    status = health.get("status")
    if isinstance(status, int) and status != 200:
        return False
    if health.get("ok"):
        return True
    payload = health.get("payload") or {}
    if payload.get("trading_healthy"):
        return True
    if str(payload.get("status") or "").upper() == "OPERATIONAL":
        return True
    if payload.get("trade_ready"):
        return True
    boot = payload.get("boot_metrics") or {}
    if boot.get("ready"):
        return True
    return False


def _watchdog_only_unhealthy(payload: dict[str, Any]) -> bool:
    """True when health ``ok`` is false solely due to legacy watchdog drift."""
    if not _v32_dual_supervision_expected():
        return False
    if payload.get("trading_healthy"):
        return True
    issues = [str(i) for i in (payload.get("issues") or [])]
    drift = payload.get("supervision_drift") or {}
    drift_issues = [str(i) for i in (drift.get("issues") or [])]
    combined = issues + [f"supervision:{i}" for i in drift_issues]
    if not combined:
        return False
    allowed = {
        "watchdog_inactive",
        "supervision:agent_running_without_watchdog",
    }
    return all(item in allowed for item in combined)


def _broker_opens_count(port: int = CFD_PORT) -> int | None:
    url = f"http://127.0.0.1:{int(port)}/api/positions/live"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "IG-Agent-Orchestrator/1.0"})
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            return int(data.get("count", data.get("broker_open_count", 0)) or 0)
    except Exception:
        return None


def _fetch_raw_broker_opens(account_id: str) -> int | None:
    """Reconcile true ledger count via REST /positions (or local SoT fallback)."""
    acct = str(account_id or "").strip().upper()
    try:
        from runtime.session_registry import get_session_registry
        from system.credentials_loader import try_load_credentials

        cred = try_load_credentials()
        if cred.ok and cred.credentials is not None and acct:
            client = get_session_registry().get_client_for_account(
                acct, cred.credentials
            )
            if client is not None and hasattr(client, "count_open_positions_live"):
                try:
                    return int(client.count_open_positions_live())
                except Exception:
                    pass
            if client is not None and hasattr(client, "count_open_positions"):
                return int(client.count_open_positions())
            if client is not None and hasattr(client, "fetch_open_positions"):
                return len(client.fetch_open_positions() or [])
    except Exception as exc:
        log_engine(
            f"orchestrator: raw positions fetch skipped "
            f"account={acct} {type(exc).__name__}: {exc}"
        )
    port = CFD_PORT if acct == CFD_ACCOUNT else SB_PORT if acct == SB_ACCOUNT else CFD_PORT
    api_n = _broker_opens_count(port)
    if api_n is not None:
        return api_n
    try:
        from execution.order_in_flight_mutex import broker_open_count_authoritative

        return broker_open_count_authoritative(acct)
    except Exception:
        return None


def maybe_reconcile_ambiguous_order_mutex(
    *,
    timeout_sec: float = AMBIGUOUS_ORDER_MUTEX_SEC,
) -> dict[str, Any] | None:
    """
    Self-heal when order_in_flight stays True > timeout after ambiguous network errors.

    1) Clear per-account mutex
    2) Check raw broker /positions
    3) Reconcile ledger count
    4) Report status for Terminal ops_strip / orchestrator API
    """
    global _last_mutex_reconcile, _healing_active, _operational_status

    try:
        from execution.order_in_flight_mutex import get_order_mutex
    except Exception:
        return None

    mux = get_order_mutex()
    aged = mux.ambiguous_accounts(timeout_sec=float(timeout_sec))
    if not aged:
        return None

    accounts_out: list[dict[str, Any]] = []
    for acct in aged:
        opens = _fetch_raw_broker_opens(acct)
        # Reconcile ledger BEFORE releasing mutex so release(filled=…) is accurate.
        filled: bool | None
        if opens is None:
            filled = None
        elif int(opens) <= 0:
            filled = False
        else:
            filled = True
        cleared = mux.release(
            acct, reason="orchestrator_ambiguous_timeout", filled=filled
        )
        try:
            from execution.order_in_flight_mutex import (
                sync_hard_cap_ledger_with_broker,
            )

            # Force ledger to broker SoT (clears stuck open=1 when flat).
            if opens is not None:
                sync_hard_cap_ledger_with_broker(
                    acct, force_broker_n=int(opens)
                )
        except Exception as exc:
            log_engine(
                f"orchestrator: ledger sync after ambiguous clear failed "
                f"account={acct} {type(exc).__name__}: {exc}"
            )
        row = {
            "account_id": acct,
            "mutex_cleared": bool(cleared),
            "broker_opens": opens,
            "timeout_sec": float(timeout_sec),
            "ledger_filled": filled,
        }
        accounts_out.append(row)
        log_engine(
            f"orchestrator: ambiguous order mutex cleared account={acct} "
            f"broker_opens={opens if opens is not None else 'n/a'} "
            f"filled={filled} after>{timeout_sec:.1f}s"
        )

    payload = {
        "ts": time.time(),
        "action": "ambiguous_order_mutex_reconcile",
        "accounts": accounts_out,
        "order_mutex": mux.status(),
    }
    with _LOCK:
        _last_mutex_reconcile = payload
        _healing_active = True
        if _operational_status != "healing":
            _operational_status = "healing"
    return payload


def _real_cap_breach_live(port: int = CFD_PORT) -> bool:
    """True only when live positions API confirms opens > max_open."""
    try:
        from system.config_loader import get_config

        max_open = max(1, int(getattr(get_config(), "max_open_positions", 6) or 6))
    except Exception:
        max_open = 6
    opens = _broker_opens_count(port)
    return opens is not None and opens > max_open


def _trade_support_sot_age_sec() -> float | None:
    path = Path(data_dir()) / "trade_support_status.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        ts = float(raw.get("ts") or 0)
        if ts <= 0:
            return None
        return max(0.0, time.time() - ts)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _maybe_boot_sot_snapshot_fallback() -> dict[str, Any] | None:
    """
    Permanent boot hydration path: when trade_support SoT is stall-aged during
    boot grace, hydrate opens from verified local broker_snapshot.json.
    """
    try:
        from runtime.boot_sot_fallback import resolve_boot_sot_fallback
        from runtime.desk_stability_harness import (
            boot_grace_active,
            trade_support_stale_budget_sec,
        )
    except Exception:
        return None

    if not boot_grace_active():
        return None

    sot_age = _trade_support_sot_age_sec()
    try:
        stale_budget = float(trade_support_stale_budget_sec())
    except Exception:
        stale_budget = float(BOOT_STALE_SOT_OVERRIDE_SEC)
    try:
        from runtime.desk_stability_harness import RUNTIME_SOT_STALE_SEC

        runtime_stale = float(RUNTIME_SOT_STALE_SEC)
    except Exception:
        runtime_stale = 20.0

    sot_ok = sot_age is not None and sot_age < stale_budget
    network_stall = sot_age is None or sot_age >= runtime_stale
    if sot_ok and not network_stall:
        return None

    fb = resolve_boot_sot_fallback(
        booting=True,
        sot_age_sec=sot_age,
        stale_budget_sec=stale_budget,
        network_timeout=network_stall,
        sot_ok=sot_ok,
        broker_open=_broker_opens_count(CFD_PORT),
    )
    if not fb.get("fallback_active"):
        return None
    log_engine(
        "orchestrator: boot_sot_snapshot_fallback "
        f"active={fb.get('fallback_active')} reason={fb.get('fallback_reason')!r} "
        f"sot_ok={fb.get('sot_ok')} opens={fb.get('broker_open')}"
    )
    return {"event": "boot_sot_snapshot_fallback", **fb}


def _cap_breach_pause_active() -> tuple[bool, str]:
    """Detect stability-harness cap-breach entry holds."""
    for name in ("trading_paused.json", "entry_halt.json"):
        for root in (
            Path(data_dir()) / "state",
            Path(data_dir()) / "state_cfd",
            Path(data_dir()) / "state_sb",
        ):
            path = root / name
            if not path.is_file():
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            if not bool(raw.get("active")):
                continue
            reason = str(raw.get("reason") or "").lower()
            if any(m in reason for m in _CAP_BREACH_PAUSE_MARKERS):
                return True, reason
    return False, ""


def _orchestrator_boot_elapsed_sec() -> float:
    try:
        from runtime.desk_stability_harness import boot_grace_active, boot_started_at

        if boot_grace_active():
            return max(0.0, time.time() - boot_started_at())
    except Exception:
        pass
    return max(0.0, time.monotonic() - _orchestrator_started_mono)


def _maybe_boot_stale_sot_cap_breach_override() -> dict[str, Any] | None:
    """
    During first BOOT_STALE_SOT_OVERRIDE_SEC, clear false cap-breach freezes when
    the live book is flat and trade_support SoT is merely boot-stale.

    Safety: never override when live opens > max_open_positions.
    """
    global _boot_stale_override_last_mono

    if _orchestrator_boot_elapsed_sec() > BOOT_STALE_SOT_OVERRIDE_SEC:
        return None

    paused, pause_reason = _cap_breach_pause_active()
    if not paused:
        return None

    if _real_cap_breach_live(CFD_PORT):
        return None

    broker_open = _broker_opens_count(CFD_PORT)
    sot_age = _trade_support_sot_age_sec()
    runtime_stale_sec = 20.0
    try:
        from runtime.desk_stability_harness import (
            RUNTIME_SOT_STALE_SEC,
            trade_support_stale_budget_sec,
        )

        runtime_stale_sec = float(RUNTIME_SOT_STALE_SEC)
        stale_budget = trade_support_stale_budget_sec()
    except Exception:
        stale_budget = 45.0

    flat = broker_open is not None and broker_open <= 0
    stale_sot = sot_age is None or sot_age > runtime_stale_sec
    if not flat and not stale_sot:
        return None

    result: dict[str, Any] = {
        "event": "boot_stale_sot_cap_breach_override",
        "broker_open": broker_open,
        "sot_age_sec": sot_age,
        "stale_budget_sec": stale_budget,
        "pause_reason": pause_reason,
        "cleared": [],
    }

    try:
        from system.shutdown_cleanup import clear_manual_stop

        clear_manual_stop()
        result["cleared"].append("manual_stop")
    except Exception as exc:
        result["manual_stop_error"] = f"{type(exc).__name__}:{exc}"

    try:
        from system.startup_hold_clear import clear_stale_entry_holds_if_flat

        hold = clear_stale_entry_holds_if_flat(
            port=CFD_PORT,
            reason="boot_stale_sot_cap_breach_override",
            allow_offline_stale_clear=True,
        )
        result["hold_clear"] = hold
        if hold.get("cleared"):
            result["cleared"].extend(hold["cleared"])
    except Exception as exc:
        result["hold_clear_error"] = f"{type(exc).__name__}:{exc}"

    try:
        from runtime.desk_dev_controls import entries_paused, resume_entries

        if entries_paused():
            resume_entries(reason="boot_stale_sot_cap_breach_override")
            result["cleared"].append("entries_paused")
    except Exception as exc:
        result["resume_error"] = f"{type(exc).__name__}:{exc}"

    try:
        from runtime.session_registry import get_session_registry
        from system.credentials_loader import try_load_credentials

        cred = try_load_credentials()
        if cred.ok and cred.credentials is not None:
            client = get_session_registry().get_client_for_account(
                CFD_ACCOUNT, cred.credentials
            )
            if hasattr(client, "refresh_session"):
                client.refresh_session()
                result["session_refresh"] = True
    except Exception:
        result["session_refresh"] = False

    now_mono = time.monotonic()
    if (
        now_mono - _boot_stale_override_last_mono
        >= BOOT_STALE_OVERRIDE_COOLDOWN_SEC
        and stale_sot
    ):
        try:
            script = project_root() / "scripts" / "trade_support_wrapper.sh"
            if script.is_file():
                subprocess.Popen(
                    ["/bin/bash", str(script)],
                    cwd=str(project_root()),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                result["trade_support_kickstart"] = True
                _boot_stale_override_last_mono = now_mono
        except OSError as exc:
            result["trade_support_kickstart_error"] = str(exc)

    log_engine(
        "orchestrator: boot_stale_sot_cap_breach_override "
        f"broker_open={broker_open} sot_age={sot_age} reason={pause_reason!r} "
        f"cleared={result.get('cleared')}"
    )
    return result


def _write_diagnostics_fault(report: FaultReport) -> None:
    path = _diagnostics_fault_path()
    if os.environ.get("IG_AGENT_PYTEST") == "1":
        return
    try:
        path.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        log_engine(f"orchestrator: diagnostics_fault write failed: {exc}")

    proposal = _diagnostics_proposal_dir() / f"patch_proposal_{report.classification.value}.md"
    if not proposal.is_file():
        try:
            proposal.write_text(
                "\n".join(
                    [
                        f"# Patch proposal — {report.classification.value}",
                        "",
                        f"Engine: {report.engine} :{report.port}",
                        "",
                        "## Recommended plan (manual review only)",
                        "",
                        *[f"- {line}" for line in report.recommended_plan],
                        "",
                        "## Log excerpt",
                        "",
                        "```",
                        *report.log_excerpt[-8:],
                        "```",
                        "",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass


def _persist_orchestrator_state(extra: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {
        "healing_active": _healing_active,
        "last_fault": _last_fault,
        "last_tick_at": _last_tick_at,
        "port_health": _port_health,
        "heal_attempts_in_window": len(_heal_attempt_mono),
        "heal_max_attempts": HEAL_MAX_ATTEMPTS,
        "heal_window_sec": HEAL_WINDOW_SEC,
        "ts": time.time(),
    }
    if extra:
        payload.update(extra)
    path = _orchestrator_state_path()
    if os.environ.get("IG_AGENT_PYTEST") == "1":
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def _clear_stale_session_locks() -> None:
    try:
        from runtime.session_lock import clear_stale_lock, lock_path_for_scope

        root = Path(data_dir())
        for scope in (f"ig:{CFD_ACCOUNT}", f"ig:{SB_ACCOUNT}"):
            clear_stale_lock(lock_path_for_scope(scope, root))
    except Exception as exc:
        log_engine(f"orchestrator: stale lock purge failed: {type(exc).__name__}: {exc}")


def _mark_fail_closed(reason: str, *, health_payload: dict[str, Any] | None = None) -> None:
    if health_payload and _watchdog_only_unhealthy(health_payload):
        log_engine(
            "orchestrator: skip deploy_hold — v32 dual supervision, watchdog-only drift"
        )
        return
    try:
        from runtime.feed_health_watchdog import mark_orchestrator_fault

        mark_orchestrator_fault(reason)
    except Exception as exc:
        log_engine(f"orchestrator: mark_orchestrator_fault skipped: {exc}")
    try:
        from runtime.deploy_hold import set_deploy_hold

        set_deploy_hold(active=True, reason=f"orchestrator:{reason[:80]}")
    except Exception as exc:
        log_engine(f"orchestrator: deploy_hold skipped: {exc}")


def _clear_orchestrator_deploy_hold() -> None:
    """Drop stale orchestrator deploy_hold once both engines are operational."""
    try:
        from runtime.deploy_hold import _read_hold_file, set_deploy_hold

        raw = _read_hold_file()
        reason = str(raw.get("reason") or "")
        if raw.get("active") and reason.startswith("orchestrator:"):
            set_deploy_hold(active=False, reason="orchestrator:cleared_dual_operational")
            log_engine("orchestrator: cleared stale deploy_hold (both engines operational)")
    except Exception as exc:
        log_engine(f"orchestrator: deploy_hold clear skipped: {exc}")


def _python_bin() -> str:
    venv = project_root() / ".venv" / "bin" / "python3"
    if venv.is_file():
        return str(venv)
    return os.environ.get("PYTHON_BIN", "python3")


def _launch_single_engine(port: int, account: str, origin: str, log_file: Path) -> int | None:
    """Allowlisted single-engine relaunch — mirrors v32_runtime_start.sh launch_engine."""
    root = project_root()
    py = _python_bin()
    pid_file = Path(data_dir()) / ("state_cfd" if port == CFD_PORT else "state_sb") / "agent.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    from kernel.ring_buffer import dual_port_shm_lane_from

    lane = dual_port_shm_lane_from(int(port), origin=origin, account=account)
    env.update(
        {
            "APP_MODE": env.get("APP_MODE", "DEMO"),
            "IG_AGENT_CONFIG": env.get(
                "IG_AGENT_CONFIG", "config/config_v31_demo_throughput.json"
            ),
            "PYTHONPATH": env.get("PYTHONPATH", "src"),
            "IG_AGENT_ROOT": str(root),
            "IG_V32_DUAL_PORT": "1",
            "IG_API_PORT": str(port),
            "PORT": str(port),
            "IG_ACCOUNT_ID": account,
            "IG_ACCOUNT_SCOPE": f"ig:{account}",
            "IG_ENGINE_ORIGIN": origin,
            "IG_SHM_RING_NAME": f"ig_agent_v33_shm_{lane}",
            "IG_SHM_RING_CREATE": "1",
            "IG_COCKPIT_SHM_NAME": f"ig_agent_v33_cockpit_{lane}",
        }
    )
    if port == CFD_PORT:
        env["IG_AGENT_ORCHESTRATOR"] = "1"
    cmd = [
        py,
        str(root / "src" / "main.py"),
        f"--port={port}",
        f"--account-id={account}",
        f"--origin={origin}",
    ]
    try:
        with open(log_file, "a", encoding="utf-8") as logfh:
            proc = subprocess.Popen(
                cmd,
                cwd=str(root),
                env=env,
                stdout=logfh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        pid_file.write_text(f"{proc.pid}\n", encoding="utf-8")
        return int(proc.pid)
    except OSError as exc:
        log_engine(f"orchestrator: single-engine launch failed: {exc}")
        return None


def attempt_allowlisted_restore(
    *,
    cfd_online: bool,
    sb_online: bool,
    fault: FaultReport,
) -> dict[str, Any]:
    """Apply allowlisted restore — abort if broker opens > 0 or cooldown exhausted."""
    result: dict[str, Any] = {
        "attempted": False,
        "ok": False,
        "reason": "",
        "action": None,
    }
    opens = _broker_opens_count(CFD_PORT if cfd_online else SB_PORT if sb_online else CFD_PORT)
    if opens is None and not cfd_online and not sb_online:
        opens = 0
    if opens is not None and opens > 0:
        result["reason"] = f"abort_cutover_broker_opens={opens}"
        log_engine(f"orchestrator: heal aborted — broker opens={opens}")
        return result

    if not heal_cooldown_allows_attempt():
        result["reason"] = "heal_cooldown_exhausted"
        return result

    record_heal_attempt()
    result["attempted"] = True
    _clear_stale_session_locks()

    logs = resolve_engine_log_paths()
    root = project_root()
    script = root / "scripts" / "v32_runtime_start.sh"

    if not cfd_online and not sb_online:
        if not script.is_file():
            result["reason"] = "v32_runtime_start_missing"
            return result
        try:
            proc = subprocess.run(
                [str(script), "start"],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            result["action"] = "v32_runtime_start.sh start"
            result["ok"] = proc.returncode == 0
            result["stdout"] = (proc.stdout or "")[-2000:]
            result["stderr"] = (proc.stderr or "")[-1000:]
            result["returncode"] = proc.returncode
            result["reason"] = "dual_start" if proc.returncode == 0 else "dual_start_failed"
            return result
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["reason"] = f"dual_start_error:{type(exc).__name__}"
            return result

    if not cfd_online and sb_online:
        pid = _launch_single_engine(
            CFD_PORT,
            CFD_ACCOUNT,
            CFD_ORIGIN,
            logs.get("cfd") or Path(data_dir()) / "logs" / "v32_cfd.log",
        )
        result["action"] = "launch_cfd_engine"
        result["ok"] = pid is not None
        result["pid"] = pid
        result["reason"] = "cfd_relaunch" if pid else "cfd_relaunch_failed"
        return result

    if cfd_online and not sb_online:
        pid = _launch_single_engine(
            SB_PORT,
            SB_ACCOUNT,
            SB_ORIGIN,
            logs.get("sb") or Path(data_dir()) / "logs" / "v32_sb.log",
        )
        result["action"] = "launch_sb_engine"
        result["ok"] = pid is not None
        result["pid"] = pid
        result["reason"] = "sb_relaunch" if pid else "sb_relaunch_failed"
        return result

    result["reason"] = "both_online_no_restore_needed"
    result["ok"] = True
    return result


def _handle_fault(
    fault: FaultReport,
    *,
    cfd_online: bool,
    sb_online: bool,
    health_payload: dict[str, Any] | None = None,
) -> None:
    global _healing_active, _last_fault

    _last_fault = fault.to_dict()
    _write_diagnostics_fault(fault)
    _mark_fail_closed(fault.classification.value, health_payload=health_payload)
    log_engine(
        f"orchestrator: fault {fault.classification.value} engine={fault.engine} "
        f"port={fault.port} detail={fault.detail[:120]}"
    )

    with _LOCK:
        _healing_active = True
    _persist_orchestrator_state()

    restore = attempt_allowlisted_restore(
        cfd_online=cfd_online,
        sb_online=sb_online,
        fault=fault,
    )
    _persist_orchestrator_state({"last_restore": restore})

    if restore.get("ok") and cfd_online and sb_online:
        with _LOCK:
            _healing_active = False
    elif restore.get("ok") and restore.get("action"):
        # Leave healing_active until next tick confirms both ports
        pass
    elif not restore.get("attempted"):
        with _LOCK:
            _healing_active = False


def _tick_once() -> None:
    global _healing_active, _last_tick_at, _port_health

    if orchestrator_heal_paused():
        _last_tick_at = time.time()
        _persist_orchestrator_state({"heal_paused": True})
        return

    _maybe_boot_stale_sot_cap_breach_override()
    try:
        _maybe_boot_sot_snapshot_fallback()
    except Exception as exc:
        log_engine(
            f"orchestrator: boot_sot_snapshot_fallback skipped "
            f"{type(exc).__name__}: {exc}"
        )
    # Ambiguous order mutex — clear lock + reconcile broker ledger after >5s.
    try:
        maybe_reconcile_ambiguous_order_mutex()
    except Exception as exc:
        log_engine(
            f"orchestrator: mutex reconcile skipped {type(exc).__name__}: {exc}"
        )
    # Periodic hard-cap ledger sync (clears stuck open=1 when broker flat).
    try:
        from execution.order_in_flight_mutex import (
            HARD_OPEN_CAP_BY_ACCOUNT,
            get_order_mutex,
            sync_hard_cap_ledger_with_broker,
        )

        for _acct in list(HARD_OPEN_CAP_BY_ACCOUNT.keys()):
            if get_order_mutex().is_locked(_acct):
                continue
            opens = _fetch_raw_broker_opens(_acct)
            if opens is not None:
                sync_hard_cap_ledger_with_broker(
                    _acct, force_broker_n=int(opens)
                )
    except Exception as exc:
        log_engine(
            f"orchestrator: hard_cap ledger sync skipped {type(exc).__name__}: {exc}"
        )
    # Rate-smoothing / token purge — runs every tick (independent of dual-port health).
    try:
        maybe_rest_pressure_executive_heal()
    except Exception as exc:
        log_engine(
            f"orchestrator: rest_pressure heal skipped {type(exc).__name__}: {exc}"
        )

    logs = resolve_engine_log_paths()
    cfd_health = _poll_health(CFD_PORT)
    sb_health = _poll_health(SB_PORT)
    cfd_online = bool(cfd_health.get("online"))
    sb_online = bool(sb_health.get("online"))
    cfd_ok = bool(cfd_health.get("operational"))
    sb_ok = bool(sb_health.get("operational"))

    _port_health = {
        "cfd": {"port": CFD_PORT, **cfd_health},
        "sb": {"port": SB_PORT, **sb_health},
    }
    _last_tick_at = time.time()

    both_healthy = cfd_ok and sb_ok
    if both_healthy:
        with _LOCK:
            # Port plane healthy — do not clear REST executive heal mid-backoff.
            if _operational_status != "healing":
                _healing_active = False
        _clear_orchestrator_deploy_hold()
        _persist_orchestrator_state()
        return

    engines = (
        ("cfd", CFD_PORT, logs.get("cfd"), cfd_ok, cfd_online),
        ("sb", SB_PORT, logs.get("sb"), sb_ok, sb_online),
    )
    for name, port, log_path, ok, online in engines:
        if ok:
            continue
        lines = tail_log_lines(log_path)
        health = cfd_health if name == "cfd" else sb_health
        payload = health.get("payload") or {}
        if _engine_in_boot_grace(
            port=port,
            online=online,
            ok=ok,
            log_path=log_path,
            health=health,
        ):
            continue
        if _watchdog_only_unhealthy(payload):
            continue
        if health.get("is_429") or health.get("status") == 429:
            fault = _build_fault_report(
                FaultClass.HTTP_429,
                name,
                port,
                lines,
                "HTTP 429 from health poll",
            )
        else:
            fault = classify_log_lines(lines, engine=name, port=port, health_ok=ok)
            if fault is None:
                fault = _build_fault_report(
                    FaultClass.PORT_OFFLINE if not online else FaultClass.ENGINE_DROP,
                    name,
                    port,
                    lines,
                    str(health.get("error") or "engine unhealthy"),
                )
        _handle_fault(
            fault,
            cfd_online=cfd_online,
            sb_online=sb_online,
            health_payload=payload,
        )
        break


def _loop() -> None:
    while not _STOP.wait(POLL_SEC):
        try:
            _tick_once()
        except Exception as exc:
            log_engine(f"orchestrator: tick failed {type(exc).__name__}: {exc}")


def get_orchestrator_status() -> dict[str, Any]:
    with _LOCK:
        healing = _healing_active
        attempts = len(_heal_attempt_mono)
        fault = _last_fault
        ports = dict(_port_health)
        tick_at = _last_tick_at
        rest_ticks = _rest_high_consecutive
        rest_gen = _rest_backoff_generation
        rest_intervals = dict(_rest_poll_intervals)
        rest_heal = dict(_last_rest_heal) if _last_rest_heal else None
        op_status = _operational_status
        mutex_reconcile = dict(_last_mutex_reconcile) if _last_mutex_reconcile else None
    cfd = ports.get("cfd") or {}
    sb = ports.get("sb") or {}
    cfd_ok = bool(cfd.get("operational")) if "operational" in cfd else bool(
        cfd.get("online") and cfd.get("ok")
    )
    sb_ok = bool(sb.get("operational")) if "operational" in sb else bool(
        sb.get("online") and sb.get("ok")
    )
    order_mutex: dict[str, Any] = {}
    try:
        from execution.order_in_flight_mutex import get_order_mutex

        order_mutex = get_order_mutex().status()
    except Exception as exc:
        order_mutex = {"ok": False, "error": f"{type(exc).__name__}"}
    return {
        "ok": True,
        "orchestrator_armed": should_run_orchestrator(),
        "healing_active": healing,
        "dual_engine_operational": cfd_ok and sb_ok,
        "cfd_port_online": bool(cfd.get("online")),
        "sb_port_online": bool(sb.get("online")),
        "cfd_health_ok": cfd_ok,
        "sb_health_ok": sb_ok,
        "heal_attempts_in_window": attempts,
        "heal_max_attempts": HEAL_MAX_ATTEMPTS,
        "heal_window_sec": HEAL_WINDOW_SEC,
        "last_fault": fault,
        "last_tick_at": tick_at,
        "diagnostics_fault_path": str(_diagnostics_fault_path()),
        "poll_sec": POLL_SEC,
        "operational_status": op_status,
        "rest_high_consecutive": rest_ticks,
        "rest_backoff_generation": rest_gen,
        "rest_poll_intervals": rest_intervals,
        "last_rest_heal": rest_heal,
        "order_mutex": order_mutex,
        "mutex_reconcile": mutex_reconcile,
        "ambiguous_order_timeout_sec": AMBIGUOUS_ORDER_MUTEX_SEC,
    }


def maybe_start_agent_orchestrator() -> bool:
    """Idempotent daemon start — call from post_ready_services or main."""
    global _THREAD, _STARTED

    if not should_run_orchestrator():
        return False
    with _LOCK:
        if _STARTED and _THREAD is not None and _THREAD.is_alive():
            return True
        _STOP.clear()
        _THREAD = threading.Thread(
            target=_loop,
            name="agent-orchestrator-v33",
            daemon=True,
        )
        _THREAD.start()
        _STARTED = True
        global _orchestrator_started_mono
        _orchestrator_started_mono = time.monotonic()
    log_engine("orchestrator: v33 self-healing daemon started (2s poll :8080/:8081)")
    return True


def stop_agent_orchestrator() -> None:
    global _STARTED
    _STOP.set()
    _STARTED = False
