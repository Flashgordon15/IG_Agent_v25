"""
Application Stability Harness — AI desk supervisor (composite SoT).

Ingests fragmented readiness planes (health, path, SoT, REST, OPM, UI, pause)
into one ``desk_stability`` grade with conservative, rate-limited auto-heals.

Never kill -9 main. Never flatten blindly. Skip under IG_TEST_HARNESS.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.paths import data_dir, project_root

BOOT_SOT_GRACE_SEC = 60.0
# Relaxed hydration buffer — prevent false ENGINE BLOCKAGE / GATE HOLD during boot.
BOOT_LATENCY_BUFFER_SEC = 30.0
BOOT_SOT_STALE_BUDGET_SEC = 30.0
RUNTIME_SOT_STALE_SEC = 20.0

_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "poll_interval_sec": 25.0,
    "ui_port": 3000,
    "api_port": 8080,
    "boot_grace_sec": BOOT_SOT_GRACE_SEC,
    "boot_latency_buffer_sec": BOOT_LATENCY_BUFFER_SEC,
    "boot_trade_support_stale_sec": BOOT_SOT_STALE_BUDGET_SEC,
    "runtime_trade_support_stale_sec": RUNTIME_SOT_STALE_SEC,
    "trade_support_stale_sec": 90.0,
    "heal_cooldown_sec": 120.0,
    "ui_heal_cooldown_sec": 180.0,
    "auto_heal_enabled": True,
    "auto_pause_on_rest_critical": True,
    "auto_pause_on_cap_breach": True,
    "auto_heal_ui": True,
    "auto_heal_trade_support": True,
}

_lock = threading.RLock()
_stop = threading.Event()
_thread: threading.Thread | None = None
_last_payload: dict[str, Any] = {}
_action_mono: dict[str, float] = {}
_boot_started_at: float = time.time()


def note_boot_started(ts: float | None = None) -> None:
    """Record process/boot gate clock (called from post_ready or first evaluate)."""
    global _boot_started_at
    with _lock:
        _boot_started_at = float(ts if ts is not None else time.time())


def boot_started_at() -> float:
    with _lock:
        return float(_boot_started_at)


def boot_grace_active(*, cfg: dict[str, Any] | None = None) -> bool:
    """First N seconds after harness boot — relax SoT / cap-breach false freezes."""
    conf = cfg or _load_cfg()
    grace = float(conf.get("boot_grace_sec", BOOT_SOT_GRACE_SEC))
    return (time.time() - boot_started_at()) < grace


def boot_latency_buffer_sec(*, cfg: dict[str, Any] | None = None) -> float:
    """Relaxed ops-badge / telemetry latency budget during boot hydration (default 30s)."""
    conf = cfg or _load_cfg()
    return float(conf.get("boot_latency_buffer_sec", BOOT_LATENCY_BUFFER_SEC))


def boot_latency_buffer_active(*, cfg: dict[str, Any] | None = None) -> bool:
    """True while initial candle/token hydration should suppress false blockage badges."""
    conf = cfg or _load_cfg()
    buf = boot_latency_buffer_sec(cfg=conf)
    return (time.time() - boot_started_at()) < buf


def false_engine_blockage_suppressed(*, cfg: dict[str, Any] | None = None) -> bool:
    """
    During the boot latency buffer, heavy REST/candle loads must not surface as
    ENGINE BLOCKAGE / DESK TRADING DOWN / GATE HOLD on the ops badge.
    """
    return boot_latency_buffer_active(cfg=cfg)


def trade_support_stale_budget_sec(*, cfg: dict[str, Any] | None = None) -> float:
    """
    SoT freshness budget — 30s during boot grace, ~20s steady-state (configurable).

    ``trade_support_stale_sec`` (90s) remains the desk_support heal threshold.
    """
    conf = cfg or _load_cfg()
    if boot_grace_active(cfg=conf):
        return float(conf.get("boot_trade_support_stale_sec", BOOT_SOT_STALE_BUDGET_SEC))
    runtime = conf.get("runtime_trade_support_stale_sec")
    if runtime is not None:
        return float(runtime)
    legacy = float(conf.get("trade_support_stale_sec", 90.0))
    return min(legacy, RUNTIME_SOT_STALE_SEC)


def _harness_mode() -> bool:
    return (
        os.environ.get("IG_TEST_HARNESS", "").strip() == "1"
        or os.environ.get("IG_AGENT_PYTEST", "").strip() == "1"
    )


def _audit_path() -> Path:
    return data_dir() / "state" / "desk_stability_harness.jsonl"


def _audit(event: str, detail: dict[str, Any]) -> None:
    row = {"ts": time.time(), "event": event, **detail}
    try:
        path = _audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except OSError:
        pass
    try:
        log_engine(f"DeskStability: {event} {detail}")
    except Exception:
        pass


def _load_cfg(cfg: Any | None = None) -> dict[str, Any]:
    out = dict(_DEFAULTS)
    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            return out
    block = {}
    try:
        raw = (
            cfg.get("desk_stability_harness")
            if hasattr(cfg, "get")
            else getattr(cfg, "desk_stability_harness", None)
        )
        if isinstance(raw, dict):
            block = raw
    except Exception:
        block = {}
    for key, val in block.items():
        if str(key).startswith("_"):
            continue
        out[key] = val
    try:
        out["api_port"] = int(os.environ.get("IG_API_PORT", out.get("api_port", 8080)))
    except (TypeError, ValueError):
        out["api_port"] = 8080
    return out


def _port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.4):
            return True
    except OSError:
        return False


def _fetch_local(path: str, *, port: int, timeout: float = 3.0) -> dict[str, Any] | None:
    url = f"http://127.0.0.1:{int(port)}{path}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body if isinstance(body, dict) else None
    except (OSError, urllib.error.URLError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _read_flag(name: str) -> dict[str, Any]:
    try:
        path = data_dir() / "state" / name
        if not path.is_file():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _flag_active(name: str) -> bool:
    raw = _read_flag(name)
    if not raw:
        return False
    return bool(raw.get("active", False))


@dataclass
class StabilityComponents:
    health_ok: bool | None = None
    trade_ready: bool | None = None
    trading_path_live: bool = False
    trading_path_badge: str = ""
    desk_rag: str = "A"
    desk_rag_label: str = ""
    path_blockers: list[str] = field(default_factory=list)
    broker_open: int = 0
    positions_verdict: str = ""
    sot_source: str = ""
    sot_age_sec: float | None = None
    sot_ok: bool = False
    trade_support_running: bool = False
    rest_pressure_level: str = "UNKNOWN"
    opm_tick_age_sec: float | None = None
    opm_ok: bool = True
    ui_up: bool = False
    ui_port: int = 3000
    liveness_ok: bool | None = None
    liveness_issues: list[str] = field(default_factory=list)
    has_open_risk: bool = False
    entries_paused: bool = False
    offline_for_dev: bool = False
    manual_stop: bool = False
    deploy_hold: bool = False
    feed_transport: dict[str, Any] = field(default_factory=dict)
    cap_breach: bool = False
    flat_book: bool = True
    boot_sot_fallback_active: bool = False
    boot_sot_fallback_reason: str = ""
    boot_sot_soft_fail: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "health_ok": self.health_ok,
            "trade_ready": self.trade_ready,
            "trading_path_live": self.trading_path_live,
            "trading_path_badge": self.trading_path_badge,
            "desk_rag": self.desk_rag,
            "desk_rag_label": self.desk_rag_label,
            "path_blockers": list(self.path_blockers),
            "broker_open": self.broker_open,
            "positions_verdict": self.positions_verdict,
            "sot_source": self.sot_source,
            "sot_age_sec": self.sot_age_sec,
            "sot_ok": self.sot_ok,
            "trade_support_running": self.trade_support_running,
            "rest_pressure_level": self.rest_pressure_level,
            "opm_tick_age_sec": self.opm_tick_age_sec,
            "opm_ok": self.opm_ok,
            "ui_up": self.ui_up,
            "ui_port": self.ui_port,
            "liveness_ok": self.liveness_ok,
            "liveness_issues": list(self.liveness_issues)[:8],
            "has_open_risk": self.has_open_risk,
            "entries_paused": self.entries_paused,
            "offline_for_dev": self.offline_for_dev,
            "manual_stop": self.manual_stop,
            "deploy_hold": self.deploy_hold,
            "feed_transport": dict(self.feed_transport),
            "cap_breach": self.cap_breach,
            "flat_book": self.flat_book,
            "boot_sot_fallback_active": self.boot_sot_fallback_active,
            "boot_sot_fallback_reason": self.boot_sot_fallback_reason,
            "boot_sot_soft_fail": self.boot_sot_soft_fail,
        }


def ingest_components(
    *,
    cfg: dict[str, Any] | None = None,
    in_process: bool = True,
) -> StabilityComponents:
    """Read-only ingest of all desk readiness planes."""
    conf = cfg or _load_cfg()
    c = StabilityComponents(ui_port=int(conf.get("ui_port", 3000)))
    api_port = int(conf.get("api_port", 8080))

    # --- health (HTTP — avoids circular imports with api.routes) ---
    health = _fetch_local("/api/health", port=api_port, timeout=2.5)
    if health:
        c.health_ok = bool(health.get("ok"))
        c.trade_ready = bool(health.get("trade_ready"))

    # --- path + desk_rag (prefer ops_strip composite when available) ---
    try:
        from runtime.trading_path_readiness import compute_trading_path_readiness

        path = compute_trading_path_readiness()
        c.trading_path_live = bool(path.get("trading_path_live"))
        c.trading_path_badge = str(path.get("badge") or "")
        blockers = path.get("blockers") or []
        c.path_blockers = [
            str(b.get("code") if isinstance(b, dict) else b) for b in blockers
        ][:8]
    except Exception:
        pass

    # --- positions / SoT ---
    pos: dict[str, Any] | None = None
    if in_process:
        try:
            from api.positions_live import build_live_positions_payload

            pos = build_live_positions_payload()
        except Exception:
            pos = None
    if pos is None:
        pos = _fetch_local("/api/positions/live", port=api_port, timeout=3.0) or {}
    c.broker_open = int(pos.get("count") or 0)
    c.positions_verdict = str(pos.get("verdict") or "")
    sot = pos.get("broker_open_sot") or {}
    c.sot_source = str(sot.get("source") or "")
    try:
        c.broker_open = max(c.broker_open, int(sot.get("count") or 0))
    except (TypeError, ValueError):
        pass
    ts_block = pos.get("trade_support") or {}
    try:
        age = ts_block.get("status_age_sec")
        if age is None:
            ts_file = data_dir() / "trade_support_status.json"
            if ts_file.is_file():
                raw = json.loads(ts_file.read_text(encoding="utf-8"))
                age = time.time() - float(raw.get("ts") or 0)
                c.trade_support_running = bool(raw.get("running", True))
                if sot.get("count") is None:
                    c.broker_open = max(c.broker_open, int(raw.get("broker_open") or 0))
        c.sot_age_sec = float(age) if age is not None else None
    except Exception:
        c.sot_age_sec = None
    stale_lim = trade_support_stale_budget_sec(cfg=conf)
    c.sot_ok = c.sot_age_sec is not None and c.sot_age_sec < stale_lim
    if not c.sot_source:
        c.sot_source = "trade_support" if c.sot_age_sec is not None else "unknown"

    # Boot stale-cache fallback circuit breaker — bypass network stall via
    # verified local broker_snapshot.json (never invent opens from empty stubs).
    try:
        from runtime.boot_sot_fallback import resolve_boot_sot_fallback

        network_stall = (
            c.sot_age_sec is None
            or (c.sot_age_sec is not None and c.sot_age_sec >= stale_lim)
        )
        fb = resolve_boot_sot_fallback(
            booting=boot_grace_active(cfg=conf),
            sot_age_sec=c.sot_age_sec,
            stale_budget_sec=stale_lim,
            network_timeout=network_stall and not c.sot_ok,
            sot_ok=c.sot_ok,
            broker_open=c.broker_open,
        )
        if fb.get("fallback_active"):
            c.boot_sot_fallback_active = True
            c.boot_sot_fallback_reason = str(fb.get("fallback_reason") or "")
            c.boot_sot_soft_fail = bool(fb.get("soft_fail"))
            c.sot_source = str(fb.get("sot_source") or c.sot_source)
            if fb.get("sot_age_sec") is not None:
                c.sot_age_sec = float(fb["sot_age_sec"])
            if fb.get("sot_ok"):
                c.sot_ok = True
                if fb.get("broker_open") is not None:
                    c.broker_open = int(fb["broker_open"])
            elif fb.get("soft_fail"):
                # Soft fail advances gate as non-hard freeze (handled in compute_boot_gate).
                c.sot_ok = False
    except Exception:
        pass

    try:
        from runtime.desk_support_wrapper import list_trade_support_pids

        pids = list_trade_support_pids()
        c.trade_support_running = bool(pids) or c.trade_support_running
    except Exception:
        pass

    c.has_open_risk = c.broker_open > 0
    c.flat_book = (
        c.broker_open <= 0
        and c.positions_verdict in ("FLAT", "HEALTHY", "")
    )

    # --- REST ---
    try:
        from system.rest_api_budget import get_rest_api_budget

        c.rest_pressure_level = str(
            get_rest_api_budget().metrics().get("pressure_level") or "IDLE"
        ).upper()
    except Exception:
        c.rest_pressure_level = "UNKNOWN"

    # --- OPM ---
    try:
        from runtime.open_position_manager import snapshot as mgr_snap

        mgr = mgr_snap() or {}
        last = float(mgr.get("last_tick_at") or 0)
        if last > 0:
            c.opm_tick_age_sec = max(0.0, time.time() - last)
        if c.has_open_risk:
            c.opm_ok = bool(mgr.get("active")) and (
                c.opm_tick_age_sec is None or c.opm_tick_age_sec <= 60.0
            )
        else:
            c.opm_ok = True  # flat: OPM idle is fine
    except Exception:
        c.opm_ok = not c.has_open_risk

    # --- UI ---
    c.ui_up = _port_listening(int(c.ui_port))

    # --- liveness ---
    try:
        from runtime.trading_desk_liveness import evaluate_liveness

        liv = evaluate_liveness()
        c.liveness_ok = bool(liv.get("ok"))
        c.liveness_issues = list(liv.get("issues") or [])[:8]
        c.has_open_risk = c.has_open_risk or bool(liv.get("has_open_risk"))
    except Exception:
        c.liveness_ok = None

    # --- pause / hold / offline ---
    try:
        from runtime.desk_dev_controls import entries_paused

        c.entries_paused = bool(entries_paused())
    except Exception:
        c.entries_paused = _flag_active("entry_halt.json") or _flag_active(
            "trading_paused.json"
        )
    c.offline_for_dev = _flag_active("offline_for_dev.json")
    try:
        from system.shutdown_cleanup import manual_stop_active

        c.manual_stop = bool(manual_stop_active(max_age_sec=86400.0))
    except Exception:
        c.manual_stop = _flag_active("manual_stop.json")
    try:
        from runtime.deploy_hold import is_deploy_hold_active

        c.deploy_hold = bool(is_deploy_hold_active())
    except Exception:
        c.deploy_hold = _flag_active("deploy_hold.json")

    # --- feed transport ---
    try:
        from runtime.feed_transport_summary import build_feed_transport_summary

        c.feed_transport = build_feed_transport_summary()
    except Exception:
        c.feed_transport = {"label": "FEED — unavailable"}

    # --- cap breach ---
    try:
        from runtime.broker_snapshot import open_count_from_snapshot
        from system.config_loader import get_config

        max_open = max(1, int(getattr(get_config(), "max_open_positions", 6) or 6))
        snap_n = open_count_from_snapshot(max_age_sec=300.0)
        if snap_n is not None and snap_n > max_open:
            if boot_grace_active(cfg=conf) and c.flat_book and c.broker_open <= 0:
                pass  # stale snapshot false cap during twin boot — live book flat
            else:
                c.cap_breach = True
    except Exception:
        pass

    # desk_rag alignment with ops_strip policy
    level = c.rest_pressure_level
    if c.cap_breach or level == "CRITICAL":
        c.desk_rag, c.desk_rag_label = "R", "RED — cap/REST critical"
    elif c.entries_paused or level in ("HIGH", "ELEVATED"):
        c.desk_rag, c.desk_rag_label = "A", "AMBER — entries paused or REST pressure"
    elif c.trading_path_live:
        c.desk_rag, c.desk_rag_label = "G", "GREEN — path live"
    else:
        c.desk_rag, c.desk_rag_label = "A", "AMBER — path not live"

    return c


def grade_stability(components: StabilityComponents) -> tuple[str, list[str]]:
    """
    Composite G|A|R policy.

    G only if path_live + SoT fresh + REST≤OK + UI up + no pause
    (or pause intentional with reason).
    """
    reasons: list[str] = []
    rest = str(components.rest_pressure_level or "UNKNOWN").upper()

    # --- RED ---
    if components.has_open_risk and not components.sot_ok:
        reasons.append("opens>0 and trade_support SoT stale/missing")
    if components.has_open_risk and not components.trade_support_running:
        reasons.append("opens>0 and trade_support not running")
    if components.has_open_risk and not components.opm_ok:
        reasons.append("opens>0 and OPM tick unhealthy")
    if components.cap_breach:
        reasons.append("max_open cap breach")
    if rest == "CRITICAL":
        reasons.append("REST CRITICAL")
    if components.positions_verdict == "CRITICAL":
        reasons.append("positions verdict CRITICAL")
    if components.offline_for_dev and components.has_open_risk:
        reasons.append("offline_for_dev with open risk")
    if reasons:
        return "R", reasons

    # --- AMBER ---
    if not components.ui_up:
        reasons.append(f"UI :{components.ui_port} down")
    if rest in ("HIGH", "ELEVATED"):
        reasons.append(f"REST {rest}")
    if not components.trading_path_live:
        badge = components.trading_path_badge or "path not live"
        reasons.append(f"path down: {badge}")
    if components.entries_paused:
        reasons.append("entries paused")
    if components.deploy_hold:
        reasons.append("deploy_hold active")
    if components.manual_stop:
        reasons.append("manual_stop / watchdog hold")
    if components.offline_for_dev:
        reasons.append("offline_for_dev")
    if components.liveness_ok is False and components.has_open_risk:
        reasons.append(
            "liveness degraded: "
            + ",".join(components.liveness_issues[:3] or ["unknown"])
        )
    # health.ok alone must never greenwash — but also must not amber the desk
    # when trade_ready + path_live already prove the hot path (iron_cage / feed
    # overlay can lag health.ok false while trading is live).
    if components.health_ok is False and not (
        components.trade_ready and components.trading_path_live
    ):
        reasons.append("health.ok false")
    if components.trade_ready is False and components.trading_path_live:
        reasons.append("trade_ready false while path claims live")
    if not components.sot_ok and not components.flat_book:
        reasons.append("SoT not fresh")
    if reasons:
        return "A", reasons

    # --- GREEN ---
    if (
        components.trading_path_live
        and components.sot_ok
        and rest in ("IDLE", "OK", "NORMAL", "UNKNOWN", "")
        and components.ui_up
        and not components.entries_paused
        and not components.offline_for_dev
        and not components.manual_stop
    ):
        return "G", ["path live · SoT fresh · REST ok · UI up · no pause"]

    # Fallback amber — incomplete green criteria
    if not components.sot_ok:
        reasons.append("SoT age unknown/stale (flat)")
    if not reasons:
        reasons.append("incomplete green criteria")
    return "A", reasons


def _cooldown_ready(action: str, cooldown_sec: float) -> bool:
    now = time.monotonic()
    last = float(_action_mono.get(action) or 0.0)
    if last > 0 and (now - last) < float(cooldown_sec):
        return False
    return True


def _mark_action(action: str) -> None:
    _action_mono[action] = time.monotonic()


def _heal_ui(conf: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"action": "heal_ui", "ok": False}
    if not conf.get("auto_heal_ui", True):
        out["skipped"] = True
        out["reason"] = "disabled"
        return out
    if not _cooldown_ready("heal_ui", float(conf.get("ui_heal_cooldown_sec", 180.0))):
        out["skipped"] = True
        out["reason"] = "cooldown"
        return out
    script = project_root() / "scripts" / "start_ui_background.sh"
    if not script.is_file():
        out["error"] = "start_ui_background.sh missing"
        return out
    try:
        subprocess.Popen(
            ["/bin/bash", str(script)],
            cwd=str(project_root()),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _mark_action("heal_ui")
        out["ok"] = True
        out["spawned"] = True
    except OSError as exc:
        out["error"] = str(exc)
    return out


def _heal_trade_support(conf: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"action": "heal_trade_support", "ok": False}
    if not conf.get("auto_heal_trade_support", True):
        out["skipped"] = True
        out["reason"] = "disabled"
        return out
    if not _cooldown_ready(
        "heal_trade_support", float(conf.get("heal_cooldown_sec", 120.0))
    ):
        out["skipped"] = True
        out["reason"] = "cooldown"
        return out
    try:
        from runtime.desk_support_wrapper import DeskSupportWrapper

        wrapper = DeskSupportWrapper(conf)
        result = wrapper.heal_trade_support()
        _mark_action("heal_trade_support")
        out.update(result if isinstance(result, dict) else {"result": result})
        out["ok"] = bool(out.get("ok"))
    except Exception as exc:
        # Fallback: spawn script only
        script = project_root() / "scripts" / "trade_support_wrapper.sh"
        if script.is_file():
            try:
                subprocess.Popen(
                    ["/bin/bash", str(script)],
                    cwd=str(project_root()),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                _mark_action("heal_trade_support")
                out["ok"] = True
                out["spawned_script"] = True
            except OSError as e2:
                out["error"] = str(e2)
        else:
            out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _pause_entries_safe(reason: str, conf: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"action": "pause_entries", "ok": False, "reason": reason}
    if not _cooldown_ready("pause_entries", float(conf.get("heal_cooldown_sec", 120.0))):
        out["skipped"] = True
        out["reason"] = "cooldown"
        return out
    try:
        from runtime.desk_dev_controls import entries_paused, pause_entries

        if entries_paused():
            out["skipped"] = True
            out["reason"] = "already_paused"
            out["ok"] = True
            return out
        pause_entries(reason=reason)
        _mark_action("pause_entries")
        out["ok"] = True
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def select_heals(
    components: StabilityComponents,
    grade: str,
    reasons: list[str],
    *,
    conf: dict[str, Any] | None = None,
) -> list[str]:
    """Decide safe auto-heal actions (no flatten, no kill -9)."""
    conf = conf or _load_cfg()
    if not conf.get("auto_heal_enabled", True):
        return []
    actions: list[str] = []

    # Never recover_and_supervise here — flat false-stale is desk_support's job
    # and already soft-filtered. This harness only does safe peripheral heals.

    if not components.ui_up:
        actions.append("heal_ui")

    if (
        (not components.sot_ok or not components.trade_support_running)
        and (components.has_open_risk or grade == "R")
    ):
        actions.append("heal_trade_support")

    if conf.get("auto_pause_on_rest_critical", True) and str(
        components.rest_pressure_level
    ).upper() == "CRITICAL":
        actions.append("pause_entries_rest_critical")

    if conf.get("auto_pause_on_cap_breach", True) and components.cap_breach:
        if not (boot_grace_active(cfg=conf) and components.flat_book):
            actions.append("pause_entries_cap_breach")

    return actions


def execute_heals(
    actions: list[str],
    *,
    conf: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    conf = conf or _load_cfg()
    results: list[dict[str, Any]] = []
    for action in actions:
        if dry_run:
            results.append({"action": action, "dry_run": True, "ok": True})
            continue
        if action == "heal_ui":
            results.append(_heal_ui(conf))
        elif action == "heal_trade_support":
            results.append(_heal_trade_support(conf))
        elif action == "pause_entries_rest_critical":
            results.append(
                _pause_entries_safe("stability_harness_rest_critical", conf)
            )
        elif action == "pause_entries_cap_breach":
            results.append(
                _pause_entries_safe("stability_harness_cap_breach", conf)
            )
        else:
            results.append({"action": action, "skipped": True, "reason": "unknown"})
    return results


def compute_boot_gate(
    components: StabilityComponents,
    grade: str,
    reasons: list[str],
    *,
    actions_taken: list[dict[str, Any]] | None = None,
    actions_planned: list[str] | None = None,
) -> dict[str, Any]:
    """
    Splash confidence gate — ready_for_desk only when trading can occur.

    Means: path armed to enter when signals fire. NOT a guarantee of an
    immediate fill. Never greenwash healing/fail into ready.
    """
    actions_taken = list(actions_taken or [])
    actions_planned = list(actions_planned or [])
    started = boot_started_at()
    elapsed = max(0.0, time.time() - started)
    rest = str(components.rest_pressure_level or "UNKNOWN").upper()
    grade_u = str(grade or "A").upper()
    rag = str(components.desk_rag or grade_u).upper()

    healing_now = any(
        (isinstance(a, dict) and a.get("ok") and not a.get("skipped") and not a.get("planned"))
        or (isinstance(a, dict) and a.get("spawned"))
        for a in actions_taken
    ) or bool(actions_planned)

    def _check(
        cid: str,
        *,
        status: str,
        detail: str,
    ) -> dict[str, Any]:
        return {"id": cid, "status": status, "detail": detail}

    checks: list[dict[str, Any]] = []
    blockers: list[str] = []

    # --- agent / trade_ready ---
    if components.trade_ready:
        checks.append(_check("trade_ready", status="pass", detail="trade_ready=true"))
    elif components.health_ok:
        checks.append(
            _check(
                "trade_ready",
                status="warn",
                detail="health.ok but trade_ready false — gates still hydrating",
            )
        )
        blockers.append("trade_ready_false")
    else:
        checks.append(
            _check("trade_ready", status="fail", detail="agent health/trade_ready not ready")
        )
        blockers.append("agent_not_ready")

    # --- path live ---
    if components.trading_path_live:
        checks.append(
            _check(
                "trading_path_live",
                status="pass",
                detail=components.trading_path_badge or "path live",
            )
        )
    else:
        checks.append(
            _check(
                "trading_path_live",
                status="fail",
                detail=components.trading_path_badge or "path not live",
            )
        )
        blockers.append("path_not_live")

    # --- desk RAG / stability grade ---
    if grade_u == "R" or rag == "R":
        checks.append(
            _check("desk_rag", status="fail", detail=f"grade={grade_u} rag={rag}")
        )
        blockers.append("desk_critical")
    elif grade_u == "G" and rag == "G":
        checks.append(_check("desk_rag", status="pass", detail="G — path live"))
    else:
        checks.append(
            _check(
                "desk_rag",
                status="warn",
                detail=f"grade={grade_u} rag={rag} — {'; '.join(reasons[:2]) or 'amber'}",
            )
        )
        # Amber does not always block — only when path/SoT/rest fail below

    # --- SoT (boot snapshot fallback advances gate; soft-fail avoids freeze) ---
    if components.sot_ok:
        detail = (
            f"{components.sot_source} age={components.sot_age_sec}s "
            f"opens={components.broker_open}"
        )
        if components.boot_sot_fallback_active:
            detail = f"{detail} [boot_fallback]"
        checks.append(
            _check(
                "trade_support_sot",
                status="pass",
                detail=detail,
            )
        )
    elif components.boot_sot_soft_fail and boot_grace_active():
        # Soft fail during boot — documented reason, not infinite GATE HOLD.
        checks.append(
            _check(
                "trade_support_sot",
                status="warn",
                detail=(
                    f"boot_fallback soft_fail:"
                    f"{components.boot_sot_fallback_reason or 'snapshot_unavailable'} "
                    f"(age={components.sot_age_sec})"
                ),
            )
        )
        # Do not append sot_stale hard blocker during boot soft-fail.
    else:
        st = "healing" if healing_now and "heal_trade_support" in actions_planned else "fail"
        checks.append(
            _check(
                "trade_support_sot",
                status=st,
                detail=f"SoT not fresh (age={components.sot_age_sec})",
            )
        )
        blockers.append("sot_stale")

    if not components.trade_support_running and components.has_open_risk:
        checks.append(
            _check("trade_support_proc", status="fail", detail="trade_support not running with opens")
        )
        blockers.append("trade_support_down")
    elif components.trade_support_running:
        checks.append(_check("trade_support_proc", status="pass", detail="trade_support running"))
    else:
        checks.append(
            _check(
                "trade_support_proc",
                status="warn",
                detail="trade_support not confirmed (flat book)",
            )
        )

    # --- REST ---
    if rest in ("CRITICAL", "HIGH"):
        checks.append(_check("rest_pressure", status="fail", detail=f"REST {rest}"))
        blockers.append(f"rest_{rest.lower()}")
    elif rest in ("ELEVATED",):
        checks.append(
            _check(
                "rest_pressure",
                status="warn",
                detail="REST ELEVATED — entries may be paused by budget",
            )
        )
        blockers.append("rest_elevated")
    else:
        checks.append(_check("rest_pressure", status="pass", detail=f"REST {rest or 'OK'}"))

    # --- pause / offline ---
    if components.offline_for_dev:
        checks.append(_check("offline_for_dev", status="fail", detail="offline_for_dev active"))
        blockers.append("offline_for_dev")
    else:
        checks.append(_check("offline_for_dev", status="pass", detail="online"))

    if components.entries_paused:
        checks.append(
            _check(
                "entries_paused",
                status="fail",
                detail="entry_halt / trading_paused — intentional soak blocks desk ready",
            )
        )
        blockers.append("entries_paused")
    else:
        checks.append(_check("entries_paused", status="pass", detail="entries not paused"))

    if components.manual_stop:
        checks.append(_check("manual_stop", status="warn", detail="watchdog manual_stop held"))
        # Soft — deploy cutover may leave briefly; only hard-block if path also down
        if not components.trading_path_live:
            blockers.append("manual_stop")

    # --- OPM ---
    if components.has_open_risk and not components.opm_ok:
        checks.append(_check("opm", status="fail", detail="OPM unhealthy with opens"))
        blockers.append("opm_unhealthy")
    elif components.opm_ok:
        checks.append(
            _check(
                "opm",
                status="pass",
                detail=f"OPM ok (tick_age={components.opm_tick_age_sec})",
            )
        )
    else:
        checks.append(_check("opm", status="warn", detail="OPM not confirmed"))

    # --- agent API reachable (splash polls this) ---
    if components.trade_ready or components.health_ok is True:
        checks.append(_check("agent_api", status="pass", detail=":8080 answering"))
    elif components.health_ok is False:
        checks.append(_check("agent_api", status="fail", detail="health.ok false"))
        blockers.append("agent_api")
    else:
        checks.append(_check("agent_api", status="warn", detail="health unknown"))

    # --- liveness ---
    if components.liveness_ok is False and components.has_open_risk:
        checks.append(
            _check(
                "liveness",
                status="fail",
                detail=",".join(components.liveness_issues[:3]) or "degraded",
            )
        )
        blockers.append("liveness_degraded")
    elif components.liveness_ok is True:
        checks.append(_check("liveness", status="pass", detail="liveness ok"))
    else:
        checks.append(_check("liveness", status="warn", detail="liveness unknown"))

    if healing_now:
        for a in actions_planned:
            checks.append(
                _check(f"heal:{a}", status="healing", detail=f"auto-heal planned: {a}")
            )
        for a in actions_taken:
            if isinstance(a, dict) and a.get("ok") and not a.get("planned"):
                checks.append(
                    _check(
                        f"heal_done:{a.get('action')}",
                        status="healing",
                        detail=f"heal executed: {a.get('action')}",
                    )
                )
        blockers.append("healing_in_progress")

    # Cap breach always blocks
    if components.cap_breach:
        blockers.append("cap_breach")
        checks.append(_check("cap", status="fail", detail="max_open cap breach"))

    # ready_for_desk — strict: no blockers, path live, SoT ok, REST ok/idle,
    # not paused, trade_ready, grade not R
    hard_blockers = [
        b
        for b in blockers
        if b
        not in (
            # warn-only: amber RAG without hard fail already listed
        )
    ]
    # Snapshot hydrate advances SoT plane; soft-fail only demotes to warn (no freeze).
    sot_plane_ok = bool(components.sot_ok)
    ready = (
        bool(components.trade_ready)
        and bool(components.trading_path_live)
        and grade_u != "R"
        and rag != "R"
        and sot_plane_ok
        and rest not in ("CRITICAL", "HIGH", "ELEVATED")
        and not components.entries_paused
        and not components.offline_for_dev
        and not components.cap_breach
        and "healing_in_progress" not in blockers
        and "sot_stale" not in blockers
        and "path_not_live" not in blockers
        and "agent_not_ready" not in blockers
        and "trade_ready_false" not in blockers
        and "opm_unhealthy" not in blockers
        and "liveness_degraded" not in blockers
        and "desk_critical" not in blockers
    )

    try:
        from runtime.desk_upgrade_manifest import upgrades_live

        live_upgrades = upgrades_live(12)
    except Exception:
        live_upgrades = []

    stuck = elapsed > 300.0 and not ready
    operator_hints: list[str] = []
    if stuck:
        operator_hints = [
            "Check curl -s http://127.0.0.1:8080/api/desk/stability",
            "If entries paused: scripts/desk_dev_pause.sh status|resume",
            "If UI stale: scripts/start_ui_background.sh (never kill -9 main)",
            "If SoT stale: launchctl kickstart trade_support or desk_deploy audit",
            "Flat deploy: ./scripts/desk_deploy.sh deploy",
        ]

    return {
        "ready_for_desk": bool(ready),
        "boot_started_at": started,
        "boot_grace_active": boot_grace_active(),
        "boot_latency_buffer_sec": boot_latency_buffer_sec(),
        "boot_latency_buffer_active": boot_latency_buffer_active(),
        "false_engine_blockage_suppressed": false_engine_blockage_suppressed(),
        "sot_stale_budget_sec": trade_support_stale_budget_sec(),
        "boot_sot_fallback": bool(components.boot_sot_fallback_active),
        "boot_sot_fallback_reason": components.boot_sot_fallback_reason or None,
        "boot_sot_soft_fail": bool(components.boot_sot_soft_fail),
        "elapsed_sec": round(elapsed, 1),
        "checks": checks,
        "healing_actions": actions_planned
        + [
            str(a.get("action"))
            for a in actions_taken
            if isinstance(a, dict) and a.get("ok") and not a.get("planned")
        ],
        "blockers": hard_blockers,
        "upgrades_live": live_upgrades,
        "stuck": stuck,
        "operator_hints": operator_hints,
        "promise": (
            "ready_for_desk means path is armed to enter when signals fire — "
            "not a guarantee of an immediate fill"
        ),
    }


def evaluate_stability(
    *,
    act: bool = False,
    in_process: bool = True,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full evaluate → grade → optional heal. Returns desk_stability payload."""
    conf = cfg or _load_cfg()
    components = ingest_components(cfg=conf, in_process=in_process)
    grade, reasons = grade_stability(components)
    planned = select_heals(components, grade, reasons, conf=conf)
    actions_taken: list[dict[str, Any]] = []
    if act and planned and not _harness_mode():
        actions_taken = execute_heals(planned, conf=conf, dry_run=False)
        _audit(
            "heal",
            {
                "grade": grade,
                "reasons": reasons,
                "planned": planned,
                "results": actions_taken,
            },
        )
    elif planned:
        actions_taken = [{"action": a, "planned": True} for a in planned]

    feed_label = ""
    if isinstance(components.feed_transport, dict):
        feed_label = str(components.feed_transport.get("label") or "")

    boot_gate = compute_boot_gate(
        components,
        grade,
        reasons,
        actions_taken=actions_taken,
        actions_planned=planned,
    )

    payload = {
        "ok": True,
        "ts": time.time(),
        "desk_stability": {
            "grade": grade,
            "reasons": reasons,
            "actions_taken": actions_taken,
            "actions_planned": planned,
            "components": components.as_dict(),
            "label": f"{grade} — " + ("; ".join(reasons[:3]) if reasons else "stable"),
            "feed": feed_label,
            "boot_gate": boot_gate,
        },
        "boot_gate": boot_gate,
        "ready_for_desk": bool(boot_gate.get("ready_for_desk")),
        # Convenience top-level aliases for Terminal / ops_strip
        "grade": grade,
        "reasons": reasons,
        "label": f"{grade} — " + ("; ".join(reasons[:3]) if reasons else "stable"),
    }
    with _lock:
        global _last_payload
        _last_payload = dict(payload)
    return payload


def latest_stability() -> dict[str, Any]:
    with _lock:
        return dict(_last_payload) if _last_payload else {}


def stability_tick(*, act: bool = True) -> dict[str, Any]:
    return evaluate_stability(act=act, in_process=True)


def start_desk_stability_harness(cfg: Any | None = None) -> None:
    """Background daemon — PERF lane, not tick path. Skip under test harness."""
    global _thread
    if _harness_mode():
        log_engine("DeskStability: skipped (IG_TEST_HARNESS)")
        return
    conf = _load_cfg(cfg)
    if not conf.get("enabled", True):
        log_engine("DeskStability: disabled in config")
        return
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()

        def _loop() -> None:
            interval = max(15.0, float(conf.get("poll_interval_sec", 25.0)))
            log_engine(
                f"DeskStability: harness armed poll={interval}s "
                f"ui=:{conf.get('ui_port')} auto_heal={conf.get('auto_heal_enabled')}"
            )
            _audit("wrapper_start", {"poll_interval_sec": interval})
            # First tick after short settle so boot REST doesn't collide.
            time.sleep(8.0)
            while not _stop.is_set():
                try:
                    stability_tick(act=True)
                except Exception as exc:
                    _audit("tick_error", {"error": f"{type(exc).__name__}: {exc}"})
                _stop.wait(interval)

        _thread = threading.Thread(
            target=_loop, name="desk-stability-harness", daemon=True
        )
        _thread.start()


def stop_desk_stability_harness() -> None:
    global _thread
    _stop.set()
    t = _thread
    if t is not None and t.is_alive():
        t.join(timeout=2.0)
    _thread = None


def reset_desk_stability_harness_for_tests() -> None:
    global _last_payload, _action_mono, _boot_started_at
    stop_desk_stability_harness()
    with _lock:
        _last_payload = {}
        _action_mono = {}
        _boot_started_at = time.time()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Desk stability harness — one-shot")
    parser.add_argument("--once", action="store_true", help="Evaluate once and exit")
    parser.add_argument(
        "--act",
        action="store_true",
        help="Allow safe auto-heals (default: observe only)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Print full JSON payload"
    )
    args = parser.parse_args(argv)
    # CLI observe mode uses HTTP + disk; avoid heavy in-process imports when
    # agent isn't this process.
    payload = evaluate_stability(act=bool(args.act), in_process=False)
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        ds = payload.get("desk_stability") or {}
        print(f"grade={ds.get('grade')}  {ds.get('label')}")
        for r in ds.get("reasons") or []:
            print(f"  - {r}")
        comps = ds.get("components") or {}
        print(
            f"  path_live={comps.get('trading_path_live')} "
            f"sot_ok={comps.get('sot_ok')} ui={comps.get('ui_up')} "
            f"rest={comps.get('rest_pressure_level')} "
            f"opens={comps.get('broker_open')}"
        )
        feed = comps.get("feed_transport") or {}
        if feed.get("label"):
            print(f"  feed: {feed.get('label')}")
        planned = ds.get("actions_planned") or []
        if planned:
            print(f"  planned: {planned}")
    return 0 if (payload.get("desk_stability") or {}).get("grade") != "R" else 2


if __name__ == "__main__":
    raise SystemExit(main())
