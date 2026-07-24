"""GUI / Trading Desk supervisor — Phase 1 observe/score + Phase 2 allowlisted heal.

Phase 1: observe + score + write resolve queue + cursor_handoff + dashboard chip.
Phase 2: allowlisted self-heal only (see gui_desk_supervisor_heal.py).
Writes SoT under IG_DATA_ROOT for Cursor / operator handoff.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PHASE = 2
SCHEMA_VERSION = 3
DEFAULT_PORTS = {
    "cfd": int(os.environ.get("IG_GUI_SUP_CFD_PORT", "8080")),
    "sb": int(os.environ.get("IG_GUI_SUP_SB_PORT", "8081")),
    "ui": int(os.environ.get("IG_GUI_SUP_UI_PORT", "3000")),
}
HTTP_TIMEOUT_SEC = float(os.environ.get("IG_GUI_SUP_HTTP_TIMEOUT", "2.5"))
CASH_NEAR_EQUAL_GBP = 2.0
HISTORY_MAX_LINES = 500
LOG_TAIL_BYTES = int(os.environ.get("IG_GUI_SUP_LOG_TAIL_BYTES", "65536"))
DOW_EPIC = "IX.D.DOW.IFM.IP"
SILENCE_MINUTES = float(os.environ.get("IG_GUI_SUP_SILENCE_MINUTES", "30"))
AUTO_HEAL_DEFAULT = os.environ.get("IG_GUI_SUP_AUTO_HEAL", "0").strip() in ("1", "true", "True", "yes")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _data_root() -> Path:
    env = (os.environ.get("IG_DATA_ROOT") or os.environ.get("IG_AGENT_DATA_DIR") or "").strip()
    if env:
        return Path(env)
    try:
        from system.paths import data_dir

        return Path(data_dir())
    except Exception:
        return _repo_root() / "src" / "data" / "v31-production"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _http_json(url: str, *, timeout: float = HTTP_TIMEOUT_SEC) -> tuple[dict[str, Any] | None, str | None]:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "gui-desk-supervisor/1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw.strip() else {}
            if not isinstance(data, dict):
                return {"_non_object": data}, None
            return data, None
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        return None, f"http_{exc.code}:{body or exc.reason}"
    except Exception as exc:
        return None, f"{type(exc).__name__}:{exc}"


def _tcp_reachable(host: str, port: int, *, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _finding(
    *,
    rank: int,
    severity: str,
    klass: str,
    title: str,
    detail: str,
    needs_code: bool = False,
    needs_ops: bool = False,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "rank": rank,
        "severity": severity,  # info | watch | fail
        "class": klass,  # ops | ui | code | strategy | ignore
        "title": title,
        "detail": detail,
        "needs_code": bool(needs_code),
        "needs_ops": bool(needs_ops),
        "evidence": evidence or {},
    }


def _port_bundle(base: str, *, listen: bool | None = None) -> dict[str, Any]:
    health, health_err = _http_json(f"{base}/api/health")
    positions, pos_err = _http_json(f"{base}/api/positions/live")
    trade_support, ts_err = _http_json(f"{base}/api/trade_support/status")
    liveness, live_err = _http_json(f"{base}/api/trading_desk/liveness")
    stability, stab_err = _http_json(f"{base}/api/desk/stability")
    ops_strip, ops_err = _http_json(f"{base}/api/desk/ops_strip")
    accounting, acct_err = _http_json(f"{base}/api/desk/simplified_accounting")
    rotation, rot_err = _http_json(f"{base}/api/rotation_state")
    state, state_err = _http_json(f"{base}/api/state")
    reachable = health is not None or positions is not None
    err_blob = f"{health_err or ''}|{pos_err or ''}"
    timeoutish = any(
        tok in err_blob
        for tok in ("Timeout", "timed out", "timeout", "URLError", "RemoteDisconnected")
    )
    hung_api = bool(listen) and (not reachable) and (timeoutish or bool(health_err or pos_err))
    return {
        "base": base,
        "reachable": reachable,
        "listen": bool(listen) if listen is not None else None,
        "hung_api": hung_api,
        "health": health,
        "health_err": health_err,
        "positions": positions,
        "positions_err": pos_err,
        "trade_support": trade_support,
        "trade_support_err": ts_err,
        "liveness": liveness,
        "liveness_err": live_err,
        "stability": stability,
        "stability_err": stab_err,
        "ops_strip": ops_strip,
        "ops_strip_err": ops_err,
        "accounting": accounting,
        "accounting_err": acct_err,
        "rotation": rotation,
        "rotation_err": rot_err,
        "state": state,
        "state_err": state_err,
    }


def _silence_state_path(data_root: Path) -> Path:
    return data_root / "state" / "gui_supervisor_silence.json"


def _read_gate_funnel(data_root: Path) -> dict[str, Any]:
    path = data_root / "gate_funnel_report.json"
    if not path.is_file():
        alt = data_root / "reports" / "gate_funnel_report.json"
        path = alt if alt.is_file() else path
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _update_silence_tracker(
    *,
    data_root: Path,
    sb_armed: bool,
    activity: bool,
    now: float | None = None,
) -> dict[str, Any]:
    """Persist armed-since / last-activity for zero-attempt silence timer."""
    now = float(now if now is not None else time.time())
    path = _silence_state_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    state: dict[str, Any] = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state = raw
        except Exception:
            state = {}
    if not sb_armed:
        state = {
            "sb_armed": False,
            "armed_since": None,
            "last_activity_ts": None,
            "silence_sec": 0.0,
            "updated_at": now,
        }
    else:
        armed_since = state.get("armed_since")
        if armed_since is None:
            armed_since = now
        last_activity = state.get("last_activity_ts")
        if activity:
            last_activity = now
        if last_activity is None:
            last_activity = float(armed_since)
        silence_sec = max(0.0, now - float(last_activity))
        state = {
            "sb_armed": True,
            "armed_since": float(armed_since),
            "last_activity_ts": float(last_activity),
            "silence_sec": silence_sec,
            "silence_minutes": round(silence_sec / 60.0, 2),
            "threshold_minutes": SILENCE_MINUTES,
            "updated_at": now,
        }
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return state


def _read_a2_marker(data_root: Path) -> dict[str, Any] | None:
    path = data_root / "state_cfd" / "a2_entries_paused.json"
    if not path.is_file():
        # legacy / alternate
        alt = data_root / "state" / "a2_entries_paused.json"
        path = alt if alt.is_file() else path
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {"_raw": raw, "path": str(path)}
    except Exception as exc:
        return {"active": None, "error": f"{type(exc).__name__}:{exc}", "path": str(path)}


def _blotter_quality(accounting: dict[str, Any] | None) -> dict[str, Any]:
    rows = []
    if isinstance(accounting, dict):
        rows = list(accounting.get("last_10_closed_trades") or [])
    if not rows:
        return {
            "rows": 0,
            "with_account": 0,
            "with_product": 0,
            "shared_journal_smell": 0,
            "deal_id_as_asset": 0,
            "thin_agent_payload": False,
        }
    with_account = 0
    with_product = 0
    smell = 0
    deal_as_asset = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        aid = str(r.get("account_id") or r.get("AccountID") or "").strip()
        pt = str(r.get("product_type") or r.get("ProductType") or r.get("product") or "").strip()
        asset = str(r.get("asset") or "").strip()
        if aid:
            with_account += 1
        if pt:
            with_product += 1
        if aid.upper() in ("SHARED", "JOURNAL") or pt.upper() in ("SHARED", "JOURNAL"):
            smell += 1
        if asset.upper().startswith("DIAAAA") or asset.upper().startswith("DIAAAAX"):
            deal_as_asset += 1
    thin = with_account == 0 and with_product == 0 and len(rows) > 0
    return {
        "rows": len(rows),
        "with_account": with_account,
        "with_product": with_product,
        "shared_journal_smell": smell,
        "deal_id_as_asset": deal_as_asset,
        "thin_agent_payload": thin,
    }


def _cash_merge_sanity(cfd_acct: dict[str, Any] | None, sb_acct: dict[str, Any] | None) -> dict[str, Any]:
    def _today(a: dict[str, Any] | None) -> float | None:
        if not isinstance(a, dict):
            return None
        try:
            return float(a.get("today_net_realized_pnl_gbp"))
        except (TypeError, ValueError):
            return None

    a = _today(cfd_acct)
    b = _today(sb_acct)
    out: dict[str, Any] = {
        "cfd_today_gbp": a,
        "sb_today_gbp": b,
        "cfd_source": (cfd_acct or {}).get("source") if isinstance(cfd_acct, dict) else None,
        "sb_source": (sb_acct or {}).get("source") if isinstance(sb_acct, dict) else None,
        "mode": "unknown",
        "double_count_risk": False,
        "merged_today_gbp": None,
    }
    if a is None and b is None:
        out["mode"] = "missing"
        return out
    if a is None:
        out["mode"] = "sb_only"
        out["merged_today_gbp"] = b
        return out
    if b is None:
        out["mode"] = "cfd_only"
        out["merged_today_gbp"] = a
        return out
    delta = abs(a - b)
    if delta <= CASH_NEAR_EQUAL_GBP:
        out["mode"] = "shared_journal_once"
        out["merged_today_gbp"] = round(a, 4)
        out["double_count_risk"] = False
        # Classic bug: GUI would show ~2x if it summed identical clones.
        out["would_look_like_if_double"] = round(a + b, 4)
        return out
    # Divergent books — merge should sum; flag only if one looks like clone of other abs-wise wrong
    out["mode"] = "dual_independent"
    out["merged_today_gbp"] = round(a + b, 4)
    out["delta_gbp"] = round(delta, 4)
    # If abs(a)≈abs(b) and signs match and |a|+|b|≈2*|a|, already covered by near-equal.
    # If one is nearly 2x the other of same sign, note possible prior double-write.
    if abs(a) > 1 and abs(abs(a) - 2 * abs(b)) <= CASH_NEAR_EQUAL_GBP:
        out["double_count_risk"] = True
        out["mode"] = "possible_double_write_skew"
    elif abs(b) > 1 and abs(abs(b) - 2 * abs(a)) <= CASH_NEAR_EQUAL_GBP:
        out["double_count_risk"] = True
        out["mode"] = "possible_double_write_skew"
    return out


def _rest_pressure(bundle: dict[str, Any]) -> dict[str, Any]:
    ops = bundle.get("ops_strip") if isinstance(bundle.get("ops_strip"), dict) else {}
    stab = bundle.get("stability") if isinstance(bundle.get("stability"), dict) else {}
    desk = stab.get("desk_stability") if isinstance(stab.get("desk_stability"), dict) else stab
    level = str(
        ops.get("rest_pressure_level")
        or (ops.get("rest_pressure") or {}).get("level")
        or ""
    ).upper()
    calls = ops.get("rest_calls_last_minute")
    reasons = desk.get("reasons") if isinstance(desk, dict) else None
    if not level and isinstance(reasons, list):
        joined = " ".join(str(r) for r in reasons).upper()
        if "REST CRITICAL" in joined:
            level = "CRITICAL"
        elif "REST ELEVATED" in joined or "REST HIGH" in joined:
            level = "ELEVATED"
        elif "REST OK" in joined or "REST IDLE" in joined:
            level = "OK"
    return {
        "level": level or "UNKNOWN",
        "calls_last_minute": calls,
        "stability_grade": (desk or {}).get("grade") if isinstance(desk, dict) else None,
        "stability_reasons": reasons if isinstance(reasons, list) else [],
    }


def _loops_plane(health: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(health, dict):
        return {}
    bm = health.get("boot_metrics") if isinstance(health.get("boot_metrics"), dict) else {}
    ss = bm.get("system_state") if isinstance(bm.get("system_state"), dict) else {}
    loops = ss.get("loops") if isinstance(ss.get("loops"), dict) else None
    if loops is None and isinstance(bm.get("loops"), dict):
        loops = bm.get("loops")
    if loops is None and isinstance(health.get("loops"), dict):
        loops = health.get("loops")
    loops = loops if isinstance(loops, dict) else {}
    iron = health.get("iron_cage") if isinstance(health.get("iron_cage"), dict) else {}
    exec_plane = iron.get("execution") if isinstance(iron.get("execution"), dict) else {}
    return {
        "built": loops.get("built"),
        "running": loops.get("running"),
        "accepting_ticks": loops.get("accepting_ticks"),
        "boot_ready": bm.get("ready") if "ready" in bm else ss.get("ready"),
        "boot_percent": bm.get("percent") if bm.get("percent") is not None else ss.get("percent"),
        "boot_label": bm.get("label") or ss.get("phase_label"),
        "boot_error": bm.get("error") or ss.get("error"),
        "loop_active": exec_plane.get("loop_active"),
        "trade_ready": health.get("trade_ready") if "trade_ready" in health else iron.get("trade_ready"),
        "trading_paused": health.get("trading_paused"),
    }


def _routing_posture(state: dict[str, Any] | None, epic: str = DOW_EPIC) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {}
    rows = state.get("routing") or []
    if not isinstance(rows, list):
        return {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("epic") or "") != epic:
            continue
        cf = row.get("contributing_factors") if isinstance(row.get("contributing_factors"), dict) else {}
        enf = cf.get("enforcement") if isinstance(cf.get("enforcement"), dict) else {}
        allow = [str(x) for x in (enf.get("hard_allow") or [])]
        block = [str(x) for x in (enf.get("hard_block") or [])]
        flags = [str(x) for x in (row.get("route_flags") or [])]
        return {
            "epic": epic,
            "execution_path": row.get("execution_path"),
            "hard_allow": allow,
            "hard_block": block,
            "hard_active": enf.get("hard_active"),
            "controller_ownership": cf.get("controller_ownership"),
            "strategy": cf.get("strategy"),
            "route_flags": flags,
            "ml_confidence": state.get("ml_confidence"),
            "signal_strength": state.get("signal_strength"),
        }
    return {
        "epic": epic,
        "ml_confidence": state.get("ml_confidence"),
        "signal_strength": state.get("signal_strength"),
    }


def _ranked_from_rotation(rotation: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(rotation, dict):
        return {"active": False, "promoted": [], "mode": "off"}
    rot = rotation.get("rotation") if isinstance(rotation.get("rotation"), dict) else rotation
    ranked = rot.get("ranked_rotator") if isinstance(rot.get("ranked_rotator"), dict) else {}
    promoted = [str(e) for e in (ranked.get("promoted") or []) if str(e).strip()]
    active_stack = []
    for row in rot.get("active_instruments") or []:
        if isinstance(row, dict) and row.get("epic"):
            active_stack.append(str(row["epic"]))
    return {
        "active": bool(ranked.get("active")),
        "mode": str(ranked.get("mode") or "off"),
        "dominant": ranked.get("dominant"),
        "promoted": promoted,
        "reason": ranked.get("reason"),
        "active_stack": active_stack,
    }


def _read_dual_core_posture(repo: Path | None = None) -> dict[str, Any]:
    """Cheap disk read of dual_core / dual_regime posture (no agent mutate)."""
    root = repo or _repo_root()
    out: dict[str, Any] = {
        "exclude_from_hot_path": [],
        "ranked_candidate_epics": [],
        "sb_hot_path_allowlist": [],
        "ranked_rotator_mode": None,
        "sb_macro_ltr_entries_only": None,
        "sb_disable_instant_micro": None,
        "sb_disable_core_b_micro": None,
        "sources": [],
    }
    for rel in (
        "config/config_v31_demo_throughput.json",
        "config/tuning_overlay.json",
    ):
        path = root / rel
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        out["sources"].append(rel)
        dual = raw.get("dual_core") if isinstance(raw.get("dual_core"), dict) else {}
        regime = raw.get("dual_regime") if isinstance(raw.get("dual_regime"), dict) else {}
        if "exclude_from_hot_path" in dual:
            out["exclude_from_hot_path"] = [
                str(e) for e in (dual.get("exclude_from_hot_path") or []) if str(e).strip()
            ]
        if "ranked_candidate_epics" in dual:
            out["ranked_candidate_epics"] = [
                str(e) for e in (dual.get("ranked_candidate_epics") or []) if str(e).strip()
            ]
        if "sb_hot_path_allowlist" in dual:
            out["sb_hot_path_allowlist"] = [
                str(e) for e in (dual.get("sb_hot_path_allowlist") or []) if str(e).strip()
            ]
        if "ranked_rotator_mode" in dual:
            out["ranked_rotator_mode"] = bool(dual.get("ranked_rotator_mode"))
        for key in (
            "sb_macro_ltr_entries_only",
            "sb_disable_instant_micro",
            "sb_disable_core_b_micro",
        ):
            if key in regime:
                out[key] = bool(regime.get(key))
    # Derived: carve expected when SB macro LTR entries-only (or both micro disables).
    carve = out.get("sb_macro_ltr_entries_only")
    if carve is None:
        carve = bool(out.get("sb_disable_instant_micro")) and bool(out.get("sb_disable_core_b_micro"))
    out["sb_path_a_carve_expected"] = bool(carve)
    return out


def _cheap_log_tick_smell(log_path: Path, *, max_bytes: int = LOG_TAIL_BYTES) -> dict[str, Any]:
    """Bounded tail of agent log — dormant vs entering tick loop counts only."""
    out: dict[str, Any] = {
        "path": str(log_path),
        "ok": False,
        "dormant_hits": 0,
        "entering_tick_hits": 0,
        "bytes_read": 0,
    }
    try:
        if not log_path.is_file():
            out["missing"] = True
            return out
        size = int(log_path.stat().st_size)
        with log_path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(-max_bytes, os.SEEK_END)
            raw = fh.read()
        text = raw.decode("utf-8", errors="replace")
        out["ok"] = True
        out["bytes_read"] = len(raw)
        out["file_size"] = size
        out["dormant_hits"] = text.count("dormant (paused_at_boot)")
        out["entering_tick_hits"] = text.count("entering tick loop")
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}:{exc}"
    return out


def _suspected_files_for(title: str, detail: str = "") -> list[str]:
    blob = f"{title} {detail}".lower()
    files: list[str] = []
    if any(x in blob for x in ("accepting_ticks", "tick loop", "paused_at_boot", "dormant")):
        files += [
            "src/runtime/market_orchestrator.py",
            "src/trading/trading_loop.py",
            "src/system/boot/post_ready_services.py",
        ]
    if any(x in blob for x in ("path a", "path_a", "micro hard", "carve", "hard_allow", "hard_block")):
        files += [
            "src/system/dual_regime.py",
            "src/runtime/hard_enforcement.py",
            "src/runtime/strategy_controller.py",
            "src/runtime/strategy_enforcement.py",
        ]
    if any(x in blob for x in ("ranked", "promot", "allowlist", "exclude_from_hot")):
        files += [
            "src/runtime/rotation_failover.py",
            "src/runtime/dual_core_execution.py",
            "config/config_v31_demo_throughput.json",
            "config/tuning_overlay.json",
        ]
    if any(x in blob for x in ("ml_confidence", "vector", "warm", "signal_strength")):
        files += [
            "src/system/boot/post_ready_services.py",
            "src/api/health_light.py",
            "src/alpha/micro_sniper_ml.py",
        ]
    if "rest" in blob:
        files += ["src/api/rest_api_budget.py", "config/config_v31_demo_throughput.json"]
    if "blotter" in blob or "accountid" in blob:
        files += ["src/api/desk_accounting.py", "terminal/src/lib/desk-accounting-merge.ts"]
    if "cash" in blob or "double-count" in blob:
        files += ["terminal/src/lib/desk-accounting-merge.ts", "terminal/src/hooks/useDeskCapital.ts"]
    # de-dupe preserve order
    seen: set[str] = set()
    ordered: list[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            ordered.append(f)
    return ordered or [
        "src/runtime/gui_desk_supervisor.py",
        "docs/GUI_DESK_SUPERVISOR.md",
    ]


def _build_cursor_handoff(
    *,
    score: str,
    findings: list[dict[str, Any]],
) -> dict[str, Any] | None:
    code_findings = [
        f
        for f in findings
        if f.get("needs_code") and f.get("severity") in ("fail", "watch")
    ]
    if not code_findings:
        return None
    top = code_findings[0]
    evidence: dict[str, Any] = {}
    for f in code_findings[:5]:
        evidence[str(f.get("title") or f"finding_{f.get('rank')}")] = f.get("evidence") or {}
    suspected: list[str] = []
    for f in code_findings[:5]:
        suspected.extend(_suspected_files_for(str(f.get("title") or ""), str(f.get("detail") or "")))
    suspected_u: list[str] = []
    seen: set[str] = set()
    for x in suspected:
        if x not in seen:
            seen.add(x)
            suspected_u.append(x)
    lines = [
        f"- [{f.get('class')}] {f.get('title')}: {f.get('detail')}" for f in code_findings[:8]
    ]
    blurb = (
        "Cursor handoff (GUI desk supervisor queue):\n"
        f"Score={score}. Read `src/data/v31-production/state/gui_supervisor_latest.json`.\n"
        "Preserve A2 CFD pause on :8080. Phase-2 heals are allowlisted only "
        "(port hung soft-recycle, loops unpause/recycle if flat, UI restart, silence soft-pause SB).\n"
        "Do not kill -9 / raise REST / re-enable Instant-micro / loosen ElasticGate.\n"
        "Code-class findings:\n" + "\n".join(lines)
    )
    return {
        "score": score,
        "symptom": str(top.get("title") or "code finding"),
        "detail": str(top.get("detail") or ""),
        "top_finding": {
            "rank": top.get("rank"),
            "severity": top.get("severity"),
            "class": top.get("class"),
            "title": top.get("title"),
            "detail": top.get("detail"),
            "needs_code": True,
        },
        "evidence": evidence,
        "suspected_files": suspected_u[:12],
        "allowed_actions": [
            "Read gui_supervisor_latest.json + .md twin",
            "Inspect suspected_files and add/fix tests under tests/",
            "Propose isolated code patches (no strategy/risk math without operator ask)",
            "Rebuild Quantum Terminal :3000 after UI changes",
            "Preserve A2 / ranked / Path A carve posture in checks and fixes",
            "Run allowlisted Phase-2 heals via gui_desk_supervisor --heal/--heal-dry-run",
        ],
        "forbidden_actions": [
            "kill -9 / SIGKILL / isolated kill of main.py",
            "Raise REST 3/min hard cap",
            "Re-enable Instant/micro or loosen ElasticGate/OBI fail-open",
            "Strategy/alpha rewrites or allow_non_dow global unlock",
            "POST /api/start on :8080 while A2 marker active (lift A2)",
            "Edit SQLite history / learning DB schema",
            "Heal beyond allowlist or after 2/hour cap",
        ],
        "state_path": "src/data/v31-production/state/gui_supervisor_latest.md".replace(
            ".md", ".json"
        ),
        "md_path": "src/data/v31-production/reports/gui_supervisor_latest.md",
        "preserve_a2": True,
        "blurb": blurb,
    }


def assess(*, ports: dict[str, int] | None = None, data_root: Path | None = None) -> dict[str, Any]:
    """Run one observation cycle. Pure observe — never mutates agent state."""
    ports = ports or dict(DEFAULT_PORTS)
    root = data_root or _data_root()
    host = "127.0.0.1"
    cfd_port = int(ports["cfd"])
    sb_port = int(ports["sb"])
    ui_port = int(ports["ui"])

    cfd_listen = _tcp_reachable(host, cfd_port)
    sb_listen = _tcp_reachable(host, sb_port)
    cfd = _port_bundle(f"http://{host}:{cfd_port}", listen=cfd_listen)
    sb = _port_bundle(f"http://{host}:{sb_port}", listen=sb_listen)
    ui_up = _tcp_reachable(host, ui_port)
    ui_http = False
    if ui_up:
        try:
            req = urllib.request.Request(
                f"http://{host}:{ui_port}/",
                headers={"User-Agent": "gui-desk-supervisor/1"},
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
                ui_http = 200 <= int(resp.status) < 500
        except Exception:
            ui_http = False

    a2 = _read_a2_marker(root)
    a2_active = bool(isinstance(a2, dict) and a2.get("active") is True)
    cfd_paused = bool((cfd.get("health") or {}).get("trading_paused")) if cfd.get("health") else None
    sb_paused = bool((sb.get("health") or {}).get("trading_paused")) if sb.get("health") else None

    cash = _cash_merge_sanity(
        cfd.get("accounting") if isinstance(cfd.get("accounting"), dict) else None,
        sb.get("accounting") if isinstance(sb.get("accounting"), dict) else None,
    )
    blotter_cfd = _blotter_quality(cfd.get("accounting") if isinstance(cfd.get("accounting"), dict) else None)
    blotter_sb = _blotter_quality(sb.get("accounting") if isinstance(sb.get("accounting"), dict) else None)
    rest_cfd = _rest_pressure(cfd)
    rest_sb = _rest_pressure(sb)

    findings: list[dict[str, Any]] = []
    area_grades: dict[str, str] = {}

    # --- Reachability / open risk / hung LISTEN ---
    if cfd.get("hung_api"):
        findings.append(
            _finding(
                rank=1,
                severity="fail",
                klass="ops",
                title=f":{cfd_port} CFD hung API (LISTEN but timeout)",
                detail=cfd.get("health_err") or "TCP up, HTTP health timed out",
                needs_ops=True,
                evidence={"err": cfd.get("health_err"), "listen": True, "heal": "port_hung_soft_recycle"},
            )
        )
        area_grades["cfd_agent"] = "FAIL"
    elif not cfd.get("reachable"):
        findings.append(
            _finding(
                rank=1,
                severity="fail",
                klass="ops",
                title=f":{cfd_port} CFD agent unreachable",
                detail=cfd.get("health_err") or "no health/positions response",
                needs_ops=True,
                evidence={"err": cfd.get("health_err"), "listen": cfd.get("listen")},
            )
        )
        area_grades["cfd_agent"] = "FAIL"
    else:
        area_grades["cfd_agent"] = "PASS"

    if sb.get("hung_api"):
        findings.append(
            _finding(
                rank=1,
                severity="fail",
                klass="ops",
                title=f":{sb_port} SB hung API (LISTEN but timeout)",
                detail=sb.get("health_err") or "TCP up, HTTP health timed out",
                needs_ops=True,
                evidence={"err": sb.get("health_err"), "listen": True, "heal": "port_hung_soft_recycle"},
            )
        )
        area_grades["sb_agent"] = "FAIL"
    elif not sb.get("reachable"):
        findings.append(
            _finding(
                rank=1,
                severity="fail",
                klass="ops",
                title=f":{sb_port} SB agent unreachable",
                detail=sb.get("health_err") or "no health/positions response",
                needs_ops=True,
                evidence={"err": sb.get("health_err"), "listen": sb.get("listen")},
            )
        )
        area_grades["sb_agent"] = "FAIL"
    else:
        area_grades["sb_agent"] = "PASS"

    def _open_grade(label: str, bundle: dict[str, Any]) -> str:
        pos = bundle.get("positions") if isinstance(bundle.get("positions"), dict) else {}
        ts = bundle.get("trade_support") if isinstance(bundle.get("trade_support"), dict) else {}
        verdict = str(pos.get("verdict") or "")
        critical = bool(pos.get("critical"))
        count = int(pos.get("count") or 0)
        broker_open = int(ts.get("broker_open") or 0)
        if critical or verdict == "CRITICAL":
            findings.append(
                _finding(
                    rank=1,
                    severity="fail",
                    klass="ops",
                    title=f"{label} positions CRITICAL",
                    detail=f"verdict={verdict} count={count} alarms={pos.get('critical_alarms')}",
                    needs_ops=True,
                    evidence={"verdict": verdict, "count": count},
                )
            )
            return "FAIL"
        if verdict in ("DEGRADED",) or int(pos.get("unmonitored") or 0) > 0:
            findings.append(
                _finding(
                    rank=2,
                    severity="watch",
                    klass="ops",
                    title=f"{label} positions degraded / unmonitored",
                    detail=f"verdict={verdict} unmonitored={pos.get('unmonitored')}",
                    needs_ops=True,
                    evidence={"verdict": verdict},
                )
            )
            return "WATCH"
        if verdict in ("FLAT", "HEALTHY") and broker_open == 0:
            return "PASS"
        if not pos and bundle.get("positions_err"):
            findings.append(
                _finding(
                    rank=2,
                    severity="watch",
                    klass="ops",
                    title=f"{label} positions/live missing",
                    detail=str(bundle.get("positions_err")),
                    needs_ops=True,
                )
            )
            return "WATCH"
        return "PASS" if count == 0 else "WATCH"

    area_grades["cfd_open_risk"] = _open_grade(f":{cfd_port}", cfd)
    area_grades["sb_open_risk"] = _open_grade(f":{sb_port}", sb)

    # Trade support
    for label, bundle, key in (
        (f":{cfd_port}", cfd, "cfd_trade_support"),
        (f":{sb_port}", sb, "sb_trade_support"),
    ):
        ts = bundle.get("trade_support") if isinstance(bundle.get("trade_support"), dict) else None
        if ts is None:
            area_grades[key] = "WATCH"
            findings.append(
                _finding(
                    rank=3,
                    severity="watch",
                    klass="ops",
                    title=f"{label} trade_support missing",
                    detail=str(bundle.get("trade_support_err") or "no payload"),
                    needs_ops=True,
                )
            )
        elif ts.get("running") or ts.get("ok"):
            area_grades[key] = "PASS"
        else:
            area_grades[key] = "WATCH"
            findings.append(
                _finding(
                    rank=2,
                    severity="watch",
                    klass="ops",
                    title=f"{label} trade_support not running",
                    detail=f"broker_open={ts.get('broker_open')} ok={ts.get('ok')}",
                    needs_ops=True,
                    evidence={"trade_support": {"running": ts.get("running"), "ok": ts.get("ok")}},
                )
            )

    # A2 CFD pause — preserve
    if a2_active:
        if cfd_paused is True:
            area_grades["a2_cfd_pause"] = "PASS"
            findings.append(
                _finding(
                    rank=99,
                    severity="info",
                    klass="ignore",
                    title="A2 CFD pause ON (preserve)",
                    detail=f"marker mode={a2.get('mode')} :{cfd_port} trading_paused=true; :{sb_port} paused={sb_paused}",
                    evidence={"a2": {"active": True, "mode": a2.get("mode"), "cfd_paused": cfd_paused, "sb_paused": sb_paused}},
                )
            )
        else:
            area_grades["a2_cfd_pause"] = "FAIL"
            findings.append(
                _finding(
                    rank=1,
                    severity="fail",
                    klass="ops",
                    title="A2 marker active but :8080 not paused",
                    detail="Do NOT POST /api/start on CFD unless operator lifts A2. Re-engage pause on :8080.",
                    needs_ops=True,
                    evidence={"a2_active": True, "cfd_paused": cfd_paused},
                )
            )
    else:
        area_grades["a2_cfd_pause"] = "PASS" if cfd_paused is not True else "WATCH"
        if cfd_paused is True and not a2_active:
            findings.append(
                _finding(
                    rank=4,
                    severity="watch",
                    klass="ops",
                    title="CFD trading_paused without A2 marker file",
                    detail="Process pause set; state_cfd/a2_entries_paused.json inactive/missing.",
                    needs_ops=True,
                )
            )

    # Cash merge
    if cash.get("double_count_risk"):
        area_grades["cash_merge"] = "FAIL"
        findings.append(
            _finding(
                rank=1,
                severity="fail",
                klass="ui",
                title="Cash merge double-count risk",
                detail=f"mode={cash.get('mode')} cfd={cash.get('cfd_today_gbp')} sb={cash.get('sb_today_gbp')}",
                needs_code=True,
                evidence=cash,
            )
        )
    elif cash.get("mode") == "shared_journal_once":
        area_grades["cash_merge"] = "PASS"
        findings.append(
            _finding(
                rank=90,
                severity="info",
                klass="ignore",
                title="Cash single-count (shared journal)",
                detail=f"today≈£{cash.get('merged_today_gbp')} both ports near-equal (not ×2)",
                evidence=cash,
            )
        )
    elif cash.get("mode") == "dual_independent":
        area_grades["cash_merge"] = "WATCH"
        findings.append(
            _finding(
                rank=5,
                severity="watch",
                klass="ui",
                title="Dual-port cash totals diverge",
                detail=(
                    f"cfd=£{cash.get('cfd_today_gbp')} sb=£{cash.get('sb_today_gbp')} "
                    f"delta=£{cash.get('delta_gbp')} — merge should SUM (not treat as shared clone)"
                ),
                evidence=cash,
            )
        )
    elif cash.get("mode") == "missing":
        area_grades["cash_merge"] = "WATCH"
        findings.append(
            _finding(
                rank=5,
                severity="watch",
                klass="ops",
                title="simplified_accounting unavailable",
                detail="both ports missing today net",
                needs_ops=True,
            )
        )
    else:
        area_grades["cash_merge"] = "PASS"

    # Blotter field quality (agent payload; Terminal may enrich live)
    thin = blotter_cfd.get("thin_agent_payload") or blotter_sb.get("thin_agent_payload")
    smell = int(blotter_cfd.get("shared_journal_smell") or 0) + int(blotter_sb.get("shared_journal_smell") or 0)
    if smell > 0:
        area_grades["blotter"] = "WATCH"
        findings.append(
            _finding(
                rank=3,
                severity="watch",
                klass="ui",
                title="Blotter SHARED/JOURNAL smell on agent rows",
                detail=f"smell_rows={smell}; Terminal enrich should override — verify :3000 blotter",
                needs_code=True,
                evidence={"cfd": blotter_cfd, "sb": blotter_sb},
            )
        )
    elif thin:
        area_grades["blotter"] = "WATCH"
        findings.append(
            _finding(
                rank=3,
                severity="watch",
                klass="code",
                title="Agent blotter rows lack AccountID/Product",
                detail=(
                    "simplified_accounting last_10 missing account_id/product_type "
                    "(code may be on disk awaiting agent reload). UI journal enrich is Phase-1 OK."
                ),
                needs_code=True,
                evidence={"cfd": blotter_cfd, "sb": blotter_sb},
            )
        )
    else:
        area_grades["blotter"] = "PASS"

    # REST
    def _rest_grade(label: str, rest: dict[str, Any]) -> str:
        level = str(rest.get("level") or "UNKNOWN").upper()
        if level == "CRITICAL":
            findings.append(
                _finding(
                    rank=2,
                    severity="fail",
                    klass="code",
                    title=f"{label} REST CRITICAL",
                    detail=f"calls/min={rest.get('calls_last_minute')} reasons={rest.get('stability_reasons')}",
                    needs_code=True,
                    needs_ops=True,
                    evidence=rest,
                )
            )
            return "FAIL"
        if level in ("ELEVATED", "HIGH"):
            findings.append(
                _finding(
                    rank=4,
                    severity="watch",
                    klass="code",
                    title=f"{label} REST {level}",
                    detail=(
                        f"calls/min={rest.get('calls_last_minute')}. "
                        "Do not raise 3/min hard cap. Prefer evening reload if MICRO-confirm thrash."
                    ),
                    needs_code=True,
                    evidence=rest,
                )
            )
            return "WATCH"
        if level in ("OK", "IDLE", "UNKNOWN"):
            return "PASS" if level != "UNKNOWN" else "WATCH"
        return "WATCH"

    area_grades["cfd_rest"] = _rest_grade(f":{cfd_port}", rest_cfd)
    area_grades["sb_rest"] = _rest_grade(f":{sb_port}", rest_sb)

    # UI :3000
    if ui_http or ui_up:
        area_grades["quantum_ui"] = "PASS"
    else:
        area_grades["quantum_ui"] = "FAIL"
        findings.append(
            _finding(
                rank=1,
                severity="fail",
                klass="ui",
                title=f":{ui_port} Quantum Terminal unreachable",
                detail="UI LaunchAgent may be down — install_trading_desk_always_on.sh --status",
                needs_ops=True,
            )
        )

    # Health ok:false under A2 / watchdog — expected watch, not fail
    for label, bundle in ((f":{cfd_port}", cfd), (f":{sb_port}", sb)):
        h = bundle.get("health") if isinstance(bundle.get("health"), dict) else None
        if not h:
            continue
        issues = h.get("issues") or []
        if h.get("ok") is False:
            soft = {"trading_paused", "watchdog_inactive", "agent_running_without_watchdog"}
            issue_set = {str(x) for x in issues} if isinstance(issues, list) else set()
            if issue_set and issue_set <= soft:
                findings.append(
                    _finding(
                        rank=80,
                        severity="info",
                        klass="ignore",
                        title=f"{label} health ok=false (expected soft)",
                        detail=f"issues={issues}; path may still be live (iron_cage/trade_ready)",
                        evidence={"issues": issues, "trade_ready": h.get("trade_ready")},
                    )
                )
            elif "watchdog_inactive" in issue_set or any("watchdog" in str(x) for x in issue_set):
                findings.append(
                    _finding(
                        rank=6,
                        severity="watch",
                        klass="ops",
                        title=f"{label} watchdog inactive",
                        detail="Safe for attended session; evening ops to re-arm launchd — do not mid-session without ask",
                        needs_ops=True,
                        evidence={"issues": issues},
                    )
                )
                area_grades.setdefault("watchdog", "WATCH")

    # Affinity cosmetic
    ops = cfd.get("ops_strip") if isinstance(cfd.get("ops_strip"), dict) else {}
    if ops and ops.get("core_affinity") is None and ops.get("hardware_affinity") is None:
        findings.append(
            _finding(
                rank=95,
                severity="info",
                klass="ignore",
                title="Hardware affinity meters empty",
                detail="ops_strip lacks affinity fields — cosmetic only",
            )
        )

    # --- STUCK-class: TradingLoops / paused_at_boot / Path A carve / ranked / warm ---
    dual_cfg = _read_dual_core_posture(_repo_root())
    cfd_loops = _loops_plane(cfd.get("health") if isinstance(cfd.get("health"), dict) else None)
    sb_loops = _loops_plane(sb.get("health") if isinstance(sb.get("health"), dict) else None)
    cfd_posture = _routing_posture(cfd.get("state") if isinstance(cfd.get("state"), dict) else None)
    sb_posture = _routing_posture(sb.get("state") if isinstance(sb.get("state"), dict) else None)
    sb_ranked = _ranked_from_rotation(sb.get("rotation") if isinstance(sb.get("rotation"), dict) else None)
    cfd_ranked = _ranked_from_rotation(cfd.get("rotation") if isinstance(cfd.get("rotation"), dict) else None)
    # Prefer SB ranked when A2 pauses CFD (desk intent posture).
    ranked = sb_ranked if (sb_ranked.get("active") or a2_active) else cfd_ranked

    log_smells = {
        "cfd": _cheap_log_tick_smell(root / "logs" / "v32_cfd.log"),
        "sb": _cheap_log_tick_smell(root / "logs" / "v32_sb.log"),
    }

    def _stuck_loops_check(label: str, loops: dict[str, Any], paused: bool | None, log_key: str) -> str:
        accepting = loops.get("accepting_ticks")
        running = loops.get("running")
        built = loops.get("built")
        boot_ready = loops.get("boot_ready")
        boot_err = loops.get("boot_error")
        trade_ready = loops.get("trade_ready")
        smell = log_smells.get(log_key) or {}
        evidence = {"loops": loops, "log_tail": {k: smell.get(k) for k in ("dormant_hits", "entering_tick_hits", "bytes_read", "ok", "path")}}

        if boot_err:
            findings.append(
                _finding(
                    rank=1,
                    severity="fail",
                    klass="code",
                    title=f"{label} boot error (loops plane)",
                    detail=str(boot_err),
                    needs_code=True,
                    evidence=evidence,
                )
            )
            return "FAIL"

        # A2 / intentional pause: dormant loops are expected — do not STUCK-fail CFD.
        if paused is True:
            if accepting is False:
                findings.append(
                    _finding(
                        rank=85,
                        severity="info",
                        klass="ignore",
                        title=f"{label} loops not accepting ticks (paused — preserve)",
                        detail="trading_paused=true; accepting_ticks=false is consistent with A2/order valve",
                        evidence=evidence,
                    )
                )
            return "PASS"

        if accepting is False and (running is True or trade_ready is True or boot_ready is True):
            findings.append(
                _finding(
                    rank=1,
                    severity="fail",
                    klass="code",
                    title=f"{label} TradingLoops not accepting ticks (STUCK)",
                    detail=(
                        f"accepting_ticks=false running={running} built={built} "
                        f"boot_ready={boot_ready} trade_ready={trade_ready}. "
                        "Suspect paused_at_boot / missing entering tick loop after Gate5."
                    ),
                    needs_code=True,
                    evidence=evidence,
                )
            )
            return "FAIL"

        # Log smell only when API already looks unhealthy or zero entering with dormant hits
        if (
            smell.get("ok")
            and int(smell.get("dormant_hits") or 0) > 0
            and int(smell.get("entering_tick_hits") or 0) == 0
            and accepting is not True
        ):
            findings.append(
                _finding(
                    rank=1,
                    severity="fail",
                    klass="code",
                    title=f"{label} paused_at_boot smell (no entering tick loop)",
                    detail=(
                        f"log tail dormant_hits={smell.get('dormant_hits')} "
                        f"entering_tick_hits=0 (last {smell.get('bytes_read')}B)"
                    ),
                    needs_code=True,
                    evidence=evidence,
                )
            )
            return "FAIL"

        if accepting is True:
            return "PASS"
        if accepting is None:
            return "WATCH"
        return "PASS"

    area_grades["cfd_loops"] = _stuck_loops_check(f":{cfd_port}", cfd_loops, cfd_paused, "cfd")
    area_grades["sb_loops"] = _stuck_loops_check(f":{sb_port}", sb_loops, sb_paused, "sb")

    # SB armed + ml_confidence stuck 0 — distinguish expected warm vs warm failure
    if sb.get("reachable") and sb_paused is not True:
        ml_c = sb_posture.get("ml_confidence")
        sig = sb_posture.get("signal_strength")
        try:
            ml_f = float(ml_c) if ml_c is not None else None
        except (TypeError, ValueError):
            ml_f = None
        try:
            sig_f = float(sig) if sig is not None else None
        except (TypeError, ValueError):
            sig_f = None
        boot_ready = sb_loops.get("boot_ready")
        boot_pct = sb_loops.get("boot_percent")
        try:
            boot_pct_f = float(boot_pct) if boot_pct is not None else None
        except (TypeError, ValueError):
            boot_pct_f = None
        cold = (ml_f is not None and ml_f <= 0.0) and (sig_f is None or sig_f <= 0.0)
        if cold and boot_ready is True:
            area_grades["sb_signal_warm"] = "WATCH"
            findings.append(
                _finding(
                    rank=2,
                    severity="watch",
                    klass="code",
                    title=f":{sb_port} ml_confidence stuck 0 after boot ready (warm failure)",
                    detail=(
                        f"boot_ready=true but ml_confidence={ml_c} signal_strength={sig}. "
                        "SB armed — Path A cannot score until signal plane warms."
                    ),
                    needs_code=True,
                    evidence={
                        "ml_confidence": ml_c,
                        "signal_strength": sig,
                        "loops": sb_loops,
                        "posture": sb_posture,
                    },
                )
            )
        elif cold and boot_ready is False:
            area_grades["sb_signal_warm"] = "PASS"
            findings.append(
                _finding(
                    rank=88,
                    severity="info",
                    klass="ignore",
                    title=f":{sb_port} ml_confidence=0 while vector warm (expected)",
                    detail=(
                        f"boot_ready=false percent={boot_pct_f} label={sb_loops.get('boot_label')}; "
                        "not a Path A veto yet"
                    ),
                    evidence={"ml_confidence": ml_c, "boot_percent": boot_pct_f},
                )
            )
        else:
            area_grades["sb_signal_warm"] = "PASS"
    else:
        area_grades["sb_signal_warm"] = "PASS"

    # Path A vs MICRO hard-block posture (preserve A2 CFD + SB carve expectation)
    if dual_cfg.get("sb_path_a_carve_expected") and sb.get("reachable") and sb_paused is not True:
        allow = set(sb_posture.get("hard_allow") or [])
        block = set(sb_posture.get("hard_block") or [])
        if allow or block:
            carve_ok = "PATH_A" in allow and "MICRO" in block and "PATH_A" not in block
            if not carve_ok:
                area_grades["sb_path_a_carve"] = "FAIL"
                findings.append(
                    _finding(
                        rank=1,
                        severity="fail",
                        klass="code",
                        title=f":{sb_port} Path A vs MICRO hard-block mismatch (SB carve)",
                        detail=(
                            "expected PATH_A in hard_allow and MICRO in hard_block; "
                            f"got allow={sorted(allow)} block={sorted(block)} "
                            f"ownership={sb_posture.get('controller_ownership')}. "
                            "Preserve sb_macro_ltr_entries_only / SB_MACRO_PATH_A_CARVE."
                        ),
                        needs_code=True,
                        evidence={"posture": sb_posture, "dual_cfg": dual_cfg},
                    )
                )
            else:
                area_grades["sb_path_a_carve"] = "PASS"
                findings.append(
                    _finding(
                        rank=92,
                        severity="info",
                        klass="ignore",
                        title=f":{sb_port} SB Path A carve posture OK",
                        detail=f"hard_allow={sorted(allow)} hard_block={sorted(block)}",
                        evidence={"posture": sb_posture},
                    )
                )
        else:
            area_grades["sb_path_a_carve"] = "WATCH"
            findings.append(
                _finding(
                    rank=3,
                    severity="watch",
                    klass="code",
                    title=f":{sb_port} Path A carve posture unavailable",
                    detail="/api/state routing missing DOW hard_allow/hard_block",
                    needs_code=True,
                    evidence={"state_err": sb.get("state_err"), "posture": sb_posture},
                )
            )
    else:
        area_grades["sb_path_a_carve"] = "PASS"

    # CFD: when not A2-paused, MICRO-allow / PATH_A-block is expected under SCALP
    if cfd.get("reachable") and cfd_paused is not True and (cfd_posture.get("hard_allow") or cfd_posture.get("hard_block")):
        allow_c = set(cfd_posture.get("hard_allow") or [])
        block_c = set(cfd_posture.get("hard_block") or [])
        # Mismatch only if carve leaked onto CFD (PATH_A allowed + MICRO blocked while SCALP)
        if "PATH_A" in allow_c and "MICRO" in block_c and dual_cfg.get("sb_path_a_carve_expected"):
            area_grades["cfd_path_posture"] = "WATCH"
            findings.append(
                _finding(
                    rank=2,
                    severity="watch",
                    klass="code",
                    title=f":{cfd_port} CFD shows SB-like Path A carve (unexpected)",
                    detail=f"allow={sorted(allow_c)} block={sorted(block_c)} — CFD SCALP should prefer MICRO",
                    needs_code=True,
                    evidence={"posture": cfd_posture},
                )
            )
        else:
            area_grades["cfd_path_posture"] = "PASS"
    else:
        area_grades["cfd_path_posture"] = "PASS"

    # Ranked promote vs exclude / candidate allowlist conflict
    promoted = list(ranked.get("promoted") or [])
    excluded = set(dual_cfg.get("exclude_from_hot_path") or [])
    candidates = set(dual_cfg.get("ranked_candidate_epics") or [])
    if ranked.get("active") and promoted:
        bad_excl = [e for e in promoted if e in excluded]
        bad_cand = [e for e in promoted if candidates and e not in candidates]
        if bad_excl:
            area_grades["ranked_allowlist"] = "FAIL"
            findings.append(
                _finding(
                    rank=1,
                    severity="fail",
                    klass="code",
                    title="Ranked promote vs exclude_from_hot_path conflict",
                    detail=f"promoted∩exclude={bad_excl}; promoted={promoted}",
                    needs_code=True,
                    evidence={
                        "ranked": ranked,
                        "exclude_from_hot_path": sorted(excluded),
                        "dual_cfg": dual_cfg,
                    },
                )
            )
        elif bad_cand:
            area_grades["ranked_allowlist"] = "WATCH"
            findings.append(
                _finding(
                    rank=2,
                    severity="watch",
                    klass="code",
                    title="Ranked promote outside ranked_candidate_epics",
                    detail=f"promoted not in candidates: {bad_cand}; candidates={sorted(candidates)}",
                    needs_code=True,
                    evidence={"ranked": ranked, "candidates": sorted(candidates)},
                )
            )
        else:
            area_grades["ranked_allowlist"] = "PASS"
            # Static sb_hot_path_allowlist may still be DOW-only — expected under ranked
            # (effective allowlist becomes promoted). Note only if base lacks a promote.
            base_allow = set(dual_cfg.get("sb_hot_path_allowlist") or [])
            if base_allow and any(e not in base_allow for e in promoted):
                findings.append(
                    _finding(
                        rank=93,
                        severity="info",
                        klass="ignore",
                        title="Ranked promote supersedes static sb_hot_path_allowlist",
                        detail=(
                            f"static_allow={sorted(base_allow)} promoted={promoted} — "
                            "effective_sb_allowlist uses ranked top-N (by design)"
                        ),
                        evidence={"base_allow": sorted(base_allow), "promoted": promoted},
                    )
                )
    else:
        area_grades["ranked_allowlist"] = "PASS" if dual_cfg.get("ranked_rotator_mode") is not True else "WATCH"
        if dual_cfg.get("ranked_rotator_mode") is True and sb_paused is not True and not ranked.get("active"):
            findings.append(
                _finding(
                    rank=4,
                    severity="watch",
                    klass="code",
                    title="ranked_rotator_mode config ON but rotation_state inactive",
                    detail="Config expects ranked; /api/rotation_state ranked_rotator.active=false on SB",
                    needs_code=True,
                    evidence={"ranked": ranked, "dual_cfg": dual_cfg},
                )
            )

    # Velocity stack may include excluded epics (Nikkei) — not a promote conflict
    stack = list(ranked.get("active_stack") or [])
    stack_excl = [e for e in stack if e in excluded]
    if stack_excl and ranked.get("active"):
        findings.append(
            _finding(
                rank=94,
                severity="info",
                klass="ignore",
                title="Velocity active_stack includes excluded epic (≠ ranked promote)",
                detail=f"stack∩exclude={stack_excl}; ranked promoted={promoted} — OK if promote path gates entries",
                evidence={"active_stack": stack, "promoted": promoted},
            )
        )

    # Zero-attempt / ARMED silence timer (SB)
    funnel = _read_gate_funnel(root)
    sb_broker = 0
    try:
        sb_broker = int((sb.get("trade_support") or {}).get("broker_open") or 0)
    except (TypeError, ValueError):
        sb_broker = 0
    sb_accepting = sb_loops.get("accepting_ticks") is True
    sb_armed = bool(
        sb.get("reachable")
        and sb_paused is not True
        and sb_accepting
        and (sb_loops.get("trade_ready") is True or sb_loops.get("boot_ready") is True or sb_loops.get("running") is True)
    )
    funnel_passed = 0
    try:
        funnel_passed = int(funnel.get("all_passed_ticks") or 0)
    except (TypeError, ValueError):
        funnel_passed = 0
    activity = sb_broker > 0 or funnel_passed > 0
    silence = _update_silence_tracker(
        data_root=root,
        sb_armed=sb_armed,
        activity=activity,
    )
    silence_sec = float(silence.get("silence_sec") or 0.0)
    silence_threshold_sec = float(SILENCE_MINUTES) * 60.0
    # Warming vector plane: do not silence-fail while boot_ready false / ml cold expected
    warming = sb_loops.get("boot_ready") is False and (
        sb_posture.get("ml_confidence") in (0, 0.0, None) or sb_loops.get("boot_percent") not in (None, 100, 100.0)
    )
    if sb_armed and not warming and silence_sec >= silence_threshold_sec and not activity:
        area_grades["sb_armed_silence"] = "FAIL"
        findings.append(
            _finding(
                rank=1,
                severity="fail",
                klass="code",
                title=f":{sb_port} ARMED but silent (zero-attempt silence timer)",
                detail=(
                    f"armed ~{silence.get('silence_minutes')}m without entry activity "
                    f"(threshold={SILENCE_MINUTES}m). "
                    "Phase-2 may soft-pause SB entries (halt bleed) — will NOT loosen gates."
                ),
                needs_code=True,
                needs_ops=True,
                evidence={
                    "silence": silence,
                    "funnel_all_passed_ticks": funnel_passed,
                    "broker_open": sb_broker,
                    "heal": "armed_silence_soft_pause_sb",
                },
            )
        )
    elif sb_armed and warming:
        area_grades["sb_armed_silence"] = "PASS"
        findings.append(
            _finding(
                rank=89,
                severity="info",
                klass="ignore",
                title=f":{sb_port} silence timer held (signal/vector warm)",
                detail=(
                    f"armed_since tracked; silence_sec={int(silence_sec)} but boot_ready="
                    f"{sb_loops.get('boot_ready')} — not a silence FAIL yet"
                ),
                evidence={"silence": silence, "loops": sb_loops},
            )
        )
    else:
        area_grades["sb_armed_silence"] = "PASS"

    # Rank findings: fail first, then watch, then info; stable by existing rank
    sev_order = {"fail": 0, "watch": 1, "info": 2}
    findings.sort(key=lambda f: (sev_order.get(str(f.get("severity")), 9), int(f.get("rank") or 99)))
    for i, f in enumerate(findings, start=1):
        f["rank"] = i

    fail_n = sum(1 for f in findings if f.get("severity") == "fail")
    watch_n = sum(1 for f in findings if f.get("severity") == "watch")
    if fail_n:
        score = "FAIL"
    elif watch_n:
        score = "WATCH"
    else:
        score = "PASS"

    # Area-grade override: any FAIL area forces FAIL
    if any(g == "FAIL" for g in area_grades.values()):
        score = "FAIL"
    elif score == "PASS" and any(g == "WATCH" for g in area_grades.values()):
        score = "WATCH"

    needs_code = any(bool(f.get("needs_code")) for f in findings if f.get("severity") in ("fail", "watch"))
    needs_ops = any(bool(f.get("needs_ops")) for f in findings if f.get("severity") in ("fail", "watch"))

    handoff = _build_cursor_handoff(score=score, findings=findings) if needs_code else None

    top_actionable = next(
        (f for f in findings if f.get("severity") in ("fail", "watch")),
        None,
    )
    chip = {
        "visible": score in ("WATCH", "FAIL"),
        "score": score,
        "label": f"SUPERVISOR {score}" if score in ("WATCH", "FAIL") else "SUPERVISOR PASS",
        "summary": (
            str(top_actionable.get("title") or "")
            if top_actionable
            else ("all clear" if score == "PASS" else score)
        ),
        "tone": "red" if score == "FAIL" else ("amber" if score == "WATCH" else "green"),
        "state_path": "src/data/v31-production/state/gui_supervisor_latest.json",
        "needs_code": needs_code,
        "needs_ops": needs_ops,
    }

    cfd_pid = (cfd.get("health") or {}).get("agent_pid") if isinstance(cfd.get("health"), dict) else None
    sb_pid = (sb.get("health") or {}).get("agent_pid") if isinstance(sb.get("health"), dict) else None

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "phase": PHASE,
        "ts": time.time(),
        "checked_at": _now_iso(),
        "score": score,
        "needs_code": needs_code,
        "needs_ops": needs_ops,
        "cursor_handoff": handoff,
        "dashboard_chip": chip,
        "top_finding": (
            {
                "rank": top_actionable.get("rank"),
                "severity": top_actionable.get("severity"),
                "class": top_actionable.get("class"),
                "title": top_actionable.get("title"),
                "detail": top_actionable.get("detail"),
                "needs_code": top_actionable.get("needs_code"),
                "needs_ops": top_actionable.get("needs_ops"),
            }
            if top_actionable
            else None
        ),
        "area_grades": area_grades,
        "findings": findings,
        "stuck_plane": {
            "cfd_loops": cfd_loops,
            "sb_loops": sb_loops,
            "cfd_path_posture": cfd_posture,
            "sb_path_posture": sb_posture,
            "ranked": ranked,
            "dual_core_cfg": {
                k: dual_cfg.get(k)
                for k in (
                    "exclude_from_hot_path",
                    "ranked_candidate_epics",
                    "sb_hot_path_allowlist",
                    "ranked_rotator_mode",
                    "sb_path_a_carve_expected",
                    "sb_macro_ltr_entries_only",
                )
            },
            "log_smells": {
                k: {kk: (log_smells.get(k) or {}).get(kk) for kk in ("ok", "dormant_hits", "entering_tick_hits", "bytes_read")}
                for k in ("cfd", "sb")
            },
        },
        "a2": {
            "marker_active": a2_active,
            "marker": {k: a2.get(k) for k in ("active", "mode", "date", "reason", "scope") if isinstance(a2, dict) and k in a2}
            if isinstance(a2, dict)
            else a2,
            "cfd_trading_paused": cfd_paused,
            "sb_trading_paused": sb_paused,
            "preserve": True,
        },
        "ports": {
            "cfd": {
                "port": cfd_port,
                "reachable": cfd.get("reachable"),
                "listen": cfd.get("listen"),
                "hung_api": cfd.get("hung_api"),
                "agent_pid": cfd_pid,
                "trading_paused": cfd_paused,
                "positions_verdict": (cfd.get("positions") or {}).get("verdict") if isinstance(cfd.get("positions"), dict) else None,
                "broker_open": (cfd.get("trade_support") or {}).get("broker_open") if isinstance(cfd.get("trade_support"), dict) else None,
                "rest": rest_cfd,
                "blotter": blotter_cfd,
                "liveness_ok": (cfd.get("liveness") or {}).get("ok") if isinstance(cfd.get("liveness"), dict) else None,
            },
            "sb": {
                "port": sb_port,
                "reachable": sb.get("reachable"),
                "listen": sb.get("listen"),
                "hung_api": sb.get("hung_api"),
                "agent_pid": sb_pid,
                "trading_paused": sb_paused,
                "positions_verdict": (sb.get("positions") or {}).get("verdict") if isinstance(sb.get("positions"), dict) else None,
                "broker_open": (sb.get("trade_support") or {}).get("broker_open") if isinstance(sb.get("trade_support"), dict) else None,
                "rest": rest_sb,
                "blotter": blotter_sb,
                "liveness_ok": (sb.get("liveness") or {}).get("ok") if isinstance(sb.get("liveness"), dict) else None,
            },
            "ui": {"port": ui_port, "tcp": ui_up, "http": ui_http},
        },
        "silence": silence,
        "cash_merge": cash,
        "policy": {
            "auto_heal": AUTO_HEAL_DEFAULT,
            "may_restart_agents": True,  # allowlisted soft recycle only
            "may_kill9": False,
            "may_edit_strategy": False,
            "heal_allowlist": [
                "port_hung_soft_recycle",
                "loops_not_arming_unpause_or_recycle",
                "ui_restart_only",
                "armed_silence_soft_pause_sb",
                "reapply_a2_cfd_pause",
            ],
            "heal_cap_per_hour": int(os.environ.get("IG_GUI_SUP_HEAL_CAP_PER_HOUR", "2")),
            "silence_minutes": SILENCE_MINUTES,
            "writes": [
                "gui_supervisor_latest.json",
                "gui_supervisor_latest.md",
                "gui_supervisor_history.jsonl",
                "gui_supervisor_silence.json",
                "gui_supervisor_heal_log.jsonl",
                "gui_supervisor_heal_budget.json",
            ],
        },
    }
    return payload


def _markdown_report(payload: dict[str, Any]) -> str:
    lines = [
        f"# GUI Desk Supervisor — Phase {payload.get('phase')}",
        "",
        f"**Score: {payload.get('score')}**  ",
        f"Checked: `{payload.get('checked_at')}`  ",
        f"needs_code={payload.get('needs_code')} · needs_ops={payload.get('needs_ops')}",
        "",
        "## Area grades",
        "",
        "| Area | Grade |",
        "|------|-------|",
    ]
    for k, v in sorted((payload.get("area_grades") or {}).items()):
        lines.append(f"| `{k}` | **{v}** |")
    a2 = payload.get("a2") or {}
    lines += [
        "",
        "## A2 CFD pause (preserve)",
        "",
        f"- marker_active: `{a2.get('marker_active')}`",
        f"- cfd_trading_paused: `{a2.get('cfd_trading_paused')}`",
        f"- sb_trading_paused: `{a2.get('sb_trading_paused')}`",
        "",
        "## Cash merge",
        "",
        f"```json\n{json.dumps(payload.get('cash_merge') or {}, indent=2)}\n```",
        "",
        "## Ranked findings",
        "",
    ]
    findings = payload.get("findings") or []
    if not findings:
        lines.append("_None._")
    else:
        for f in findings:
            lines.append(
                f"{f.get('rank')}. **{str(f.get('severity')).upper()}** "
                f"`{f.get('class')}` — {f.get('title')}  \n"
                f"   {f.get('detail')}"
            )
    chip = payload.get("dashboard_chip") or {}
    if chip:
        lines += [
            "",
            "## Dashboard chip",
            "",
            f"- visible: `{chip.get('visible')}`",
            f"- label: `{chip.get('label')}`",
            f"- summary: {chip.get('summary')}",
            f"- tone: `{chip.get('tone')}`",
            "",
        ]
    handoff = payload.get("cursor_handoff")
    if handoff:
        lines += ["", "## Cursor handoff", ""]
        if isinstance(handoff, dict):
            lines += [
                f"**Symptom:** {handoff.get('symptom')}  ",
                f"**Detail:** {handoff.get('detail')}",
                "",
                "### Suspected files",
                "",
            ]
            for sf in handoff.get("suspected_files") or []:
                lines.append(f"- `{sf}`")
            lines += ["", "### Allowed actions", ""]
            for a in handoff.get("allowed_actions") or []:
                lines.append(f"- {a}")
            lines += ["", "### Forbidden actions", ""]
            for a in handoff.get("forbidden_actions") or []:
                lines.append(f"- {a}")
            lines += ["", "### Paste blurb", "", "```", str(handoff.get("blurb") or ""), "```"]
            lines += [
                "",
                "### Evidence (JSON)",
                "",
                f"```json\n{json.dumps(handoff.get('evidence') or {}, indent=2, default=str)}\n```",
            ]
        else:
            lines += ["```", str(handoff), "```"]
    silence = payload.get("silence") or {}
    if silence:
        lines += [
            "",
            "## Silence timer (SB)",
            "",
            f"- sb_armed: `{silence.get('sb_armed')}`",
            f"- silence_minutes: `{silence.get('silence_minutes')}` / threshold `{silence.get('threshold_minutes')}`",
            "",
        ]
    heal = payload.get("heal") or {}
    if heal:
        lines += [
            "",
            "## Phase 2 heal",
            "",
            f"```json\n{json.dumps(heal, indent=2, default=str)}\n```",
            "",
        ]
    lines += [
        "",
        "## Policy",
        "",
        "- Phase 1: observe + score + write queue + cursor_handoff + dashboard chip",
        "- Phase 2: allowlisted heals only (soft SIGTERM recycle / UI restart / A2 reapply / silence soft-pause)",
        "- Forbidden: kill -9, raise REST cap, re-enable Instant/micro, loosen ElasticGate, strategy rewrites",
        "",
    ]
    return "\n".join(lines) + "\n"


def write_sot(payload: dict[str, Any], *, data_root: Path | None = None) -> dict[str, str]:
    root = data_root or _data_root()
    state_dir = root / "state"
    reports_dir = root / "reports"
    state_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    json_path = state_dir / "gui_supervisor_latest.json"
    md_path = reports_dir / "gui_supervisor_latest.md"
    hist_path = state_dir / "gui_supervisor_history.jsonl"

    json_path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    md_path.write_text(_markdown_report(payload), encoding="utf-8")

    compact = {
        "ts": payload.get("ts"),
        "checked_at": payload.get("checked_at"),
        "score": payload.get("score"),
        "needs_code": payload.get("needs_code"),
        "needs_ops": payload.get("needs_ops"),
        "a2_marker_active": (payload.get("a2") or {}).get("marker_active"),
        "cfd_paused": (payload.get("a2") or {}).get("cfd_trading_paused"),
        "cash_mode": (payload.get("cash_merge") or {}).get("mode"),
        "finding_titles": [f.get("title") for f in (payload.get("findings") or []) if f.get("severity") in ("fail", "watch")][:12],
    }
    with hist_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(compact, default=str) + "\n")
    # Trim history if huge
    try:
        lines = hist_path.read_text(encoding="utf-8").splitlines()
        if len(lines) > HISTORY_MAX_LINES:
            hist_path.write_text("\n".join(lines[-HISTORY_MAX_LINES:]) + "\n", encoding="utf-8")
    except Exception:
        pass

    return {
        "json": str(json_path),
        "md": str(md_path),
        "history": str(hist_path),
    }


def run_once(
    *,
    write: bool = True,
    heal: bool = False,
    heal_dry_run: bool = False,
) -> dict[str, Any]:
    payload = assess()
    # Explicit --heal executes; --heal-dry-run or IG_GUI_SUP_AUTO_HEAL=1 plans dry-run only
    if heal or heal_dry_run or AUTO_HEAL_DEFAULT:
        from runtime.gui_desk_supervisor_heal import execute_heal_plan, plan_heals

        plans = plan_heals(payload)
        execute = bool(heal) and not bool(heal_dry_run)
        heal_result = execute_heal_plan(plans, dry_run=not execute)
        payload["heal"] = {"plans": plans, "result": heal_result, "executed": execute}
        if heal_result.get("hard_fail"):
            payload["score"] = "FAIL"
            payload.setdefault("findings", []).insert(
                0,
                _finding(
                    rank=1,
                    severity="fail",
                    klass="ops",
                    title="Phase-2 heal cap exceeded (hard FAIL)",
                    detail=str(heal_result.get("message") or "2 heals/hour cap"),
                    needs_ops=True,
                    evidence=heal_result,
                ),
            )
            payload["needs_ops"] = True
    paths = write_sot(payload) if write else {}
    payload["_written"] = paths
    return payload


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        description="GUI/Desk supervisor Phase 1+2 — observe/score + allowlisted heal"
    )
    p.add_argument("--dry-run", action="store_true", help="Assess but do not write SoT files")
    p.add_argument("--json-stdout", action="store_true", help="Print full JSON payload to stdout")
    p.add_argument(
        "--heal",
        action="store_true",
        help="Execute allowlisted Phase-2 heals (respects open-book + 2/hour cap; never kill -9)",
    )
    p.add_argument(
        "--heal-dry-run",
        action="store_true",
        help="Plan allowlisted heals and audit without mutating agents/UI",
    )
    args = p.parse_args(argv)
    payload = run_once(
        write=not args.dry_run,
        heal=bool(args.heal),
        heal_dry_run=bool(args.heal_dry_run),
    )
    if args.json_stdout:
        print(json.dumps(payload, indent=2, default=str))
    else:
        written = payload.get("_written") or {}
        heal = payload.get("heal") or {}
        print(
            f"score={payload.get('score')} needs_code={payload.get('needs_code')} "
            f"needs_ops={payload.get('needs_ops')} a2={ (payload.get('a2') or {}).get('marker_active') }"
        )
        if heal:
            plans = heal.get("plans") or []
            print(f"heal_plans={len(plans)} heal_ok={(heal.get('result') or {}).get('ok')}")
        if written:
            print(f"json={written.get('json')}")
            print(f"md={written.get('md')}")
    return 0 if payload.get("score") != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
