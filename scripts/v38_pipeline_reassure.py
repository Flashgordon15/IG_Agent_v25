#!/usr/bin/env python3
"""V38 daytime pipeline reassurance probe — observe-only.

Polls :8080 (CFD / QUANT_SNIPER) and :8081 (SB / MACRO_SENTINEL) for ~60–120s.
Never restarts agents, places orders, or changes gates.

Usage:
  PYTHONPATH=src python3 scripts/v38_pipeline_reassure.py
  PYTHONPATH=src python3 scripts/v38_pipeline_reassure.py --seconds 90 --interval 10
  PYTHONPATH=src python3 scripts/v38_pipeline_reassure.py --samples 8 --interval 12

Verdicts: PIPELINE_OK | WARMING | SELECTIVE_QUIET | STUCK
Explicit: Edge/£1k NOT evaluated.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import time
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "src" / "data" / "v31-production"
REPORT_DIR = DATA / "reports"
STATE_DIR = DATA / "state"
LOG_DIR = DATA / "logs"
CONFIG_PRIMARY = ROOT / "config" / "config_v31_demo_throughput.json"
CONFIG_OVERLAY = ROOT / "config" / "tuning_overlay.json"
TZ = ZoneInfo("Europe/London")

PORTS = (
    {"port": 8080, "label": "CFD", "engine": "QUANT_SNIPER", "account": "Z6BAH4"},
    {"port": 8081, "label": "SB", "engine": "MACRO_SENTINEL", "account": "Z6BAH3"},
)

FOCUS_EPICS = (
    "IX.D.DOW.IFM.IP",
    "CS.D.CFPGOLD.CFP.IP",
    "CS.D.EURUSD.CFD.IP",
    "IX.D.FTSE.IFM.IP",
    "IX.D.NIKKEI.IFM.IP",
)

NIKKEI = "IX.D.NIKKEI.IFM.IP"
RANKED_EXPECTED = (
    "IX.D.DOW.IFM.IP",
    "CS.D.CFPGOLD.CFP.IP",
    "CS.D.EURUSD.CFD.IP",
    "IX.D.FTSE.IFM.IP",
)

REJECT_KEYS = (
    "hot_path_epic_excluded",
    "sb_core_b_micro_hard_disabled",
    "sb_instant_micro_hard_disabled",
    "api_trading_paused",
    "regime_veto",
    "obi_unavailable",
    "PATH_A",
    "execute_entry",
    "placeOrder",
    "ORDER_OPEN",
)


def _now() -> datetime:
    return datetime.now(TZ)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).isoformat()


def _http_json(url: str, timeout: float = 5.0) -> dict[str, Any]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "v38_pipeline_reassure"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8", errors="replace"))
            return raw if isinstance(raw, dict) else {"ok": False, "payload": raw}
    except Exception as exc:
        return {"_down": True, "ok": False, "error": str(exc)}


def _finite(x: Any) -> bool:
    try:
        v = float(x)
        return math.isfinite(v)
    except Exception:
        return False


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _deep_get(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _read_configured_thresholds() -> dict[str, Any]:
    """Print thresholds from live config/overlay — never invent gate numbers."""
    primary = _load_json(CONFIG_PRIMARY)
    overlay = _load_json(CONFIG_OVERLAY)

    def pick(*path: str) -> Any:
        ov = _deep_get(overlay, *path)
        if ov is not None:
            return ov, "tuning_overlay.json"
        pr = _deep_get(primary, *path)
        if pr is not None:
            return pr, "config_v31_demo_throughput.json"
        return None, None

    dual = {}
    for key in (
        "ranked_rotator_mode",
        "exclude_from_hot_path",
        "ranked_candidate_epics",
        "sb_hot_path_allowlist",
        "multi_source_auto_rotation",
        "sb_disable_instant_micro",
        "sb_disable_core_b_micro",
        "sb_macro_ltr_entries_only",
    ):
        val, src = pick("dual_core", key)
        if val is None and key.startswith("sb_"):
            val, src = pick("dual_regime", key)
        if val is not None:
            dual[key] = {"value": val, "source": src}

    # dual_regime may hold Instant-micro flags when not under dual_core
    for key in (
        "sb_disable_instant_micro",
        "sb_disable_core_b_micro",
        "sb_macro_ltr_entries_only",
        "sb_macro_path_a_carve_active",
        "sb_forbid_obi_velocity_scalp",
    ):
        if key in dual:
            continue
        val, src = pick("dual_regime", key)
        if val is not None:
            dual[key] = {"value": val, "source": src}

    elastic = {}
    for key in (
        "enabled",
        "healthy_p_lo",
        "healthy_p_hi",
        "stressed_p_lo",
        "stressed_p_hi",
        "healthy_obi_abs",
        "thin_obi_abs",
        "tight_spread_elasticity",
        "wide_spread_elasticity",
    ):
        val, src = pick("elastic_gate", key)
        if val is not None:
            elastic[key] = {"value": val, "source": src}

    selectivity = {}
    for key in ("min_ml_p_success", "min_abs_obi", "require_15m_trend_ml_obi", "elastic_gate_enabled"):
        val, src = pick("selectivity_gates", key)
        if val is not None:
            selectivity[key] = {"value": val, "source": src}

    profit = {}
    for key in ("min_ml_probability", "marginal_ml_veto"):
        val, src = pick("profit_philosophy", key)
        if val is not None:
            profit[key] = {"value": val, "source": src}

    return {
        "dual_core_or_regime": dual,
        "elastic_gate": elastic,
        "selectivity_gates": selectivity,
        "profit_philosophy": profit,
        "config_files": [
            str(CONFIG_PRIMARY.relative_to(ROOT)),
            str(CONFIG_OVERLAY.relative_to(ROOT)),
        ],
    }


def _a2_markers() -> dict[str, Any]:
    cfd = _load_json(DATA / "state_cfd" / "a2_entries_paused.json")
    sb = _load_json(DATA / "state_sb" / "a2_and_sb_entries_paused.json")
    return {
        "cfd_marker_active": bool(cfd.get("active")),
        "cfd_mode": cfd.get("mode"),
        "sb_marker_active": bool(sb.get("active")),
        "sb_mode": sb.get("mode"),
        "cfd_reason": cfd.get("reason"),
        "sb_reason": sb.get("reason"),
    }


def _gui_supervisor_latest() -> dict[str, Any]:
    path = STATE_DIR / "gui_supervisor_latest.json"
    raw = _load_json(path)
    if not raw:
        return {"present": False, "path": str(path)}
    handoff = raw.get("cursor_handoff") if isinstance(raw.get("cursor_handoff"), dict) else {}
    top = handoff.get("top_finding") if isinstance(handoff.get("top_finding"), dict) else {}
    findings = raw.get("findings") if isinstance(raw.get("findings"), list) else []
    if not top and findings:
        top = findings[0] if isinstance(findings[0], dict) else {}
    return {
        "present": True,
        "path": str(path),
        "score": raw.get("score") or handoff.get("score"),
        "checked_at": raw.get("checked_at"),
        "needs_code": raw.get("needs_code"),
        "needs_ops": raw.get("needs_ops"),
        "top_finding": {
            "title": top.get("title") or handoff.get("symptom"),
            "detail": top.get("detail") or handoff.get("detail"),
            "severity": top.get("severity"),
            "class": top.get("class"),
        },
        "area_grades": raw.get("area_grades") if isinstance(raw.get("area_grades"), dict) else {},
    }


def _gate_funnel() -> dict[str, Any]:
    for path in (DATA / "gate_funnel_report.json", REPORT_DIR / "gate_funnel_report.json"):
        raw = _load_json(path)
        if raw:
            return {"path": str(path), **raw}
    return {"path": None, "present": False}


def _tail_reject_counts(*, max_bytes: int = 900_000) -> dict[str, Any]:
    """Cheap log-derived veto/activity counters — no invented thresholds."""
    files = ("v32_sb.log", "v32_cfd.log", "demo_execution_trace.log", "engine.log")
    by_file: dict[str, dict[str, int]] = {}
    totals: Counter[str] = Counter()
    for name in files:
        path = LOG_DIR / name
        if not path.is_file() or path.stat().st_size == 0:
            by_file[name] = {}
            continue
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - max_bytes))
            text = fh.read().decode("utf-8", errors="replace")
        c: Counter[str] = Counter()
        for key in REJECT_KEYS:
            c[key] = len(re.findall(re.escape(key), text, flags=re.I))
        # generic approve/reject words (informational only)
        c["approve_word"] = len(re.findall(r"\bapprove(?:d|s)?\b", text, flags=re.I))
        c["reject_word"] = len(re.findall(r"\breject(?:ed|s|ion)?\b", text, flags=re.I))
        by_file[name] = dict(c)
        totals.update(c)
    return {"by_file": by_file, "totals": dict(totals), "note": "tail scan only; not a full-day funnel"}


def _sample_port(port: int) -> dict[str, Any]:
    base = f"http://127.0.0.1:{port}"
    health = _http_json(f"{base}/api/health")
    rotation = _http_json(f"{base}/api/rotation_state")
    state = _http_json(f"{base}/api/state")
    signals = _http_json(f"{base}/api/signals")
    positions = _http_json(f"{base}/api/positions/live")
    liveness = _http_json(f"{base}/api/trading_desk/liveness")
    fulfillment = _http_json(f"{base}/api/unified/fulfillment", timeout=8.0)

    loops = {}
    if isinstance(health, dict) and not health.get("_down"):
        bm = health.get("boot_metrics") if isinstance(health.get("boot_metrics"), dict) else {}
        ss = bm.get("system_state") if isinstance(bm.get("system_state"), dict) else {}
        loops = (
            ss.get("loops")
            or bm.get("loops")
            or health.get("loops")
            or {}
        )
        if not isinstance(loops, dict):
            loops = {}

    rot = rotation.get("rotation") if isinstance(rotation.get("rotation"), dict) else {}
    ranked = rot.get("ranked_rotator") if isinstance(rot.get("ranked_rotator"), dict) else {}

    mq = fulfillment.get("market_quotes") if isinstance(fulfillment.get("market_quotes"), dict) else {}
    quote_sanity: dict[str, Any] = {}
    for epic in FOCUS_EPICS:
        q = mq.get(epic) if isinstance(mq.get(epic), dict) else {}
        bid, offer, mid = q.get("bid"), q.get("offer"), q.get("mid")
        if mid is None and _finite(bid) and _finite(offer):
            mid = (float(bid) + float(offer)) / 2.0
        ok = _finite(bid) and _finite(offer) and _finite(mid)
        quote_sanity[epic] = {
            "ok": ok,
            "bid": bid,
            "offer": offer,
            "mid": mid,
            "fresh": (health.get("quote_fresh_by_epic") or {}).get(epic)
            if isinstance(health.get("quote_fresh_by_epic"), dict)
            else None,
        }

    sig_list = signals.get("signals") if isinstance(signals.get("signals"), list) else []
    gate_prog = state.get("gate_progression") if isinstance(state.get("gate_progression"), dict) else {}
    boot = health.get("boot_metrics") if isinstance(health.get("boot_metrics"), dict) else {}

    return {
        "ts": time.time(),
        "port": port,
        "reachable": not bool(health.get("_down")),
        "pid": health.get("agent_pid") or health.get("pid"),
        "session_id": health.get("session_id"),
        "session_status": health.get("session_status"),
        "trading_paused": health.get("trading_paused"),
        "trade_ready": health.get("trade_ready"),
        "issues": health.get("issues") if isinstance(health.get("issues"), list) else [],
        "block_reason": health.get("block_reason"),
        "accepting_ticks": loops.get("accepting_ticks"),
        "loops_built": loops.get("built"),
        "loops_running": loops.get("running") or health.get("trading_loops_running"),
        "quotes_fresh_count": health.get("quotes_fresh_count"),
        "quotes_total": health.get("quotes_total"),
        "boot_ready": boot.get("ready"),
        "boot_label": boot.get("label") or gate_prog.get("label"),
        "boot_percent": boot.get("percent") or gate_prog.get("percent"),
        "warm_up_complete": gate_prog.get("warm_up_complete"),
        "operational_ready": gate_prog.get("operational_ready"),
        "ml_confidence": state.get("ml_confidence"),
        "signal_strength": state.get("signal_strength"),
        "signal_threshold": state.get("signal_threshold") or state.get("config_signal_threshold"),
        "agent_state": state.get("agent_state"),
        "stream_status": state.get("stream_status"),
        "bid": state.get("bid"),
        "offer": state.get("offer"),
        "signals_n": len(sig_list),
        "positions_verdict": positions.get("verdict"),
        "positions_count": positions.get("count"),
        "positions_critical": positions.get("critical"),
        "liveness_ok": liveness.get("ok"),
        "rotation_sweep_count": rot.get("rotation_sweep_count"),
        "multi_source_auto_rotation": rot.get("multi_source_auto_rotation"),
        "active_stack_epics": [
            (x.get("epic") if isinstance(x, dict) else x)
            for x in (rot.get("active_instruments") or rot.get("active_stack_epics") or [])
        ],
        "ranked": {
            "active": ranked.get("active"),
            "mode": ranked.get("mode"),
            "dominant": ranked.get("dominant"),
            "promoted": ranked.get("promoted") if isinstance(ranked.get("promoted"), list) else [],
            "reason": ranked.get("reason"),
        },
        "quote_sanity": quote_sanity,
        "fulfillment_all_ready": fulfillment.get("all_ready"),
        "errors": {
            k: v.get("error")
            for k, v in {
                "health": health,
                "rotation": rotation,
                "state": state,
                "fulfillment": fulfillment,
            }.items()
            if isinstance(v, dict) and v.get("_down")
        },
    }


def _aggregate(samples: list[dict[str, Any]]) -> dict[str, Any]:
    if not samples:
        return {}
    last = samples[-1]
    sweep_vals = [s.get("rotation_sweep_count") for s in samples if s.get("rotation_sweep_count") is not None]
    ml_vals = [s.get("ml_confidence") for s in samples if s.get("ml_confidence") is not None]
    sig_vals = [s.get("signal_strength") for s in samples if s.get("signal_strength") is not None]
    reachable_n = sum(1 for s in samples if s.get("reachable"))
    ticks_n = sum(1 for s in samples if s.get("accepting_ticks") is True)
    paused_vals = [s.get("trading_paused") for s in samples]

    # quote ok across samples (last sample focus)
    qs = last.get("quote_sanity") if isinstance(last.get("quote_sanity"), dict) else {}
    focus_ok = all(bool((qs.get(e) or {}).get("ok")) for e in FOCUS_EPICS if e != NIKKEI)  # Nikkei may still quote
    nikkei_ok = bool((qs.get(NIKKEI) or {}).get("ok"))
    nikkei_in_hot = False
    ranked = last.get("ranked") if isinstance(last.get("ranked"), dict) else {}
    promoted = ranked.get("promoted") or []
    if isinstance(promoted, list) and NIKKEI in promoted:
        nikkei_in_hot = True
    # also check if Nikkei is sole/dominant hot path (config exclude is the SoT)
    gold_eur_ftse = {
        "gold_present": any("CFPGOLD" in str(x) for x in promoted)
        or any("CFPGOLD" in str(x) for x in (last.get("active_stack_epics") or [])),
        "eur_present": any("EURUSD" in str(x) for x in promoted)
        or any("EURUSD" in str(x) for x in (last.get("active_stack_epics") or [])),
        "ftse_present": any("FTSE" in str(x) for x in promoted)
        or any("FTSE" in str(x) for x in (last.get("active_stack_epics") or [])),
        # presence in ranked candidate universe / rows preferred
    }
    # ranked rows presence from last sample reason/promoted + expected list
    gold_eur_ftse["gold_in_ranked_universe"] = "CS.D.CFPGOLD.CFP.IP" in RANKED_EXPECTED
    gold_eur_ftse["eur_in_ranked_universe"] = "CS.D.EURUSD.CFD.IP" in RANKED_EXPECTED
    gold_eur_ftse["ftse_in_ranked_universe"] = "IX.D.FTSE.IFM.IP" in RANKED_EXPECTED

    return {
        "samples": len(samples),
        "reachable_ratio": reachable_n / max(1, len(samples)),
        "accepting_ticks_ratio": ticks_n / max(1, len(samples)),
        "last": last,
        "sweep_first": sweep_vals[0] if sweep_vals else None,
        "sweep_last": sweep_vals[-1] if sweep_vals else None,
        "sweep_advanced": (sweep_vals[-1] > sweep_vals[0]) if len(sweep_vals) >= 2 else None,
        "ml_max": max(ml_vals) if ml_vals else None,
        "ml_last": ml_vals[-1] if ml_vals else None,
        "signal_max": max(sig_vals) if sig_vals else None,
        "signal_last": sig_vals[-1] if sig_vals else None,
        "paused_stable": paused_vals[-1] if paused_vals else None,
        "focus_quotes_ok": focus_ok,
        "nikkei_quote_ok": nikkei_ok,
        "nikkei_in_promoted": nikkei_in_hot,
        "gold_eur_ftse": gold_eur_ftse,
        "ranked_mode_ok": str(ranked.get("mode") or "").lower() == "ranked" and ranked.get("active") is True,
    }


def _posture_checks(cfg: dict[str, Any], agg_by_port: dict[int, dict[str, Any]]) -> dict[str, Any]:
    dual = cfg.get("dual_core_or_regime") or {}

    def val(key: str) -> Any:
        node = dual.get(key) or {}
        return node.get("value")

    exclude = val("exclude_from_hot_path") or []
    nikkei_excluded = NIKKEI in exclude if isinstance(exclude, list) else False
    instant_off = val("sb_disable_instant_micro") is True
    micro_off = val("sb_disable_core_b_micro") is True
    ranked_cfg = val("ranked_rotator_mode") is True
    candidates = val("ranked_candidate_epics") or []
    gold_eur_ftse_cfg = all(
        e in candidates
        for e in ("CS.D.CFPGOLD.CFP.IP", "CS.D.EURUSD.CFD.IP", "IX.D.FTSE.IFM.IP")
    ) if isinstance(candidates, list) else False

    cfd = agg_by_port.get(8080, {}).get("last") or {}
    sb = agg_by_port.get(8081, {}).get("last") or {}
    a2 = _a2_markers()

    return {
        "a2_cfd_pause": {
            "expected": True,
            "marker_active": a2.get("cfd_marker_active"),
            "live_trading_paused": cfd.get("trading_paused") is True,
            "ok": a2.get("cfd_marker_active") and cfd.get("trading_paused") is True,
        },
        "sb_armed": {
            "expected": True,
            "trading_paused": sb.get("trading_paused"),
            "ok": sb.get("trading_paused") is False,
        },
        "instant_micro_off": {
            "sb_disable_instant_micro": instant_off,
            "sb_disable_core_b_micro": micro_off,
            "ok": instant_off and micro_off,
        },
        "ranked_rotator_cfg": {"ranked_rotator_mode": ranked_cfg, "ok": ranked_cfg},
        "nikkei_excluded": {"in_exclude_from_hot_path": nikkei_excluded, "ok": nikkei_excluded},
        "gold_eur_ftse_in_ranked_candidates": {"ok": gold_eur_ftse_cfg, "candidates": candidates},
        "sb_ranked_live": {
            "ok": bool(agg_by_port.get(8081, {}).get("ranked_mode_ok")),
            "ranked": (sb.get("ranked") or {}),
        },
        "nikkei_not_promoted": {
            "ok": not bool(agg_by_port.get(8081, {}).get("nikkei_in_promoted")),
            "promoted": ((sb.get("ranked") or {}).get("promoted") or []),
        },
    }


def _verdict(
    *,
    agg_by_port: dict[int, dict[str, Any]],
    posture: dict[str, Any],
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    cfd = agg_by_port.get(8080, {})
    sb = agg_by_port.get(8081, {})
    cfd_last = cfd.get("last") or {}
    sb_last = sb.get("last") or {}

    both_down = (cfd.get("reachable_ratio") or 0) < 0.5 and (sb.get("reachable_ratio") or 0) < 0.5
    sb_down = (sb.get("reachable_ratio") or 0) < 0.5
    sb_ticks = (sb.get("accepting_ticks_ratio") or 0) < 0.5
    quotes_bad = sb.get("focus_quotes_ok") is False
    posture_broke = any(
        (posture.get(k) or {}).get("ok") is False
        for k in (
            "a2_cfd_pause",
            "sb_armed",
            "instant_micro_off",
            "ranked_rotator_cfg",
            "nikkei_excluded",
            "sb_ranked_live",
        )
    )

    ml_last = sb.get("ml_last")
    sig_last = sb.get("signal_last")
    ml_zero = ml_last is not None and float(ml_last) == 0.0
    sig_zero = sig_last is not None and float(sig_last) == 0.0
    boot_ready = sb_last.get("boot_ready") is True
    warm_label = str(sb_last.get("boot_label") or "")
    still_compiling = ("Compiling" in warm_label) or (sb_last.get("boot_ready") is False)

    if both_down or sb_down:
        reasons.append("SB (or both) API unreachable during poll window")
        return "STUCK", reasons
    if sb_ticks:
        reasons.append("SB accepting_ticks false for majority of samples")
        return "STUCK", reasons
    if quotes_bad:
        reasons.append("Non-finite mids/bids/offers on focus epics (DOW/Gold/EUR/FTSE)")
        return "STUCK", reasons
    if posture.get("a2_cfd_pause", {}).get("ok") is False:
        reasons.append("A2 CFD pause not intact (marker or live trading_paused mismatch)")
        return "STUCK", reasons
    if posture.get("sb_armed", {}).get("ok") is False:
        reasons.append("SB unexpectedly trading_paused=true")
        return "STUCK", reasons
    if posture.get("instant_micro_off", {}).get("ok") is False:
        reasons.append("Instant-micro / core-B micro config no longer hard-off")
        return "STUCK", reasons
    if posture.get("ranked_rotator_cfg", {}).get("ok") is False:
        reasons.append("ranked_rotator_mode missing from config")
        return "STUCK", reasons
    if posture.get("sb_ranked_live", {}).get("ok") is False:
        reasons.append("SB ranked_rotator not active/mode=ranked in rotation_state")
        # Not always STUCK — can be WARMING/SELECTIVE if everything else ok; treat as selective quiet signal
        # but user cares about ranked — flag as STUCK only if also no ticks/sweep and cold forever.
        if sb.get("sweep_advanced") is False and ml_zero and sig_zero and boot_ready:
            reasons.append("ranked inactive + sweep frozen + confidence glued at 0 after boot_ready")
            return "STUCK", reasons

    if still_compiling or (ml_zero and sig_zero and not boot_ready):
        reasons.append(
            f"Signal/ML plane cold (ml={ml_last}, signal={sig_last}); "
            f"boot_ready={boot_ready}; label={warm_label!r}"
        )
        return "WARMING", reasons

    ticks_ok = (sb.get("accepting_ticks_ratio") or 0) >= 0.8
    ranked_ok = posture.get("sb_ranked_live", {}).get("ok") is True

    if ml_zero and sig_zero and still_compiling:
        reasons.append("Still compiling vectors with ml/signal at 0")
        return "WARMING", reasons

    if ml_zero and sig_zero and ticks_ok and ranked_ok:
        # Confidence glued at 0 while loops healthy — late warm, not stuck restart.
        reasons.append(
            "ml_confidence/signal_strength still 0 with healthy ticks + ranked active "
            f"(boot_ready={boot_ready}, label={warm_label!r}) — WARMING, not STUCK"
        )
        return "WARMING", reasons

    # Healthy pipeline, intentional selectivity (micro hard-off + A2 CFD pause)
    if (
        ticks_ok
        and posture.get("instant_micro_off", {}).get("ok")
        and posture.get("a2_cfd_pause", {}).get("ok")
        and (sb_last.get("signals_n") or 0) == 0
        and (float(sig_last or 0) < float(sb_last.get("signal_threshold") or 55))
    ):
        reasons.append("Desks alive; SB armed; no SETUP yet under Path-A-only + Instant-micro OFF")
        return "SELECTIVE_QUIET", reasons

    if ticks_ok and cfd_last.get("reachable") and sb_last.get("reachable"):
        reasons.append("Both agents reachable, ticks accepted, posture preserved")
        if posture_broke:
            reasons.append("some posture checks soft-failed — see posture section")
        return "PIPELINE_OK", reasons

    reasons.append("Ambiguous — defaulting to SELECTIVE_QUIET (alive but not clearly warm/stuck)")
    return "SELECTIVE_QUIET", reasons


def _md_report(payload: dict[str, Any]) -> str:
    v = payload["verdict"]
    reasons = payload.get("verdict_reasons") or []
    posture = payload.get("posture") or {}
    gui = payload.get("gui_supervisor") or {}
    cfg = payload.get("configured_thresholds") or {}
    agg = payload.get("aggregates") or {}
    a2 = payload.get("a2_markers") or {}
    funnel = payload.get("gate_funnel") or {}
    logs = payload.get("log_tail_counts") or {}

    lines: list[str] = []
    lines.append(f"# V38 Pipeline Reassure — {payload.get('day')}")
    lines.append("")
    lines.append(f"- Generated: `{payload.get('generated_at')}`")
    lines.append(f"- Window: `{payload.get('window_sec')}s` · samples=`{payload.get('samples_requested')}` · interval=`{payload.get('interval_sec')}s`")
    lines.append(f"- Mode: **READ-ONLY** (no restarts, no orders, no gate changes)")
    lines.append("- Explicit: **Edge/£1k NOT evaluated.**")
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(f"**{v}**")
    lines.append("")
    for r in reasons:
        lines.append(f"- {r}")
    lines.append("")
    lines.append("## Pre-dev audit (read-only)")
    lines.append("")
    lines.append("| Check | Result |")
    lines.append("|---|---|")
    flat = True
    for port, a in agg.items():
        last = (a or {}).get("last") or {}
        if (last.get("positions_count") or 0) > 0 or str(last.get("positions_verdict") or "").upper() not in ("FLAT", "", "NONE"):
            if last.get("positions_verdict") and str(last.get("positions_verdict")).upper() != "FLAT":
                flat = False
    lines.append(f"| Market / book | {'FLAT both ports' if flat else 'CHECK positions'} |")
    lines.append("| Watchdog hold | Not engaged (observe-only probe) |")
    lines.append("| Active PIDs | Left alone — see desk table |")
    lines.append("")
    lines.append("## Desk snapshot (last sample)")
    lines.append("")
    lines.append("| Port | Role | PID | trading_paused | accepting_ticks | trade_ready | ml / signal | ranked |")
    lines.append("|---|---|---:|---|---|---|---|---|")
    for meta in PORTS:
        port = meta["port"]
        last = (agg.get(str(port)) or agg.get(port) or {}).get("last") or {}
        # JSON keys may be str
        if not last:
            last = (agg.get(str(port)) or {}).get("last") or {}
        ranked = last.get("ranked") or {}
        lines.append(
            f"| :{port} | {meta['label']} {meta['engine']} | {last.get('pid')} | "
            f"**{last.get('trading_paused')}** | {last.get('accepting_ticks')} | {last.get('trade_ready')} | "
            f"{last.get('ml_confidence')} / {last.get('signal_strength')} | "
            f"active={ranked.get('active')} mode={ranked.get('mode')} dominant={ranked.get('dominant')} |"
        )
    lines.append("")
    lines.append("### Ranked / hot-path posture")
    lines.append("")
    sb_last = ((agg.get("8081") or agg.get(8081) or {}).get("last") or {})
    ranked = sb_last.get("ranked") or {}
    lines.append(f"- SB ranked: `active={ranked.get('active')}` `mode={ranked.get('mode')}`")
    lines.append(f"- Dominant: `{ranked.get('dominant')}`")
    lines.append(f"- Promoted: `{ranked.get('promoted')}`")
    lines.append(f"- Reason: `{ranked.get('reason')}`")
    lines.append(f"- A2 CFD marker: active=`{a2.get('cfd_marker_active')}` mode=`{a2.get('cfd_mode')}`")
    lines.append(f"- SB marker: active=`{a2.get('sb_marker_active')}` mode=`{a2.get('sb_mode')}`")
    lines.append("")
    lines.append("| Posture check | OK | Detail |")
    lines.append("|---|---|---|")
    for key, node in posture.items():
        if not isinstance(node, dict):
            continue
        lines.append(f"| `{key}` | **{node.get('ok')}** | `{json.dumps({k: v for k, v in node.items() if k != 'ok'}, default=str)[:180]}` |")
    lines.append("")
    lines.append("## Quotes (FE NaN proxy via `/api/unified/fulfillment`)")
    lines.append("")
    lines.append("WebGL NaN guard is UI-side; this probe only checks finite bid/offer/mid on focus epics.")
    lines.append("")
    lines.append("| Epic | mid | bid | offer | ok | fresh |")
    lines.append("|---|---:|---:|---:|---|---|")
    qs = sb_last.get("quote_sanity") or {}
    for epic in FOCUS_EPICS:
        q = qs.get(epic) or {}
        lines.append(
            f"| `{epic}` | {q.get('mid')} | {q.get('bid')} | {q.get('offer')} | {q.get('ok')} | {q.get('fresh')} |"
        )
    lines.append("")
    lines.append("## Configured thresholds (from disk — not invented)")
    lines.append("")
    for section in ("elastic_gate", "selectivity_gates", "profit_philosophy"):
        block = cfg.get(section) or {}
        if not block:
            continue
        lines.append(f"### {section}")
        lines.append("")
        for k, node in block.items():
            lines.append(f"- `{k}` = `{node.get('value')}` (source: `{node.get('source')}`)")
        lines.append("")
    dual = cfg.get("dual_core_or_regime") or {}
    if dual:
        lines.append("### dual_core / dual_regime flags")
        lines.append("")
        for k, node in dual.items():
            lines.append(f"- `{k}` = `{node.get('value')}` (source: `{node.get('source')}`)")
        lines.append("")
    lines.append("## Gate / funnel")
    lines.append("")
    lines.append(
        f"- gate_funnel_report: present=`{bool(funnel.get('path'))}` "
        f"updated_at=`{funnel.get('updated_at')}` "
        f"total_ticks=`{funnel.get('total_ticks')}` "
        f"all_passed=`{funnel.get('all_passed_ticks')}` "
        f"first_block_counts=`{funnel.get('first_block_counts')}`"
    )
    totals = (logs.get("totals") or {}) if isinstance(logs, dict) else {}
    lines.append(f"- Log tail counters (informational): `{json.dumps(totals, default=str)}`")
    lines.append("- Note: empty funnel + cold signal plane ≠ invented OBI reject storm.")
    lines.append("")
    lines.append("## GUI supervisor")
    lines.append("")
    lines.append(f"- Score: **{gui.get('score')}** (checked_at=`{gui.get('checked_at')}`)")
    top = gui.get("top_finding") or {}
    lines.append(f"- Top finding: `{top.get('title')}` — {top.get('detail')}")
    grades = gui.get("area_grades") or {}
    if grades:
        lines.append(f"- Area grades: `{json.dumps(grades, default=str)}`")
    lines.append("")
    lines.append("## Operator card")
    lines.append("")
    lines.append(payload.get("operator_card") or "")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("*Edge/£1k NOT evaluated. This is a liveness + posture reassurance probe only.*")
    lines.append("")
    return "\n".join(lines)


def _operator_card(verdict: str, payload: dict[str, Any]) -> str:
    agg = payload.get("aggregates") or {}
    # normalize keys
    def last(port: int) -> dict[str, Any]:
        a = agg.get(str(port)) or agg.get(port) or {}
        return a.get("last") or {}

    cfd, sb = last(8080), last(8081)
    gui = payload.get("gui_supervisor") or {}
    ranked = sb.get("ranked") or {}
    lines = [
        f"Verdict: **{verdict}** — Edge/£1k NOT evaluated.",
        f"CFD :8080 PID {cfd.get('pid')} paused={cfd.get('trading_paused')} (A2 expected) · "
        f"SB :8081 PID {sb.get('pid')} paused={sb.get('trading_paused')} accepting_ticks={sb.get('accepting_ticks')}.",
        f"Ranked (SB): active={ranked.get('active')} mode={ranked.get('mode')} "
        f"dominant={ranked.get('dominant')} promoted={ranked.get('promoted')}.",
        f"Signal plane: ml={sb.get('ml_confidence')} signal={sb.get('signal_strength')} "
        f"boot_ready={sb.get('boot_ready')} label={sb.get('boot_label')!r}.",
        f"Supervisor: {gui.get('score')} — {(gui.get('top_finding') or {}).get('title')}.",
        "Preserve: A2 CFD pause · SB Path A · Instant-micro OFF · ranked rotator · Nikkei excluded.",
        "Do not restart from this probe alone.",
    ]
    return "\n".join(f"- {x}" for x in lines)


def run(*, seconds: float, interval: float, samples: int | None) -> dict[str, Any]:
    if samples and samples > 0:
        n = samples
        interval = max(1.0, float(interval))
        window = n * interval
    else:
        window = max(30.0, float(seconds))
        interval = max(1.0, float(interval))
        n = max(3, int(round(window / interval)))

    started = _now()
    per_port: dict[int, list[dict[str, Any]]] = {p["port"]: [] for p in PORTS}
    print(f"[reassure] start {_iso(started)} samples={n} interval={interval}s (READ-ONLY)", flush=True)

    for i in range(n):
        t0 = time.time()
        for meta in PORTS:
            port = meta["port"]
            sample = _sample_port(port)
            per_port[port].append(sample)
            print(
                f"[reassure] #{i+1}/{n} :{port} pid={sample.get('pid')} "
                f"paused={sample.get('trading_paused')} ticks={sample.get('accepting_ticks')} "
                f"ml={sample.get('ml_confidence')} ranked={ (sample.get('ranked') or {}).get('mode')}/"
                f"{(sample.get('ranked') or {}).get('active')} "
                f"sweep={sample.get('rotation_sweep_count')}",
                flush=True,
            )
        if i < n - 1:
            elapsed = time.time() - t0
            time.sleep(max(0.0, interval - elapsed))

    cfg = _read_configured_thresholds()
    aggregates: dict[Any, dict[str, Any]] = {port: _aggregate(rows) for port, rows in per_port.items()}
    # also str keys for JSON stability in markdown
    aggregates_out = {str(k): v for k, v in aggregates.items()}
    posture = _posture_checks(cfg, aggregates)
    verdict, reasons = _verdict(agg_by_port=aggregates, posture=posture)
    gui = _gui_supervisor_latest()
    funnel = _gate_funnel()
    logs = _tail_reject_counts()
    a2 = _a2_markers()

    payload: dict[str, Any] = {
        "schema": "v38_pipeline_reassure/v1",
        "generated_at": _iso(),
        "day": started.strftime("%Y-%m-%d"),
        "window_sec": round(n * interval, 1),
        "interval_sec": interval,
        "samples_requested": n,
        "read_only": True,
        "edge_evaluated": False,
        "note": "Edge/£1k NOT evaluated.",
        "verdict": verdict,
        "verdict_reasons": reasons,
        "a2_markers": a2,
        "posture": posture,
        "configured_thresholds": cfg,
        "gui_supervisor": gui,
        "gate_funnel": funnel,
        "log_tail_counts": logs,
        "aggregates": aggregates_out,
        "samples_raw_last": {str(p): (rows[-1] if rows else {}) for p, rows in per_port.items()},
    }
    payload["operator_card"] = _operator_card(verdict, payload)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    day = payload["day"]
    md_path = REPORT_DIR / f"v38_pipeline_reassure_{day}.md"
    json_path = REPORT_DIR / f"v38_pipeline_reassure_{day}.json"
    md_path.write_text(_md_report(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    payload["report_md"] = str(md_path)
    payload["report_json"] = str(json_path)
    print(f"[reassure] verdict={verdict} -> {md_path}", flush=True)
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description="V38 daytime pipeline reassurance (read-only)")
    ap.add_argument("--seconds", type=float, default=90.0, help="Poll window seconds (default 90)")
    ap.add_argument("--interval", type=float, default=10.0, help="Seconds between samples")
    ap.add_argument("--samples", type=int, default=0, help="If >0, overrides --seconds as N samples")
    args = ap.parse_args()
    payload = run(
        seconds=args.seconds,
        interval=args.interval,
        samples=args.samples if args.samples > 0 else None,
    )
    print("\n=== OPERATOR CARD ===")
    print(payload.get("operator_card"))
    print(f"\nReport: {payload.get('report_md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
