#!/usr/bin/env python3
"""Daylight alpha forensic probe — dual desk DEMO (:8080 CFD / :8081 SB).

READ-ONLY. Does not restart ports, flatten, place orders, or mutate gates.

Usage::

  PYTHONPATH=src IG_AGENT_CONFIG=config/config_v31_demo_throughput.json APP_MODE=DEMO \\
    .venv/bin/python3 -u src/diagnostics/daylight_alpha_probe.py \\
      --ports 8080,8081 --candidates 100 --max-minutes 30
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "src/data/v31-production"
LOG_DIR = DATA / "logs"
REPORT_DIR = DATA / "reports"
METRICS = DATA / "metrics"
JOURNAL = METRICS / "daily_journal.csv"
ML_OUTCOMES = METRICS / "ml_trade_outcomes.jsonl"

TZ = ZoneInfo("Europe/London")

PORT_META = {
    8080: {"account": "Z6BAH4", "engine": "QUANT_SNIPER", "label": "CFD"},
    8081: {"account": "Z6BAH3", "engine": "MACRO_SENTINEL", "label": "SB"},
}

LOG_FILES = ("v32_cfd.log", "v32_sb.log", "engine.log", "ig_agent.log")

MATRIX_BLOCK_RE = re.compile(
    r"strategy matrix blocked epic=(?P<epic>\S+)\s+reason=(?P<reason>\S+)"
    r"(?:\s+(?P<details>.*))?$"
)
SUBMIT_RE = re.compile(
    r"(?:ORDER_SUBMITTED|DualCoreCoordinator: micro order confirm|"
    r"Lifecycle:.*ORDER_SUBMITTED)"
)
SNIPER_P_RE = re.compile(r"p=([0-9.]+)")
HTTP_429_RE = re.compile(r"HTTP 429|status.?429", re.I)

# Reasons we treat as structurally correct vs possible false negatives.
CORRECT_REJECT_PREFIXES = (
    "sniper_ml_chop_isolation",
    "overnight_cfd_new_entries_blocked",
    "overnight_sb_instant_micro_rejected",
    "overnight_sb_non_dow_rejected",
    "overnight_sb_path_not_long_runner",
    "ml_score_null_abort",
    "selectivity_p_fail",
    "selectivity_obi_fail",
    "selectivity_non_dow_rejected",
    "rest_pressure",
    "exclude_from_hot_path",
    "spread_hard",
)
FALSE_NEG_HINTS = (
    "features_unavailable_fail_open",
    "obi_velocity=0",
    "selectivity_trend_disagree",  # may be correct; tagged uncertain unless buy into bear
    "limit_chase_max_ticks_exceeded",
    "alpha_decay_kill",
)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------


def parse_ports(spec: str) -> list[int]:
    """Parse ``8080,8081`` → ``[8080, 8081]``."""
    out: list[int] = []
    for part in str(spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        out.append(int(part))
    if not out:
        raise ValueError("ports empty")
    return out


def percentile(samples: list[float], pct: float) -> float | None:
    """Nearest-rank percentile for ``pct`` in [0, 100]. Empty → None."""
    if not samples:
        return None
    if pct < 0 or pct > 100:
        raise ValueError("pct must be in [0, 100]")
    xs = sorted(float(x) for x in samples)
    if len(xs) == 1:
        return xs[0]
    # nearest-rank
    k = max(1, int(math.ceil((pct / 100.0) * len(xs))))
    return xs[min(k, len(xs)) - 1]


def latency_summary(
    samples_ms: list[float],
    *,
    method: str,
) -> dict[str, Any]:
    """Summarise tick→score latency samples with explicit measurement method."""
    clean = [float(x) for x in samples_ms if x is not None and math.isfinite(float(x))]
    p50 = percentile(clean, 50)
    p95 = percentile(clean, 95)
    p99 = percentile(clean, 99)
    under_20 = None
    if p99 is not None:
        under_20 = bool(p99 < 20.0)
    return {
        "n": len(clean),
        "p50_ms": p50,
        "p95_ms": p95,
        "p99_ms": p99,
        "max_ms": max(clean) if clean else None,
        "under_20ms_p99": under_20,
        "method": method,
        "compliance_claim": (
            "measured"
            if clean
            else "insufficient_samples — do NOT claim <20ms compliance"
        ),
    }


def classify_feature_degeneracy(features: dict[str, Any] | None) -> dict[str, Any]:
    """Flag non-informative / stuck feature planes."""
    feats = dict(features or {})
    flags: list[str] = []
    obi_v = feats.get("obi_velocity")
    elast = feats.get("spread_elasticity")
    atr_v = feats.get("atr_velocity")
    fail_open = bool(feats.get("features_unavailable_fail_open"))
    obi_unavail = bool(feats.get("obi_unavailable")) or str(
        feats.get("obi_source") or ""
    ) == "obi_unavailable"
    try:
        if obi_v is not None and abs(float(obi_v)) < 1e-12:
            flags.append("obi_velocity_zero")
    except (TypeError, ValueError):
        flags.append("obi_velocity_unreadable")
    try:
        if elast is not None and abs(float(elast) - 1.0) < 1e-9:
            flags.append("spread_elasticity_stuck_1")
    except (TypeError, ValueError):
        flags.append("spread_elasticity_unreadable")
    try:
        if atr_v is not None and abs(float(atr_v)) < 1e-12:
            flags.append("atr_velocity_zero")
    except (TypeError, ValueError):
        flags.append("atr_velocity_unreadable")
    if fail_open:
        flags.append("features_unavailable_fail_open")
    if obi_unavail:
        flags.append("obi_unavailable")
    degenerate = bool(flags) and (
        fail_open
        or obi_unavail
        or ("obi_velocity_zero" in flags and "spread_elasticity_stuck_1" in flags)
    )
    return {
        "degenerate": degenerate,
        "flags": flags,
        "obi_velocity": obi_v,
        "spread_elasticity": elast,
        "atr_velocity": atr_v,
        "tick_acceleration": feats.get("tick_acceleration"),
        "fail_open": fail_open,
        "obi_unavailable": obi_unavail,
    }


def parse_matrix_block_line(line: str) -> dict[str, Any] | None:
    """Extract epic/reason/p from a strategy-matrix block log line."""
    m = MATRIX_BLOCK_RE.search(str(line or "").strip())
    if not m:
        return None
    details = (m.group("details") or "").strip()
    blob = f"{m.group('reason')} {details}"
    p_m = SNIPER_P_RE.search(blob)
    p_val: float | None = float(p_m.group(1)) if p_m else None
    return {
        "epic": m.group("epic"),
        "reason": m.group("reason"),
        "details": details,
        "p_success": p_val,
        "source": "log_matrix_block",
    }


def classify_reject(
    reason: str,
    *,
    features: dict[str, Any] | None = None,
    approved: bool | None = None,
) -> str:
    """Return ``correct`` | ``false_negative`` | ``uncertain``."""
    r = str(reason or "")
    deg = classify_feature_degeneracy(features)
    if approved is True and not r:
        return "correct"  # pass-through
    if any(r.startswith(p) for p in CORRECT_REJECT_PREFIXES):
        # Chop isolation with degenerate features may be false negative.
        if r.startswith("sniper_ml_chop_isolation") and deg["degenerate"]:
            return "false_negative"
        return "correct"
    if deg.get("fail_open"):
        return "false_negative"
    if any(h in r for h in FALSE_NEG_HINTS):
        return "uncertain"
    if "REJECTED" in r.upper() or r.upper() == "UNKNOWN":
        return "uncertain"
    return "uncertain"


def aggregate_funnel(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Gate funnel reject counts by reason + correctness bucket."""
    by_reason: Counter[str] = Counter()
    by_correctness: Counter[str] = Counter()
    by_desk: Counter[str] = Counter()
    by_epic: Counter[str] = Counter()
    approved_n = 0
    rejected_n = 0
    null_p = 0
    finite_p = 0
    for c in candidates:
        reason = str(c.get("reject_reason") or c.get("reason") or "unknown")
        approved = bool(c.get("approved"))
        if approved:
            approved_n += 1
            by_reason["APPROVED"] += 1
        else:
            rejected_n += 1
            by_reason[reason] += 1
        verdict = str(c.get("reject_class") or classify_reject(reason, features=c.get("features"), approved=approved))
        by_correctness[verdict] += 1
        desk = str(c.get("desk") or c.get("label") or "?")
        by_desk[desk] += 1
        epic = str(c.get("epic") or "?")
        by_epic[epic] += 1
        p = c.get("p_success")
        if p is None or (isinstance(p, float) and not math.isfinite(p)):
            null_p += 1
        else:
            finite_p += 1
    return {
        "n": len(candidates),
        "approved": approved_n,
        "rejected": rejected_n,
        "by_reason": dict(by_reason.most_common()),
        "by_correctness": dict(by_correctness),
        "by_desk": dict(by_desk),
        "by_epic": dict(by_epic),
        "ml_finite_p": finite_p,
        "ml_null_p": null_p,
        "ml_finite_rate": (finite_p / len(candidates)) if candidates else None,
    }


def summarize_journal_rows(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Expectancy / WR / ML fill / exit mix for journal rows."""
    n = len(rows)
    wins = losses = flats = 0
    net = 0.0
    holds: list[float] = []
    ml_filled = 0
    ml_null = 0
    by_exit: Counter[str] = Counter()
    by_style: Counter[str] = Counter()
    by_acct: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            pnl = float(row.get("RealizedPnL_GBP") or row.get("pnl") or 0.0)
        except (TypeError, ValueError):
            pnl = 0.0
        net += pnl
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
        else:
            flats += 1
        ml = row.get("MlScoreAtEntry")
        if ml is None:
            ml = row.get("ml_score")
        if ml is None or str(ml).strip() in ("", "None", "null", "na"):
            ml_null += 1
        else:
            ml_filled += 1
        hs = row.get("HoldSec") or row.get("hold_sec")
        try:
            if hs is not None and str(hs).strip() != "":
                holds.append(float(hs))
        except (TypeError, ValueError):
            pass
        er = str(row.get("ExitReason") or row.get("exit_reason") or "?")
        by_exit[er] += 1
        st = str(row.get("Style") or row.get("style") or "?")
        by_style[st] += 1
        acct = str(row.get("AccountID") or row.get("account") or "?")
        bucket = by_acct.setdefault(acct, {"n": 0, "net": 0.0, "wins": 0, "losses": 0})
        bucket["n"] += 1
        bucket["net"] += pnl
        if pnl > 0:
            bucket["wins"] += 1
        elif pnl < 0:
            bucket["losses"] += 1
    decided = wins + losses
    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "net_gbp": round(net, 4),
        "wr": (wins / decided) if decided else None,
        "expectancy_gbp": (net / n) if n else None,
        "hold_sec_mean": (sum(holds) / len(holds)) if holds else None,
        "hold_sec_n": len(holds),
        "ml_score_fill_rate": (ml_filled / n) if n else None,
        "ml_filled": ml_filled,
        "ml_null": ml_null,
        "by_exit": dict(by_exit.most_common()),
        "by_style": dict(by_style.most_common()),
        "by_account": by_acct,
    }


def cro_verdict_from_evidence(ev: dict[str, Any]) -> str:
    """Map evidence bundle → enum CRO verdict string."""
    funnel = ev.get("funnel") or {}
    feats = ev.get("feature_health") or {}
    latency = ev.get("latency") or {}
    daytime = ev.get("daytime_pnl") or {}
    data = ev.get("data_plane") or {}

    finite_rate = funnel.get("ml_finite_rate")
    deg_rate = feats.get("degenerate_rate")
    rest_high = bool(data.get("rest_pressure_high"))
    feed_ok = bool(data.get("feeds_ok", True))
    net = daytime.get("net_gbp")
    n_trades = int(daytime.get("n") or 0)
    approved = int(funnel.get("approved") or 0)
    rejected = int(funnel.get("rejected") or 0)

    if finite_rate is not None and finite_rate < 0.05:
        return "BLIND"
    if not feed_ok or float(data.get("http_429_hits") or 0) > 200:
        return "DATA UNRELIABLE"
    if deg_rate is not None and deg_rate >= 0.6:
        return "WEAK"
    if rejected > 0 and approved == 0 and (finite_rate or 0) > 0.5:
        return "GATES OVER-FILTERING"
    if rest_high and approved == 0:
        return "GATES OVER-FILTERING"
    if n_trades >= 5 and net is not None and float(net) < -50:
        return "WEAK"
    if (
        (finite_rate or 0) >= 0.8
        and (deg_rate or 1) < 0.4
        and latency.get("under_20ms_p99") is True
        and feed_ok
    ):
        return "ALPHA FUNCTIONING"
    if (finite_rate or 0) >= 0.5 and approved >= 1:
        return "WEAK"
    return "WEAK"


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(TZ)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).isoformat()


def _git_head() -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=str(ROOT), stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )
    except Exception:
        return "unknown"


def _http_json(url: str, timeout: float = 5.0) -> dict[str, Any]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "daylight_alpha_probe"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return {"ok": False, "error": str(exc), "_down": True}


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def _load_journal_since(london_hour_start: int = 7) -> list[dict[str, Any]]:
    if not JOURNAL.exists():
        return []
    today = _now().date()
    out: list[dict[str, Any]] = []
    with JOURNAL.open(newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = row.get("Timestamp") or ""
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TZ)
            except Exception:
                continue
            if dt.date() != today:
                continue
            if dt.hour < london_hour_start:
                continue
            out.append(row)
    return out


def _tail_new_lines(path: Path, offset: int) -> tuple[list[str], int]:
    if not path.exists():
        return [], offset
    size = path.stat().st_size
    if offset > size:
        offset = 0
    with path.open("r", encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        data = f.read()
        new_off = f.tell()
    lines = data.splitlines()
    return lines, new_off


def _measure_local_score_latency(epics: list[str], repeats: int = 3) -> list[float]:
    """In-process sniper score latency (NOT end-to-end tick→order path)."""
    samples: list[float] = []
    try:
        from alpha.micro_sniper_ml import evaluate_live_sniper_probability
    except Exception:
        return samples
    for epic in epics:
        for _ in range(repeats):
            t0 = time.perf_counter()
            try:
                evaluate_live_sniper_probability(epic, "BUY", cfg=None, quote=None)
            except Exception:
                pass
            samples.append((time.perf_counter() - t0) * 1000.0)
    return samples


def _candidate_from_sniper_row(
    *,
    port: int,
    epic: str,
    row: dict[str, Any],
    gate_stack: dict[str, Any],
    source: str,
    score_latency_ms: float | None = None,
) -> dict[str, Any]:
    meta = PORT_META.get(port, {})
    feats = dict(row.get("features") or {})
    approved = bool(row.get("approved"))
    reason = str(row.get("reason") or ("approved" if approved else "rejected"))
    deg = classify_feature_degeneracy(feats)
    reject_class = classify_reject(reason, features=feats, approved=approved)
    trend = gate_stack.get("live_15min_macro_trend")
    return {
        "ts": _iso(),
        "ts_epoch": time.time(),
        "port": port,
        "desk": meta.get("label"),
        "account": meta.get("account"),
        "engine": meta.get("engine"),
        "epic": epic,
        "p_success": row.get("p_success"),
        "approved": approved,
        "threshold": row.get("threshold"),
        "reason": reason,
        "reject_reason": None if approved else reason,
        "reject_class": reject_class,
        "features": feats,
        "feature_degeneracy": deg,
        "trend_15m": trend,
        "gate_stack_all_clear": gate_stack.get("all_clear"),
        "score_latency_ms": score_latency_ms,
        "source": source,
    }


@dataclass
class ProbeState:
    candidates: list[dict[str, Any]] = field(default_factory=list)
    seen_keys: set[str] = field(default_factory=set)
    latency_ms: list[float] = field(default_factory=list)
    http_429: int = 0
    log_offsets: dict[str, int] = field(default_factory=dict)
    polls: int = 0
    backfill_used: bool = False
    notes: list[str] = field(default_factory=list)


def _add_candidate(state: ProbeState, cand: dict[str, Any], jsonl: Path) -> bool:
    """Dedup loosely by port/epic/reason/p/rounded-ts-bucket; always append unique live polls with source stamp."""
    # Allow repeated live polls — use source+poll counter in key for live API.
    src = str(cand.get("source") or "")
    if src.startswith("live_api"):
        key = f"live|{cand.get('port')}|{cand.get('epic')}|{cand.get('p_success')}|{cand.get('reason')}|{state.polls}|{len(state.candidates)}"
    else:
        key = f"{cand.get('port')}|{cand.get('epic')}|{cand.get('reason')}|{cand.get('p_success')}|{cand.get('details', '')}"
    if key in state.seen_keys:
        return False
    state.seen_keys.add(key)
    if cand.get("score_latency_ms") is not None:
        try:
            state.latency_ms.append(float(cand["score_latency_ms"]))
        except (TypeError, ValueError):
            pass
    state.candidates.append(cand)
    _append_jsonl(jsonl, cand)
    return True


def _scan_log_lines(
    state: ProbeState,
    lines: list[str],
    *,
    port_hint: int | None,
    jsonl: Path,
) -> int:
    added = 0
    for line in lines:
        if HTTP_429_RE.search(line):
            state.http_429 += 1
        parsed = parse_matrix_block_line(line)
        if parsed:
            port = port_hint or (8080 if "cfd" in line.lower() else 8081)
            # Prefer filename hint via port_hint
            meta = PORT_META.get(port, {})
            reason = parsed["reason"]
            feats: dict[str, Any] = {}
            cand = {
                "ts": _iso(),
                "ts_epoch": time.time(),
                "port": port,
                "desk": meta.get("label"),
                "account": meta.get("account"),
                "engine": meta.get("engine"),
                "epic": parsed["epic"],
                "p_success": parsed.get("p_success"),
                "approved": False,
                "threshold": None,
                "reason": reason,
                "reject_reason": reason,
                "reject_class": classify_reject(reason),
                "features": feats,
                "feature_degeneracy": classify_feature_degeneracy(feats),
                "details": parsed.get("details"),
                "source": "log_backfill" if state.backfill_used else "log_tail",
            }
            if _add_candidate(state, cand, jsonl):
                added += 1
            continue
        if SUBMIT_RE.search(line):
            # Count submit attempts as candidates (approved path evidence)
            port = port_hint or 8080
            meta = PORT_META.get(port, {})
            cand = {
                "ts": _iso(),
                "ts_epoch": time.time(),
                "port": port,
                "desk": meta.get("label"),
                "account": meta.get("account"),
                "engine": meta.get("engine"),
                "epic": "UNKNOWN",
                "p_success": None,
                "approved": True,
                "threshold": None,
                "reason": "order_submit_observed",
                "reject_reason": None,
                "reject_class": "correct",
                "features": {},
                "feature_degeneracy": classify_feature_degeneracy({}),
                "source": "log_submit",
                "raw_snip": line[-240:],
            }
            if _add_candidate(state, cand, jsonl):
                added += 1
    return added


def _backfill_logs(state: ProbeState, jsonl: Path, max_add: int = 80) -> int:
    state.backfill_used = True
    state.notes.append("log_backfill_engaged")
    added = 0
    for name in LOG_FILES:
        path = LOG_DIR / name
        if not path.exists():
            continue
        port_hint = 8080 if "cfd" in name else (8081 if "sb" in name else None)
        try:
            # Read last ~2MB only
            size = path.stat().st_size
            with path.open("rb") as f:
                if size > 2_000_000:
                    f.seek(size - 2_000_000)
                text = f.read().decode("utf-8", errors="replace")
            lines = text.splitlines()
            for line in lines:
                if added >= max_add:
                    return added
                before = len(state.candidates)
                _scan_log_lines(state, [line], port_hint=port_hint, jsonl=jsonl)
                if len(state.candidates) > before:
                    added += 1
        except Exception as exc:
            state.notes.append(f"backfill_err:{name}:{type(exc).__name__}")
    return added


def _poll_once(state: ProbeState, ports: list[int], jsonl: Path) -> dict[str, Any]:
    state.polls += 1
    snap: dict[str, Any] = {"ts": _iso(), "ports": {}}
    for port in ports:
        base = f"http://127.0.0.1:{port}"
        health = _http_json(f"{base}/api/health")
        sniper = _http_json(f"{base}/api/desk/sniper_ml")
        gates = _http_json(f"{base}/api/v31/gate-stack")
        feeds = _http_json(f"{base}/api/data_feed_state")
        pos = _http_json(f"{base}/api/positions/live")
        stab = _http_json(f"{base}/api/desk/stability")
        why = _http_json(f"{base}/api/desk/why_idle")
        # Local score latency for hot epics (measurement method labelled later)
        by_epic = dict(sniper.get("by_epic") or {})
        epics = list(by_epic.keys()) or ["IX.D.DOW.IFM.IP"]
        lat_samples = _measure_local_score_latency(epics[:3], repeats=1)
        for i, (epic, row) in enumerate(by_epic.items()):
            lat = lat_samples[i] if i < len(lat_samples) else (lat_samples[-1] if lat_samples else None)
            cand = _candidate_from_sniper_row(
                port=port,
                epic=epic,
                row=row if isinstance(row, dict) else {},
                gate_stack=gates if isinstance(gates, dict) else {},
                source="live_api_sniper_ml",
                score_latency_ms=lat,
            )
            # Selectivity overlay (config thresholds) — diagnostic only
            try:
                from runtime.overnight_entry_policy import evaluate_selectivity_gates

                obi = None
                feats = cand.get("features") or {}
                if feats.get("obi_velocity") is not None:
                    obi = feats.get("obi_velocity")
                sel = evaluate_selectivity_gates(
                    epic=epic,
                    direction=str((feats or {}).get("direction") or "BUY"),
                    p_success=cand.get("p_success"),
                    obi=obi,
                    trend_15m=cand.get("trend_15m"),
                    cfg=None,  # uses defaults; live cfg thresholds stamped below
                )
                # Re-evaluate with live overlay thresholds if importable
                try:
                    from system.config import get_config  # type: ignore

                    cfg = get_config()
                except Exception:
                    cfg = {
                        "selectivity_gates": {
                            "min_ml_p_success": 0.78,
                            "min_abs_obi": 0.25,
                            "require_15m_trend_ml_obi": True,
                            "allow_non_dow": False,
                        }
                    }
                sel = evaluate_selectivity_gates(
                    epic=epic,
                    direction=str((feats or {}).get("direction") or "BUY"),
                    p_success=cand.get("p_success"),
                    obi=obi if obi is not None else 0.0,
                    trend_15m=cand.get("trend_15m"),
                    cfg=cfg,
                )
                cand["selectivity"] = {
                    "allow": sel.allow,
                    "reason": sel.reason,
                    "abs_obi": sel.abs_obi,
                    "trend_ok": sel.trend_ok,
                }
                if cand["approved"] and not sel.allow:
                    cand["selectivity_would_block"] = True
                    cand["reject_class"] = classify_reject(
                        sel.reason, features=feats, approved=False
                    )
            except Exception as exc:
                cand["selectivity_error"] = type(exc).__name__
            _add_candidate(state, cand, jsonl)

        components = ((stab.get("desk_stability") or {}).get("components") or {})
        snap["ports"][str(port)] = {
            "health_ok": health.get("ok"),
            "trade_ready": health.get("trade_ready"),
            "pid": health.get("agent_pid") or health.get("pid"),
            "verdict": pos.get("verdict"),
            "broker_open": ((pos.get("trade_support") or {}).get("broker_open")),
            "gate_all_clear": gates.get("all_clear"),
            "trend_15m": gates.get("live_15min_macro_trend"),
            "feeds_health": feeds.get("health"),
            "fresh_count": feeds.get("fresh_count"),
            "rest_pressure": components.get("rest_pressure_level"),
            "trading_path_badge": components.get("trading_path_badge"),
            "entries_paused": components.get("entries_paused"),
            "why_idle": (why.get("after") or why.get("before") or {}),
            "sniper_epics": len(by_epic),
        }
    # Tail logs
    for name in ("v32_cfd.log", "v32_sb.log"):
        path = LOG_DIR / name
        off = state.log_offsets.get(name, path.stat().st_size if path.exists() else 0)
        # On first poll, start at EOF (live only); backfill handles history
        if name not in state.log_offsets and path.exists():
            state.log_offsets[name] = path.stat().st_size
            continue
        lines, new_off = _tail_new_lines(path, off)
        state.log_offsets[name] = new_off
        port_hint = 8080 if "cfd" in name else 8081
        _scan_log_lines(state, lines, port_hint=port_hint, jsonl=jsonl)
    return snap


def build_summary(state: ProbeState, ports: list[int], started: datetime) -> dict[str, Any]:
    funnel = aggregate_funnel(state.candidates)
    deg_n = sum(
        1
        for c in state.candidates
        if (c.get("feature_degeneracy") or {}).get("degenerate")
    )
    feature_health = {
        "degenerate_n": deg_n,
        "degenerate_rate": (deg_n / len(state.candidates)) if state.candidates else None,
        "sample_flags": Counter(
            flag
            for c in state.candidates
            for flag in ((c.get("feature_degeneracy") or {}).get("flags") or [])
        ),
    }
    # convert Counter
    feature_health["sample_flags"] = dict(feature_health["sample_flags"].most_common())

    latency = latency_summary(
        state.latency_ms,
        method=(
            "in-process evaluate_live_sniper_probability via "
            "alpha.micro_sniper_ml (perf_counter); NOT full tick→order e2e. "
            "API e2e_latency_ns from /api/unified/performance often zeroed."
        ),
    )

    journal_rows = _load_journal_since(7)
    daytime_pnl = summarize_journal_rows(journal_rows)

    # ml_trade_outcomes since London 07:00
    ml_rows: list[dict[str, Any]] = []
    cutoff = _now().replace(hour=7, minute=0, second=0, microsecond=0).timestamp()
    if ML_OUTCOMES.exists():
        with ML_OUTCOMES.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                ts = row.get("ts")
                try:
                    if float(ts) >= cutoff:
                        ml_rows.append(
                            {
                                "AccountID": row.get("account"),
                                "RealizedPnL_GBP": row.get("pnl"),
                                "MlScoreAtEntry": row.get("ml_score_at_entry", row.get("ml_score")),
                                "ExitReason": row.get("exit_reason"),
                                "Style": row.get("style"),
                                "HoldSec": row.get("hold_sec") or row.get("hold_duration_seconds"),
                            }
                        )
                except (TypeError, ValueError):
                    continue
    ml_day = summarize_journal_rows(ml_rows)

    # Live data plane snapshot (last poll ports)
    data_plane = {
        "http_429_hits": state.http_429,
        "feeds_ok": True,
        "rest_pressure_high": False,
        "ports": {},
    }
    for port in ports:
        feeds = _http_json(f"http://127.0.0.1:{port}/api/data_feed_state")
        stab = _http_json(f"http://127.0.0.1:{port}/api/desk/stability")
        components = ((stab.get("desk_stability") or {}).get("components") or {})
        if feeds.get("health") not in (None, "ok"):
            data_plane["feeds_ok"] = False
        if str(components.get("rest_pressure_level") or "").upper() == "HIGH":
            data_plane["rest_pressure_high"] = True
        data_plane["ports"][str(port)] = {
            "feeds": feeds,
            "rest_pressure": components.get("rest_pressure_level"),
            "path_badge": components.get("trading_path_badge"),
            "fresh_count": feeds.get("fresh_count"),
            "epic_quotes": feeds.get("epic_quotes"),
        }

    # Unified perf latency (often empty)
    unified = _http_json("http://127.0.0.1:8080/api/unified/performance")
    e2e = ((unified.get("e2e_latency_ns") or {}) if isinstance(unified, dict) else {})

    evidence = {
        "funnel": funnel,
        "feature_health": feature_health,
        "latency": latency,
        "daytime_pnl": daytime_pnl,
        "data_plane": data_plane,
    }
    verdict = cro_verdict_from_evidence(evidence)

    # Ranked remediation (diagnostic only)
    leaks: list[dict[str, Any]] = []
    if feature_health.get("degenerate_rate") and feature_health["degenerate_rate"] >= 0.5:
        leaks.append(
            {
                "severity": "P0",
                "title": "ML feature plane degenerate (OBI=0, elasticity≈1)",
                "evidence": feature_health,
                "fix": "Restore live OBI/elasticity inputs on rest_poll Mini; stop fail-open mid-threshold stamps on thin features.",
            }
        )
    if data_plane.get("rest_pressure_high"):
        leaks.append(
            {
                "severity": "P0",
                "title": "REST pressure HIGH — trading path down / entries starved",
                "evidence": {p: data_plane["ports"][p].get("path_badge") for p in data_plane["ports"]},
                "fix": "Throttle non-essential REST; confirm RestApiBudget 3/min; avoid dual-desk stampede.",
            }
        )
    if funnel.get("rejected", 0) and funnel.get("approved", 0) == 0:
        leaks.append(
            {
                "severity": "P1",
                "title": "Gates over-filtering daytime — zero approved candidates in sample",
                "evidence": funnel.get("by_reason"),
                "fix": "Reconcile sniper thr 0.68 vs selectivity 0.78; confirm 15m BEARISH only blocks BUY.",
            }
        )
    if daytime_pnl.get("ml_score_fill_rate") == 0 and daytime_pnl.get("n", 0) > 0:
        leaks.append(
            {
                "severity": "P1",
                "title": "Journal MlScoreAtEntry null on daytime closes",
                "evidence": {"n": daytime_pnl.get("n"), "ml_null": daytime_pnl.get("ml_null")},
                "fix": "Wire score_entry_candidate_ml stamp into journal/autopsy on fill (ml_unblind path).",
            }
        )
    if state.http_429 > 0:
        leaks.append(
            {
                "severity": "P2",
                "title": "Finnhub/multi_feed HTTP 429 during probe",
                "evidence": {"http_429_hits": state.http_429},
                "fix": "Keep yahoo/rest_poll primary; back off Finnhub reconnects.",
            }
        )
    if not any("long_trade" in str(c.get("reason", "")).lower() for c in state.candidates):
        leaks.append(
            {
                "severity": "P2",
                "title": "No long_trade_runner engagement observed",
                "evidence": {"log_ltr_hint": "0 in live candidate sample"},
                "fix": "Confirm SB LTR arming after 3m profit / 4R path during London.",
            }
        )

    # £1k/day sanity bound — NOT a claim of achievability
    n = daytime_pnl.get("n") or 0
    exp = daytime_pnl.get("expectancy_gbp")
    sanity = {
        "claim": "NOT proven",
        "daytime_n": n,
        "daytime_expectancy_gbp": exp,
        "implied_trades_for_1k": (
            int(math.ceil(1000.0 / exp)) if exp and exp > 0 else None
        ),
        "note": (
            "£1k/day requires positive expectancy × sufficient volume. "
            "Current daytime sample does not support promotion."
        ),
    }

    return {
        "generated_at": _iso(),
        "started_at": started.isoformat(),
        "duration_sec": (_now() - started).total_seconds(),
        "git_head": _git_head(),
        "config": os.environ.get("IG_AGENT_CONFIG", "config/config_v31_demo_throughput.json"),
        "app_mode": os.environ.get("APP_MODE", "DEMO"),
        "ports": ports,
        "port_meta": {str(p): PORT_META.get(p) for p in ports},
        "candidate_count": len(state.candidates),
        "polls": state.polls,
        "backfill_used": state.backfill_used,
        "notes": state.notes,
        "funnel": funnel,
        "feature_health": feature_health,
        "latency": latency,
        "unified_e2e_latency_ns": e2e,
        "daytime_journal": daytime_pnl,
        "daytime_ml_outcomes": ml_day,
        "data_plane": {
            "http_429_hits": data_plane["http_429_hits"],
            "feeds_ok": data_plane["feeds_ok"],
            "rest_pressure_high": data_plane["rest_pressure_high"],
            "port_summaries": {
                p: {
                    "rest_pressure": data_plane["ports"][p].get("rest_pressure"),
                    "path_badge": data_plane["ports"][p].get("path_badge"),
                    "fresh_count": data_plane["ports"][p].get("fresh_count"),
                    "feeds_health": (data_plane["ports"][p].get("feeds") or {}).get("health"),
                    "feed_detail": {
                        k: {
                            "health": (v or {}).get("health"),
                            "alive": (v or {}).get("alive"),
                            "retry_count": (v or {}).get("retry_count"),
                        }
                        for k, v in (
                            ((data_plane["ports"][p].get("feeds") or {}).get("feeds") or {})
                        ).items()
                    },
                    "quote_ages": [
                        {"epic": q.get("epic"), "age_sec": q.get("age_sec"), "source": q.get("source")}
                        for q in ((data_plane["ports"][p].get("feeds") or {}).get("epic_quotes") or [])
                    ],
                }
                for p in data_plane["ports"]
            },
        },
        "cro_verdict": verdict,
        "leaks": leaks[:5],
        "all_leaks": leaks,
        "gbp_1k_sanity": sanity,
        "sample_limits": {
            "target_candidates": None,  # filled by main
            "max_minutes": None,
            "achieved_candidates": len(state.candidates),
            "honest_note": (
                "Candidate = live sniper_ml epic snapshot and/or log matrix-block/submit. "
                "Not every quote tick. Latency is in-process scorer only."
            ),
        },
    }


def run_probe(
    *,
    ports: list[int],
    target_candidates: int = 100,
    max_minutes: float = 30.0,
    poll_sec: float = 2.0,
) -> dict[str, Any]:
    started = _now()
    day = started.strftime("%Y-%m-%d")
    jsonl = LOG_DIR / f"daylight_alpha_probe_{day}.jsonl"
    state = ProbeState()
    # Initialize log offsets at EOF
    for name in ("v32_cfd.log", "v32_sb.log"):
        path = LOG_DIR / name
        if path.exists():
            state.log_offsets[name] = path.stat().st_size

    print(
        f"[daylight] start {_iso()} ports={ports} target={target_candidates} "
        f"max_min={max_minutes} jsonl={jsonl}",
        flush=True,
    )
    deadline = time.time() + max_minutes * 60.0
    extend_checked = False

    while time.time() < deadline and len(state.candidates) < target_candidates:
        snap = _poll_once(state, ports, jsonl)
        print(
            f"[daylight] poll={state.polls} candidates={len(state.candidates)} "
            f"429={state.http_429} ports={json.dumps({k: {kk: vv for kk, vv in v.items() if kk in ('trade_ready','verdict','rest_pressure','trend_15m','gate_all_clear')} for k,v in (snap.get('ports') or {}).items()})}",
            flush=True,
        )
        elapsed_min = (time.time() - (deadline - max_minutes * 60.0)) / 60.0
        if (not extend_checked) and elapsed_min >= 10.0 and len(state.candidates) < 20:
            state.notes.append(
                f"scarce_candidates_at_10m n={len(state.candidates)} — engaging log backfill + continue"
            )
            print("[daylight] <20 candidates at 10m — log backfill + extend to max", flush=True)
            _backfill_logs(state, jsonl, max_add=max(0, target_candidates - len(state.candidates)))
            extend_checked = True
        # Early backfill if almost idle after first few polls
        if state.polls >= 3 and len(state.candidates) < 10 and not state.backfill_used:
            print("[daylight] scarce early — proactive log backfill", flush=True)
            _backfill_logs(state, jsonl, max_add=40)
        time.sleep(poll_sec)

    if len(state.candidates) < target_candidates and not state.backfill_used:
        print("[daylight] final backfill to approach target", flush=True)
        _backfill_logs(state, jsonl, max_add=max(0, target_candidates - len(state.candidates)))

    summary = build_summary(state, ports, started)
    summary["sample_limits"]["target_candidates"] = target_candidates
    summary["sample_limits"]["max_minutes"] = max_minutes
    summary["jsonl_path"] = str(jsonl)
    out_json = REPORT_DIR / f"daylight_alpha_probe_{day}.json"
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    summary["report_json"] = str(out_json)
    print(
        f"[daylight] done candidates={len(state.candidates)} verdict={summary['cro_verdict']} "
        f"json={out_json}",
        flush=True,
    )
    print("[daylight] TOP LEAKS:", flush=True)
    for i, leak in enumerate(summary.get("leaks") or [], 1):
        print(f"  {i}. [{leak.get('severity')}] {leak.get('title')}", flush=True)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Daylight alpha forensic probe (read-only)")
    ap.add_argument("--ports", default="8080,8081")
    ap.add_argument("--candidates", type=int, default=100)
    ap.add_argument("--max-minutes", type=float, default=30.0)
    ap.add_argument("--poll-sec", type=float, default=2.0)
    args = ap.parse_args(argv)
    ports = parse_ports(args.ports)
    os.environ.setdefault("IG_AGENT_CONFIG", "config/config_v31_demo_throughput.json")
    os.environ.setdefault("APP_MODE", "DEMO")
    run_probe(
        ports=ports,
        target_candidates=int(args.candidates),
        max_minutes=float(args.max_minutes),
        poll_sec=float(args.poll_sec),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
