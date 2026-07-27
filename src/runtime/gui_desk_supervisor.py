"""GUI / Trading Desk supervisor — Phase 1 observe/score + Phase 2 allowlisted heal.

Phase 1: observe + score + write resolve queue + cursor_handoff + dashboard chip.
Phase 2: allowlisted self-heal only (see gui_desk_supervisor_heal.py).
Writes SoT under IG_DATA_ROOT for Cursor / operator handoff.
"""

from __future__ import annotations

import csv
import json
import os
import socket
import statistics
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PHASE = 2
SCHEMA_VERSION = 4
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
NIKKEI_EPIC = "IX.D.NIKKEI.IFM.IP"
SILENCE_MINUTES = float(os.environ.get("IG_GUI_SUP_SILENCE_MINUTES", "30"))
AUTO_HEAL_DEFAULT = os.environ.get("IG_GUI_SUP_AUTO_HEAL", "0").strip() in ("1", "true", "True", "yes")

# --- Phase-2 desk integrity thresholds (env-overridable) ---
BLEED_WINDOW_MINUTES = float(os.environ.get("IG_GUI_SUP_BLEED_WINDOW_MINUTES", "180"))
BLEED_MIN_TRADES = int(os.environ.get("IG_GUI_SUP_BLEED_MIN_TRADES", "5"))
BLEED_MAX_WR = float(os.environ.get("IG_GUI_SUP_BLEED_MAX_WR", "0.25"))  # FAIL when WR < this
BLEED_MAX_NET_GBP = float(os.environ.get("IG_GUI_SUP_BLEED_MAX_NET_GBP", "-50.0"))  # FAIL when net <= this
MICRO_HOLD_MEDIAN_SEC = float(os.environ.get("IG_GUI_SUP_MICRO_HOLD_MEDIAN_SEC", "60"))
MICRO_HOLD_AVG_SEC = float(os.environ.get("IG_GUI_SUP_MICRO_HOLD_AVG_SEC", "90"))
MICRO_HOLD_MIN_SAMPLES = int(os.environ.get("IG_GUI_SUP_MICRO_HOLD_MIN_SAMPLES", "3"))
SESSION_KILL_NET_GBP = float(os.environ.get("IG_GUI_SUP_SESSION_KILL_NET_GBP", "-150.0"))
POST_CUTOVER_MINUTES = float(os.environ.get("IG_GUI_SUP_POST_CUTOVER_MINUTES", "30"))
POST_CUTOVER_HOLD_SEC = float(os.environ.get("IG_GUI_SUP_POST_CUTOVER_HOLD_SEC", "120"))
FLICKER_WINDOW_MINUTES = float(os.environ.get("IG_GUI_SUP_FLICKER_WINDOW_MINUTES", "15"))
FLICKER_MAX_FLIPS = int(os.environ.get("IG_GUI_SUP_FLICKER_MAX_FLIPS", "6"))


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
        return {"status": "unavailable"}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {"status": "unavailable"}
        try:
            from trading.gate_funnel_counter import classify_funnel_status

            raw = dict(raw)
            raw["status"] = classify_funnel_status(raw)
        except Exception:
            raw.setdefault("status", "ok")
        return raw
    except Exception:
        return {"status": "unavailable"}


def _latest_ml_strategy_review(data_root: Path) -> dict[str, Any]:
    """Read newest ml_strategy_review_*.json (observe-only)."""
    try:
        from diagnostics.ml_strategy_review import load_latest_review_verdict

        verdict, path = load_latest_review_verdict(data_root)
        if not path:
            return {}
        payload: dict[str, Any] = {"verdict": verdict, "path": str(path)}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                payload["day"] = raw.get("day")
                payload["next_one_step"] = raw.get("next_one_step")
                payload["generated_at"] = raw.get("generated_at")
        except Exception:
            pass
        return payload
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


def _parse_iso_ts(raw: str | None) -> datetime | None:
    s = (raw or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _journal_path(data_root: Path) -> Path:
    return data_root / "metrics" / "daily_journal.csv"


def _ml_outcomes_path(data_root: Path) -> Path:
    return data_root / "metrics" / "ml_trade_outcomes.jsonl"


def _epic_from_journal_row(row: dict[str, Any]) -> str | None:
    for key in ("Epic", "epic", "Instrument", "Asset"):
        val = str(row.get(key) or "").strip()
        if val.startswith(("IX.", "CS.", "KA.")):
            return val
    # Infer from common labels when epic column absent
    asset = str(row.get("Asset") or row.get("asset") or "").upper()
    if "NIKKEI" in asset or "JAPAN" in asset:
        return NIKKEI_EPIC
    if "DOW" in asset or "WALL" in asset:
        return DOW_EPIC
    if "GOLD" in asset:
        return "CS.D.CFPGOLD.CFP.IP"
    if "EUR" in asset:
        return "CS.D.EURUSD.CFD.IP"
    return None


def _read_recent_closes(
    data_root: Path,
    *,
    window_minutes: float,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Recent DI* closes from daily_journal (+ hold enrichment from ml outcomes)."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=float(window_minutes))
    holds_by_deal: dict[str, float] = {}
    ml_path = _ml_outcomes_path(data_root)
    if ml_path.is_file():
        try:
            with ml_path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(row, dict):
                        continue
                    did = str(row.get("deal_id") or row.get("DealID") or "").strip()
                    hs = row.get("hold_sec")
                    if hs is None:
                        hs = row.get("hold_duration_seconds")
                    if not did or hs is None:
                        continue
                    try:
                        holds_by_deal[did] = float(hs)
                    except (TypeError, ValueError):
                        continue
        except OSError:
            pass

    out: list[dict[str, Any]] = []
    path = _journal_path(data_root)
    if not path.is_file():
        return out
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                did = str(row.get("DealID") or "").strip()
                if not did.startswith("DI"):
                    continue
                ts = _parse_iso_ts(row.get("Timestamp"))
                if ts is None or ts < cutoff:
                    continue
                try:
                    pnl = float(row.get("RealizedPnL_GBP") or 0.0)
                except (TypeError, ValueError):
                    pnl = 0.0
                hold: float | None = None
                raw_hold = row.get("HoldSec")
                if raw_hold not in (None, ""):
                    try:
                        hold = float(raw_hold)
                    except (TypeError, ValueError):
                        hold = None
                if hold is None and did in holds_by_deal:
                    hold = holds_by_deal[did]
                out.append(
                    {
                        "deal_id": did,
                        "ts": ts,
                        "pnl_gbp": pnl,
                        "hold_sec": hold,
                        "epic": _epic_from_journal_row(row),
                        "account_id": str(row.get("AccountID") or "").strip() or None,
                        "product_type": str(row.get("ProductType") or "").strip() or None,
                        "engine_origin": str(row.get("EngineOrigin") or "").strip() or None,
                    }
                )
    except OSError:
        return out
    out.sort(key=lambda r: r["ts"])
    return out


def _close_stats(closes: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(closes)
    if n == 0:
        return {
            "n": 0,
            "wins": 0,
            "losses": 0,
            "wr": None,
            "net_gbp": 0.0,
            "holds": [],
            "median_hold_sec": None,
            "avg_hold_sec": None,
            "hold_samples": 0,
        }
    wins = sum(1 for c in closes if float(c.get("pnl_gbp") or 0) > 0)
    losses = sum(1 for c in closes if float(c.get("pnl_gbp") or 0) < 0)
    net = sum(float(c.get("pnl_gbp") or 0) for c in closes)
    holds = [float(c["hold_sec"]) for c in closes if c.get("hold_sec") is not None]
    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "wr": (wins / n) if n else None,
        "net_gbp": round(net, 4),
        "holds": holds,
        "median_hold_sec": round(statistics.median(holds), 3) if holds else None,
        "avg_hold_sec": round(statistics.mean(holds), 3) if holds else None,
        "hold_samples": len(holds),
    }


def _calendar_day_net_gbp(data_root: Path, *, day: str | None = None) -> dict[str, Any]:
    """Today's calendar-day journal net (London-ish date string YYYY-MM-DD)."""
    day = day or datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    closes = _read_recent_closes(data_root, window_minutes=36 * 60)
    day_closes = [c for c in closes if c["ts"].astimezone().strftime("%Y-%m-%d") == day]
    stats = _close_stats(day_closes)
    stats["day"] = day
    return stats


def _reopen_witness_path(data_root: Path) -> Path:
    return data_root / "state" / "operator_reopen_witness.json"


def write_reopen_witness(
    data_root: Path,
    *,
    day_net_at_reopen: float | None = None,
    reason: str = "operator_reopen_live_witness",
) -> dict[str, Any]:
    """Stamp witness mode so pre-halt journal damage does not instantly re-lock."""
    path = _reopen_witness_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "active": True,
        "reason": reason,
        "reopened_at": _now_iso(),
        "reopened_at_epoch": time.time(),
        "day_net_at_reopen_gbp": day_net_at_reopen,
        "policy": (
            "ensure_bleed_halt only on NEW closes after reopened_at_epoch "
            "or day_net worsening vs day_net_at_reopen; Instant/micro stay HARD OFF"
        ),
    }
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    return body


def _load_reopen_witness(data_root: Path) -> dict[str, Any] | None:
    path = _reopen_witness_path(data_root)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict) or raw.get("active") is False:
        return None
    return raw


def _load_bleed_lock(data_root: Path) -> dict[str, Any] | None:
    try:
        from runtime.gui_desk_supervisor_heal import load_operator_bleed_lock

        return load_operator_bleed_lock(root=data_root)
    except Exception:
        # Fallback: direct glob
        for sub in ("state_cfd", "state_sb", "state"):
            d = data_root / sub
            if not d.is_dir():
                continue
            for path in sorted(d.glob("operator_bleed_lock_*.json")):
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if isinstance(raw, dict) and raw.get("active", True) is not False:
                    if raw.get("do_not_auto_resume", True) is not False:
                        out = dict(raw)
                        out["_path"] = str(path)
                        return out
        return None


def _flicker_state_path(data_root: Path) -> Path:
    return data_root / "state" / "gui_supervisor_flicker.json"


def _update_flicker_tracker(
    *,
    data_root: Path,
    prefer_epic: str | None,
    setup_epics: list[str],
    now: float | None = None,
) -> dict[str, Any]:
    """Track prefer/SETUP flips across supervisor cycles (cheap disk state)."""
    now = float(now if now is not None else time.time())
    path = _flicker_state_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    state: dict[str, Any] = {"events": []}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state = raw
        except Exception:
            state = {"events": []}
    events = [e for e in (state.get("events") or []) if isinstance(e, dict)]
    cutoff = now - float(FLICKER_WINDOW_MINUTES) * 60.0
    events = [e for e in events if float(e.get("ts") or 0) >= cutoff]
    prev_prefer = state.get("last_prefer_epic")
    prev_setup = set(str(x) for x in (state.get("last_setup_epics") or []))
    cur_setup = set(str(x) for x in setup_epics if str(x).strip())
    flipped = False
    if prev_prefer is not None and str(prefer_epic or "") != str(prev_prefer or ""):
        events.append(
            {
                "ts": now,
                "kind": "prefer",
                "from": prev_prefer,
                "to": prefer_epic,
            }
        )
        flipped = True
    if prev_setup and cur_setup != prev_setup:
        events.append(
            {
                "ts": now,
                "kind": "setup",
                "from": sorted(prev_setup),
                "to": sorted(cur_setup),
            }
        )
        flipped = True
    state = {
        "last_prefer_epic": prefer_epic,
        "last_setup_epics": sorted(cur_setup),
        "events": events[-80:],
        "flip_count_window": len(events),
        "window_minutes": FLICKER_WINDOW_MINUTES,
        "threshold_flips": FLICKER_MAX_FLIPS,
        "updated_at": now,
        "last_flipped": flipped,
    }
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return state


def _extract_rotation_gui_signals(rotation: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(rotation, dict):
        return {
            "prefer_epic": None,
            "preference_reason": None,
            "setup_epics": [],
            "wait_epics": [],
            "rows": [],
            "per_epic": {},
        }
    rot = rotation.get("rotation") if isinstance(rotation.get("rotation"), dict) else rotation
    ranked = rot.get("ranked_rotator") if isinstance(rot.get("ranked_rotator"), dict) else {}
    prefer = (
        rot.get("prefer_epic")
        or rotation.get("prefer_epic")
        or ranked.get("prefer_epic")
        or ranked.get("dominant")
    )
    prefer_s = str(prefer).strip() if prefer else None
    reason = rot.get("preference_reason") or rotation.get("preference_reason") or ranked.get("preference_reason")
    rows = list(ranked.get("rows") or [])
    per_epic = (
        rot.get("per_epic_confidence")
        or rotation.get("per_epic_confidence")
        or ranked.get("per_epic_confidence")
        or {}
    )
    if not isinstance(per_epic, dict):
        per_epic = {}
    setup_epics: list[str] = []
    wait_epics: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        epic = str(row.get("epic") or "").strip()
        mode = str(row.get("mode") or "").upper()
        if not epic:
            continue
        if mode == "SETUP":
            setup_epics.append(epic)
        elif mode == "WAIT":
            wait_epics.append(epic)
    for epic, meta in per_epic.items():
        if not isinstance(meta, dict):
            continue
        mode = str(meta.get("mode") or "").upper()
        e = str(epic).strip()
        if mode == "SETUP" and e and e not in setup_epics:
            setup_epics.append(e)
        if mode == "WAIT" and e and e not in wait_epics:
            wait_epics.append(e)
    return {
        "prefer_epic": prefer_s,
        "preference_reason": reason,
        "setup_epics": setup_epics,
        "wait_epics": wait_epics,
        "rows": rows,
        "per_epic": per_epic,
    }


def _sb_aggregate_setup_mode(ops_strip: dict[str, Any] | None, gui_sig: dict[str, Any]) -> str | None:
    """Approximate Intent SB aggregate: SETUP only when sniper approved above threshold."""
    sniper = None
    if isinstance(ops_strip, dict):
        sniper = ops_strip.get("sniper_ml") if isinstance(ops_strip.get("sniper_ml"), dict) else None
    if isinstance(sniper, dict) and sniper.get("p_success") is not None:
        try:
            p = float(sniper.get("p_success"))
        except (TypeError, ValueError):
            p = None
        try:
            thr = float(sniper.get("threshold")) if sniper.get("threshold") is not None else 0.68
        except (TypeError, ValueError):
            thr = 0.68
        approved = sniper.get("approved") is True
        if p is not None:
            return "SETUP" if approved and p >= thr else "WAIT"
    # Fallback: prefer epic mode from ranked
    prefer = gui_sig.get("prefer_epic")
    per = gui_sig.get("per_epic") if isinstance(gui_sig.get("per_epic"), dict) else {}
    if prefer and prefer in per and isinstance(per[prefer], dict):
        mode = str(per[prefer].get("mode") or "").upper()
        if mode in ("SETUP", "WAIT"):
            return mode
    return None


def _phase2_integrity_checks(
    *,
    data_root: Path,
    findings: list[dict[str, Any]],
    area_grades: dict[str, str],
    dual_cfg: dict[str, Any],
    sb_paused: bool | None,
    cfd_paused: bool | None,
    sb_bundle: dict[str, Any],
    cfd_bundle: dict[str, Any],
    path_a_claimed: bool,
) -> dict[str, Any]:
    """BLEED / MICRO_HOLD / GUI_LIE / FLICKER / SESSION_KILL / POST_CUTOVER / EPIC_POLICY / HALTED."""
    meta: dict[str, Any] = {
        "halted": False,
        "ensure_bleed_halt": False,
        "alerts": [],
        "bleed_lock": None,
        "journal_window": {},
        "session_day": {},
        "post_cutover": {},
        "flicker": {},
        "gui_signals": {},
    }
    alerts: list[str] = []

    lock = _load_bleed_lock(data_root)
    meta["bleed_lock"] = (
        {k: lock.get(k) for k in ("active", "reason", "do_not_auto_resume", "mode", "_path", "date") if k in lock}
        if isinstance(lock, dict)
        else None
    )
    locked = bool(lock)
    witness = _load_reopen_witness(data_root)
    meta["reopen_witness"] = (
        {
            k: witness.get(k)
            for k in ("active", "reason", "reopened_at", "reopened_at_epoch", "day_net_at_reopen_gbp")
            if isinstance(witness, dict) and k in witness
        }
        if isinstance(witness, dict)
        else None
    )
    witness_epoch = None
    if isinstance(witness, dict) and witness.get("reopened_at_epoch") is not None:
        try:
            witness_epoch = float(witness.get("reopened_at_epoch"))
        except (TypeError, ValueError):
            witness_epoch = None

    window_closes = _read_recent_closes(data_root, window_minutes=BLEED_WINDOW_MINUTES)
    window_stats = _close_stats(window_closes)
    window_stats["window_minutes"] = BLEED_WINDOW_MINUTES
    meta["journal_window"] = window_stats

    day_stats = _calendar_day_net_gbp(data_root)
    meta["session_day"] = day_stats

    cutover_closes = _read_recent_closes(data_root, window_minutes=POST_CUTOVER_MINUTES)
    cutover_stats = _close_stats(cutover_closes)
    cutover_stats["window_minutes"] = POST_CUTOVER_MINUTES
    meta["post_cutover"] = cutover_stats

    # Post-reopen: only NEW closes arm auto-lock (pre-halt journal must not instantly re-lock).
    fresh_closes = window_closes
    if witness_epoch is not None:
        fresh_closes = [
            c
            for c in window_closes
            if float(c["ts"].timestamp()) >= witness_epoch
        ]
    fresh_stats = _close_stats(fresh_closes)
    meta["journal_fresh_since_reopen"] = {
        **fresh_stats,
        "witness_epoch": witness_epoch,
    }

    # --- HALTED / BLEED lock posture ---
    if locked:
        meta["halted"] = True
        alerts.append("HALTED")
        alerts.append("BLEED")
        area_grades["halted_posture"] = "FAIL"
        findings.append(
            _finding(
                rank=1,
                severity="fail",
                klass="ops",
                title="HALTED: operator bleed lock active (do_not_auto_resume)",
                detail=(
                    f"lock={lock.get('_path')} reason={lock.get('reason')} — "
                    "heals must NOT POST /api/start; chip must not show PASS"
                ),
                needs_ops=True,
                evidence={"lock": meta["bleed_lock"]},
            )
        )
    else:
        area_grades["halted_posture"] = "PASS"

    # --- BLEED (recent WR/net) ---
    bleed_hit = False
    wr = window_stats.get("wr")
    net = float(window_stats.get("net_gbp") or 0.0)
    n = int(window_stats.get("n") or 0)
    fresh_n = int(fresh_stats.get("n") or 0)
    fresh_wr = fresh_stats.get("wr")
    fresh_net = float(fresh_stats.get("net_gbp") or 0.0)
    bleed_window_hit = n >= BLEED_MIN_TRADES and (
        (wr is not None and float(wr) < BLEED_MAX_WR) or net <= BLEED_MAX_NET_GBP
    )
    bleed_fresh_hit = fresh_n >= BLEED_MIN_TRADES and (
        (fresh_wr is not None and float(fresh_wr) < BLEED_MAX_WR) or fresh_net <= BLEED_MAX_NET_GBP
    )
    if bleed_window_hit:
        bleed_hit = True
        if "BLEED" not in alerts:
            alerts.append("BLEED")
        # Under witness, pre-reopen window damage is WATCH unless fresh closes also bleed.
        sev = "fail" if (not witness_epoch or bleed_fresh_hit or locked) else "watch"
        area_grades["bleed"] = "FAIL" if sev == "fail" else "WATCH"
        findings.append(
            _finding(
                rank=1 if sev == "fail" else 2,
                severity=sev,
                klass="ops",
                title="BLEED: recent closes WR/net below threshold",
                detail=(
                    f"window={BLEED_WINDOW_MINUTES:.0f}m n={n} wr={wr} "
                    f"(max_ok={BLEED_MAX_WR}) net=£{net} (floor={BLEED_MAX_NET_GBP}). "
                    + (
                        f"Witness: fresh_n={fresh_n} fresh_net=£{fresh_net} "
                        f"auto-lock={'YES' if bleed_fresh_hit else 'held (prior damage)'}."
                        if witness_epoch
                        else "Ensure pause both + durable bleed lock."
                    )
                ),
                needs_ops=True,
                evidence={
                    "stats": window_stats,
                    "fresh_stats": fresh_stats,
                    "witness": meta["reopen_witness"],
                    "thresholds": {
                        "window_minutes": BLEED_WINDOW_MINUTES,
                        "min_trades": BLEED_MIN_TRADES,
                        "max_wr": BLEED_MAX_WR,
                        "max_net_gbp": BLEED_MAX_NET_GBP,
                    },
                    "heal": "ensure_operator_bleed_halt" if (not witness_epoch or bleed_fresh_hit) else None,
                },
            )
        )
        if not locked and (bleed_fresh_hit if witness_epoch else True):
            meta["ensure_bleed_halt"] = True
    else:
        area_grades.setdefault("bleed", "PASS" if not locked else "FAIL")

    # --- SESSION_KILL (day net) ---
    day_net = float(day_stats.get("net_gbp") or 0.0)
    day_n = int(day_stats.get("n") or 0)
    day_net_at_reopen = None
    if isinstance(witness, dict) and witness.get("day_net_at_reopen_gbp") is not None:
        try:
            day_net_at_reopen = float(witness.get("day_net_at_reopen_gbp"))
        except (TypeError, ValueError):
            day_net_at_reopen = None
    day_worsened = (
        day_net_at_reopen is not None and day_net <= (day_net_at_reopen - 1.0)
    )
    if day_n > 0 and day_net <= SESSION_KILL_NET_GBP:
        if "BLEED" not in alerts:
            alerts.append("BLEED")
        alerts.append("SESSION_KILL")
        # Witness: prior day breach is visible WATCH until PnL worsens after reopen.
        sev = "fail" if (not witness_epoch or day_worsened or locked) else "watch"
        area_grades["session_kill"] = "FAIL" if sev == "fail" else "WATCH"
        findings.append(
            _finding(
                rank=1 if sev == "fail" else 2,
                severity=sev,
                klass="ops",
                title="SESSION_KILL: day realized PnL beyond −£X",
                detail=(
                    f"day={day_stats.get('day')} n={day_n} net=£{day_net} "
                    f"(kill_floor={SESSION_KILL_NET_GBP}). "
                    + (
                        f"Witness reopen_net=£{day_net_at_reopen}; "
                        f"worsened={day_worsened}; auto-lock={'YES' if day_worsened else 'held'}."
                        if witness_epoch
                        else "Stop both + lock."
                    )
                ),
                needs_ops=True,
                evidence={
                    "day_stats": day_stats,
                    "witness": meta["reopen_witness"],
                    "day_worsened": day_worsened,
                    "heal": "ensure_operator_bleed_halt" if (not witness_epoch or day_worsened) else None,
                },
            )
        )
        if not locked and (day_worsened if witness_epoch else True):
            meta["ensure_bleed_halt"] = True
            bleed_hit = True
    else:
        area_grades["session_kill"] = "PASS"

    # --- MICRO_HOLD ---
    med = window_stats.get("median_hold_sec")
    avg = window_stats.get("avg_hold_sec")
    hold_n = int(window_stats.get("hold_samples") or 0)
    if path_a_claimed and hold_n >= MICRO_HOLD_MIN_SAMPLES and (
        (med is not None and float(med) < MICRO_HOLD_MEDIAN_SEC)
        or (avg is not None and float(avg) < MICRO_HOLD_AVG_SEC)
    ):
        alerts.append("MICRO_HOLD")
        area_grades["micro_hold"] = "FAIL"
        findings.append(
            _finding(
                rank=1,
                severity="fail",
                klass="code",
                title="MICRO_HOLD: holds too short while macro/Path A claimed",
                detail=(
                    f"median_hold={med}s avg_hold={avg}s samples={hold_n} "
                    f"(limits median<{MICRO_HOLD_MEDIAN_SEC}s or avg<{MICRO_HOLD_AVG_SEC}s) "
                    "with SB Path A / macro carve expected — micro masquerading as macro."
                ),
                needs_code=True,
                needs_ops=True,
                evidence={
                    "stats": window_stats,
                    "path_a_claimed": path_a_claimed,
                    "heal": "ensure_operator_bleed_halt",
                },
            )
        )
        # APP: MICRO_HOLD FAIL must force pause + durable lock (same as BLEED).
        if not locked:
            meta["ensure_bleed_halt"] = True
    elif path_a_claimed and n >= BLEED_MIN_TRADES and hold_n == 0:
        area_grades["micro_hold"] = "WATCH"
        findings.append(
            _finding(
                rank=3,
                severity="watch",
                klass="code",
                title="MICRO_HOLD: hold telemetry missing under Path A",
                detail=f"n={n} closes in window but HoldSec samples=0 — cannot certify macro holds",
                needs_code=True,
                evidence={"stats": window_stats},
            )
        )
    else:
        area_grades["micro_hold"] = "PASS"

    # --- POST_CUTOVER_OUTCOME ---
    c_net = float(cutover_stats.get("net_gbp") or 0.0)
    c_n = int(cutover_stats.get("n") or 0)
    c_med = cutover_stats.get("median_hold_sec")
    short_cutover = (
        c_n > 0
        and c_net < 0
        and (
            (c_med is not None and float(c_med) < POST_CUTOVER_HOLD_SEC)
            or int(cutover_stats.get("hold_samples") or 0) == 0
        )
    )
    if short_cutover:
        sev = "fail" if (c_med is not None and float(c_med) < POST_CUTOVER_HOLD_SEC) or c_net <= BLEED_MAX_NET_GBP else "watch"
        if sev == "fail":
            alerts.append("POST_CUTOVER")
        area_grades["post_cutover"] = "FAIL" if sev == "fail" else "WATCH"
        findings.append(
            _finding(
                rank=1 if sev == "fail" else 2,
                severity=sev,
                klass="ops",
                title="POST_CUTOVER_OUTCOME: recent closes net-neg / short holds",
                detail=(
                    f"last {POST_CUTOVER_MINUTES:.0f}m n={c_n} net=£{c_net} "
                    f"median_hold={c_med}s — never score PASS/PIPELINE_OK"
                ),
                needs_ops=True,
                needs_code=sev == "fail",
                evidence={"stats": cutover_stats},
            )
        )
    else:
        area_grades["post_cutover"] = "PASS"

    # --- GUI_LIE / FLICKER from rotation APIs ---
    sb_rot = sb_bundle.get("rotation") if isinstance(sb_bundle.get("rotation"), dict) else None
    gui_sig = _extract_rotation_gui_signals(sb_rot)
    meta["gui_signals"] = {
        "prefer_epic": gui_sig.get("prefer_epic"),
        "setup_epics": gui_sig.get("setup_epics"),
        "wait_epics": gui_sig.get("wait_epics"),
        "preference_reason": gui_sig.get("preference_reason"),
    }
    sb_ops = sb_bundle.get("ops_strip") if isinstance(sb_bundle.get("ops_strip"), dict) else None
    sb_agg = _sb_aggregate_setup_mode(sb_ops, gui_sig)
    prefer = gui_sig.get("prefer_epic")
    setup_epics = list(gui_sig.get("setup_epics") or [])
    prefer_mode = None
    if prefer and isinstance(gui_sig.get("per_epic"), dict):
        meta_pe = gui_sig["per_epic"].get(prefer)
        if isinstance(meta_pe, dict):
            prefer_mode = str(meta_pe.get("mode") or "").upper() or None
    if prefer_mode is None and prefer in setup_epics:
        prefer_mode = "SETUP"

    gui_lie = False
    # Prefer / SETUP while the *armed* SB desk is paused = trust break.
    # CFD A2 pause with SB live + ranked SETUP is the intended Step-2 posture (not a lie).
    if sb_paused is True and (prefer or setup_epics):
        gui_lie = True
        alerts.append("GUI_LIE")
        area_grades["gui_lie"] = "FAIL"
        findings.append(
            _finding(
                rank=1,
                severity="fail",
                klass="ui",
                title="GUI_LIE: prefer/SETUP while desk paused",
                detail=(
                    f"sb_paused={sb_paused} cfd_paused={cfd_paused} "
                    f"prefer={prefer} setup_epics={setup_epics} — Intent must not show SETUP on paused SB"
                ),
                needs_code=True,
                evidence={
                    "gui_signals": meta["gui_signals"],
                    "sb_paused": sb_paused,
                    "cfd_paused": cfd_paused,
                },
            )
        )
    elif cfd_paused is True and sb_paused is not True and (prefer or setup_epics):
        # A2 CFD pause — note only; SB is the Intent primary.
        findings.append(
            _finding(
                rank=91,
                severity="info",
                klass="ignore",
                title="A2 CFD pause with SB prefer/SETUP (expected)",
                detail=f"cfd_paused=true sb live; prefer={prefer} setup_epics={setup_epics}",
                evidence={"prefer": prefer, "setup_epics": setup_epics},
            )
        )
    # SETUP on prefer while SB aggregate WAIT
    elif prefer_mode == "SETUP" and sb_agg == "WAIT" and sb_paused is not True:
        gui_lie = True
        alerts.append("GUI_LIE")
        area_grades["gui_lie"] = "FAIL"
        findings.append(
            _finding(
                rank=1,
                severity="fail",
                klass="ui",
                title="GUI_LIE: Intent SETUP vs SB WAIT contradiction",
                detail=(
                    f"prefer={prefer} prefer_mode=SETUP but SB aggregate={sb_agg} — "
                    "strip would flash SETUP against WAIT"
                ),
                needs_code=True,
                evidence={"prefer": prefer, "prefer_mode": prefer_mode, "sb_aggregate": sb_agg},
            )
        )
    elif setup_epics and sb_agg == "WAIT" and sb_paused is not True:
        alerts.append("GUI_LIE")
        area_grades["gui_lie"] = "WATCH"
        findings.append(
            _finding(
                rank=2,
                severity="watch",
                klass="ui",
                title="GUI_LIE: ranked SETUP rows while SB aggregate WAIT",
                detail=f"setup_epics={setup_epics} sb_aggregate=WAIT prefer={prefer}",
                needs_code=True,
                evidence={"setup_epics": setup_epics, "sb_aggregate": sb_agg},
            )
        )
        gui_lie = True
    else:
        area_grades.setdefault("gui_lie", "PASS")

    flicker = _update_flicker_tracker(
        data_root=data_root,
        prefer_epic=prefer,
        setup_epics=setup_epics,
    )
    meta["flicker"] = {
        "flip_count_window": flicker.get("flip_count_window"),
        "threshold_flips": flicker.get("threshold_flips"),
        "window_minutes": flicker.get("window_minutes"),
        "last_prefer_epic": flicker.get("last_prefer_epic"),
    }
    flips = int(flicker.get("flip_count_window") or 0)
    if flips >= FLICKER_MAX_FLIPS:
        alerts.append("FLICKER")
        area_grades["flicker"] = "WATCH"
        findings.append(
            _finding(
                rank=3,
                severity="watch",
                klass="ui",
                title="FLICKER: prefer/SETUP flip rate elevated",
                detail=(
                    f"{flips} flips in {FLICKER_WINDOW_MINUTES:.0f}m "
                    f"(threshold={FLICKER_MAX_FLIPS}) prefer={prefer}"
                ),
                needs_code=True,
                evidence=meta["flicker"],
            )
        )
    else:
        area_grades["flicker"] = "PASS"

    # --- EPIC_POLICY (excluded hot-path) ---
    excluded = set(str(e) for e in (dual_cfg.get("exclude_from_hot_path") or []) if str(e).strip())
    if not excluded:
        excluded = {NIKKEI_EPIC}
    bad_closes = [
        c for c in window_closes if c.get("epic") and str(c.get("epic")) in excluded
    ]
    prefer_excluded = bool(prefer and prefer in excluded)
    promoted_bad = []
    ranked = _ranked_from_rotation(sb_rot)
    for e in ranked.get("promoted") or []:
        if e in excluded:
            promoted_bad.append(e)
    if bad_closes or prefer_excluded or promoted_bad:
        alerts.append("EPIC_POLICY")
        area_grades["epic_policy"] = "FAIL"
        findings.append(
            _finding(
                rank=1,
                severity="fail",
                klass="code",
                title="EPIC_POLICY: excluded epic close/prefer/promote",
                detail=(
                    f"excluded={sorted(excluded)} bad_closes={len(bad_closes)} "
                    f"prefer={prefer} prefer_excluded={prefer_excluded} "
                    f"promoted_intersect_exclude={promoted_bad}"
                ),
                needs_code=True,
                needs_ops=True,
                evidence={
                    "bad_deal_ids": [c.get("deal_id") for c in bad_closes[:8]],
                    "prefer": prefer,
                    "promoted_bad": promoted_bad,
                },
            )
        )
    else:
        area_grades["epic_policy"] = "PASS"

    # Dedupe alerts preserve order
    seen_a: set[str] = set()
    ordered_alerts: list[str] = []
    for a in alerts:
        if a not in seen_a:
            seen_a.add(a)
            ordered_alerts.append(a)
    meta["alerts"] = ordered_alerts
    meta["bleed_hit"] = bleed_hit
    meta["gui_lie"] = gui_lie
    _ = cfd_bundle  # reserved for future dual-port GUI_LIE
    return meta


def _suspected_files_for(title: str, detail: str = "") -> list[str]:
    blob = f"{title} {detail}".lower()
    files: list[str] = []
    if any(x in blob for x in ("gui_lie", "setup", "prefer", "flicker", "desk intent", "intent")):
        files += [
            "terminal/src/lib/desk-intent.ts",
            "terminal/src/components/gpu/GuiSupervisorChip.tsx",
            "src/runtime/gui_desk_supervisor.py",
        ]
    if any(x in blob for x in ("bleed", "session_kill", "post_cutover", "halted")):
        files += [
            "src/runtime/gui_desk_supervisor.py",
            "src/runtime/gui_desk_supervisor_heal.py",
            "docs/GUI_DESK_SUPERVISOR.md",
            "docs/DESK_REOPEN_CHECKLIST.md",
        ]
    if any(x in blob for x in ("micro_hold", "hold_sec", "hold telemetry")):
        files += [
            "src/diagnostics/performance_journal.py",
            "src/diagnostics/ml_trade_outcomes.py",
            "src/system/dual_regime.py",
        ]
    if any(x in blob for x in ("epic_policy", "nikkei", "exclude_from_hot")):
        files += [
            "config/config_v31_demo_throughput.json",
            "src/runtime/dual_core_execution.py",
            "src/runtime/rotation_failover.py",
        ]
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
    ops_findings = [
        f
        for f in findings
        if f.get("needs_ops") and f.get("severity") in ("fail", "watch")
    ]
    queue = code_findings or ops_findings
    if not queue:
        return None
    top = queue[0]
    evidence: dict[str, Any] = {}
    for f in queue[:5]:
        evidence[str(f.get("title") or f"finding_{f.get('rank')}")] = f.get("evidence") or {}
    suspected: list[str] = []
    for f in queue[:5]:
        suspected.extend(_suspected_files_for(str(f.get("title") or ""), str(f.get("detail") or "")))
    suspected_u: list[str] = []
    seen: set[str] = set()
    for x in suspected:
        if x not in seen:
            seen.add(x)
            suspected_u.append(x)
    lines = [f"- [{f.get('class')}] {f.get('title')}: {f.get('detail')}" for f in queue[:8]]
    blurb = (
        "Cursor handoff (GUI desk supervisor queue):\n"
        f"Score={score}. Read `src/data/v31-production/state/gui_supervisor_latest.json`.\n"
        "Preserve A2 CFD pause on :8080. Honour operator bleed locks "
        "(never POST /api/start while do_not_auto_resume).\n"
        "Phase-2 heals are allowlisted only "
        "(port hung soft-recycle, loops unpause/recycle if flat, UI restart, "
        "silence soft-pause SB, ensure_operator_bleed_halt).\n"
        "Do not kill -9 / raise REST / re-enable Instant-micro / loosen ElasticGate.\n"
        "Actionable findings:\n" + "\n".join(lines)
    )
    return {
        "score": score,
        "symptom": str(top.get("title") or "supervisor finding"),
        "detail": str(top.get("detail") or ""),
        "top_finding": {
            "rank": top.get("rank"),
            "severity": top.get("severity"),
            "class": top.get("class"),
            "title": top.get("title"),
            "detail": top.get("detail"),
            "needs_code": bool(top.get("needs_code")),
            "needs_ops": bool(top.get("needs_ops")),
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
            "For BLEED/HALTED: keep paused + locks; follow docs/DESK_REOPEN_CHECKLIST.md (no auto start)",
        ],
        "forbidden_actions": [
            "kill -9 / SIGKILL / isolated kill of main.py",
            "Raise REST 3/min hard cap",
            "Re-enable Instant/micro or loosen ElasticGate/OBI fail-open",
            "Strategy/alpha rewrites or allow_non_dow global unlock",
            "POST /api/start while operator bleed lock / A2 marker active",
            "Remove operator_bleed_lock_*.json without explicit operator unlock",
            "Edit SQLite history / learning DB schema",
            "Heal beyond allowlist or after 2/hour cap",
        ],
        "state_path": "src/data/v31-production/state/gui_supervisor_latest.json",
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

    # ML strategy review APP_BLOCKED → code finding (never auto-resume / loosen)
    ml_review = _latest_ml_strategy_review(root)
    ml_verdict = str(ml_review.get("verdict") or "").strip().upper()
    if ml_verdict == "APP_BLOCKED":
        area_grades["ml_strategy_review"] = "FAIL"
        findings.append(
            _finding(
                rank=2,
                severity="fail",
                klass="code",
                title="ML strategy review APP_BLOCKED",
                detail=(
                    "APP/stamp share dominates measurement — fix APP tickets; "
                    "do NOT loosen LOGIC strategy params; do NOT POST /api/start"
                ),
                needs_code=True,
                evidence={
                    "verdict": ml_verdict,
                    "day": ml_review.get("day"),
                    "path": ml_review.get("path"),
                    "next_one_step": ml_review.get("next_one_step"),
                },
            )
        )
    elif ml_verdict:
        area_grades["ml_strategy_review"] = "WATCH" if ml_verdict == "NOT_MEASURABLE" else "PASS"
        findings.append(
            _finding(
                rank=95,
                severity="info",
                klass="ignore",
                title=f"ML strategy review {ml_verdict}",
                detail=str(ml_review.get("next_one_step") or "")[:240],
                evidence={"verdict": ml_verdict, "day": ml_review.get("day"), "path": ml_review.get("path")},
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
    funnel_status = str(funnel.get("status") or "")
    try:
        funnel_passed = int(funnel.get("all_passed_ticks") or 0)
    except (TypeError, ValueError):
        funnel_passed = 0
    # Stale/empty funnel must not look like live activity forever.
    if funnel_status in {"stale", "empty", "unavailable"}:
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

    # --- Desk integrity: BLEED / MICRO_HOLD / GUI_LIE / FLICKER / SESSION_KILL / … ---
    path_a_claimed = bool(dual_cfg.get("sb_path_a_carve_expected")) or (
        "PATH_A" in set(sb_posture.get("hard_allow") or [])
    )
    integrity = _phase2_integrity_checks(
        data_root=root,
        findings=findings,
        area_grades=area_grades,
        dual_cfg=dual_cfg,
        sb_paused=sb_paused,
        cfd_paused=cfd_paused,
        sb_bundle=sb,
        cfd_bundle=cfd,
        path_a_claimed=path_a_claimed,
    )

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

    # POST_CUTOVER / HALTED: never green PASS
    if integrity.get("halted") or "POST_CUTOVER" in (integrity.get("alerts") or []):
        if score == "PASS":
            score = "WATCH" if not fail_n else "FAIL"
        if integrity.get("halted"):
            score = "FAIL"

    needs_code = any(bool(f.get("needs_code")) for f in findings if f.get("severity") in ("fail", "watch"))
    needs_ops = any(bool(f.get("needs_ops")) for f in findings if f.get("severity") in ("fail", "watch"))

    # Handoff for code (GUI_LIE/MICRO_HOLD/…) and ops-critical BLEED/HALTED
    handoff = None
    if needs_code or (
        needs_ops
        and any(
            str(f.get("title") or "").startswith(("BLEED:", "HALTED:", "SESSION_KILL:"))
            for f in findings
            if f.get("severity") in ("fail", "watch")
        )
    ):
        handoff = _build_cursor_handoff(score=score, findings=findings)

    top_actionable = next(
        (f for f in findings if f.get("severity") in ("fail", "watch")),
        None,
    )
    alerts = list(integrity.get("alerts") or [])
    halted = bool(integrity.get("halted"))
    if halted and "HALTED" not in alerts:
        alerts.insert(0, "HALTED")
    alert_label = " · ".join(alerts) if alerts else ""
    if halted:
        chip_label = "SUPERVISOR HALTED · BLEED LOCK"
        chip_tone = "red"
        chip_visible = True
    elif score in ("WATCH", "FAIL"):
        chip_label = f"SUPERVISOR {score}" + (f" · {alert_label}" if alert_label else "")
        chip_tone = "red" if score == "FAIL" else "amber"
        chip_visible = True
    else:
        chip_label = "SUPERVISOR PASS"
        chip_tone = "green"
        chip_visible = False
    chip_summary = (
        alert_label + (" · " if alert_label and top_actionable else "")
        + (str(top_actionable.get("title") or "") if top_actionable else ("all clear" if score == "PASS" else score))
    )
    chip = {
        "visible": chip_visible,
        "score": "HALTED" if halted else score,
        "label": chip_label,
        "summary": chip_summary,
        "tone": chip_tone,
        "alerts": alerts,
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
        "halted": halted,
        "ensure_bleed_halt": bool(integrity.get("ensure_bleed_halt")),
        "alerts": alerts,
        "needs_code": needs_code,
        "needs_ops": needs_ops,
        "cursor_handoff": handoff,
        "dashboard_chip": chip,
        "integrity": {
            "bleed_lock": integrity.get("bleed_lock"),
            "reopen_witness": integrity.get("reopen_witness"),
            "journal_window": integrity.get("journal_window"),
            "journal_fresh_since_reopen": integrity.get("journal_fresh_since_reopen"),
            "session_day": integrity.get("session_day"),
            "post_cutover": integrity.get("post_cutover"),
            "flicker": integrity.get("flicker"),
            "gui_signals": integrity.get("gui_signals"),
            "alerts": alerts,
        },
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
                "ensure_operator_bleed_halt",
            ],
            "heal_cap_per_hour": int(os.environ.get("IG_GUI_SUP_HEAL_CAP_PER_HOUR", "2")),
            "silence_minutes": SILENCE_MINUTES,
            "bleed_window_minutes": BLEED_WINDOW_MINUTES,
            "bleed_max_wr": BLEED_MAX_WR,
            "bleed_max_net_gbp": BLEED_MAX_NET_GBP,
            "session_kill_net_gbp": SESSION_KILL_NET_GBP,
            "micro_hold_median_sec": MICRO_HOLD_MEDIAN_SEC,
            "post_cutover_minutes": POST_CUTOVER_MINUTES,
            "writes": [
                "gui_supervisor_latest.json",
                "gui_supervisor_latest.md",
                "gui_supervisor_history.jsonl",
                "gui_supervisor_silence.json",
                "gui_supervisor_flicker.json",
                "gui_supervisor_heal_log.jsonl",
                "gui_supervisor_heal_budget.json",
                "operator_bleed_lock_*.json (ensure halt only)",
            ],
        },
    }
    return payload


def _markdown_report(payload: dict[str, Any]) -> str:
    alerts = payload.get("alerts") or []
    lines = [
        f"# GUI Desk Supervisor — Phase {payload.get('phase')}",
        "",
        f"**Score: {payload.get('score')}**  ",
        f"Checked: `{payload.get('checked_at')}`  ",
        f"halted={payload.get('halted')} · alerts=`{' · '.join(alerts) if alerts else '—'}`  ",
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
            f"- alerts: `{chip.get('alerts') or alerts}`",
            "",
        ]
    integrity = payload.get("integrity") or {}
    if integrity:
        lines += [
            "",
            "## Integrity plane",
            "",
            f"```json\n{json.dumps({k: integrity.get(k) for k in ('bleed_lock', 'journal_window', 'session_day', 'post_cutover', 'flicker', 'gui_signals', 'alerts')}, indent=2, default=str)}\n```",
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
    # Safety-critical: BLEED/SESSION_KILL while unlocked → force pause+lock (never /api/start).
    # If locks present but a port is not paused, reassert stop+locks. Never remove locks.
    cfd_paused_now = ((payload.get("ports") or {}).get("cfd") or {}).get("trading_paused")
    sb_paused_now = ((payload.get("ports") or {}).get("sb") or {}).get("trading_paused")
    need_reassert_pause = bool(payload.get("halted")) and (
        cfd_paused_now is not True or sb_paused_now is not True
    )
    if payload.get("ensure_bleed_halt") or need_reassert_pause:
        from runtime.gui_desk_supervisor_heal import ensure_operator_bleed_halt

        if not heal_dry_run:
            halt_res = ensure_operator_bleed_halt(
                ports=[
                    int(((payload.get("ports") or {}).get("cfd") or {}).get("port") or 8080),
                    int(((payload.get("ports") or {}).get("sb") or {}).get("port") or 8081),
                ],
                dry_run=False,
                reason="operator_halt_unacceptable_bleed",
                detail={
                    "alerts": payload.get("alerts"),
                    "score": payload.get("score"),
                    "ensure_bleed_halt": payload.get("ensure_bleed_halt"),
                    "halted": payload.get("halted"),
                    "reassert_pause": need_reassert_pause,
                },
            )
            payload["bleed_halt"] = halt_res
        else:
            payload["bleed_halt"] = ensure_operator_bleed_halt(dry_run=True)

    if payload.get("halted") or payload.get("ensure_bleed_halt") or payload.get("bleed_halt"):
        payload["halted"] = True
        payload["score"] = "FAIL"
        alerts = list(payload.get("alerts") or [])
        for tag in ("HALTED", "BLEED"):
            if tag not in alerts:
                alerts.insert(0, tag)
        payload["alerts"] = alerts
        chip = payload.get("dashboard_chip") if isinstance(payload.get("dashboard_chip"), dict) else {}
        chip.update(
            {
                "visible": True,
                "score": "HALTED",
                "label": "SUPERVISOR HALTED · BLEED LOCK",
                "summary": chip.get("summary") or " · ".join(alerts),
                "tone": "red",
                "alerts": alerts,
                "needs_ops": True,
            }
        )
        payload["dashboard_chip"] = chip
        payload["needs_ops"] = True

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
