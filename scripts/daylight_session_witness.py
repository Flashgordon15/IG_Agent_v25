#!/usr/bin/env python3
"""Daylight London/US dual-desk witness — observe-only success report.

Ports / accounts (DEMO):
  :8080  Z6BAH4  QUANT_SNIPER   (CFD)
  :8081  Z6BAH3  MACRO_SENTINEL (SB)

Polls health/positions, tails journal + ml_trade_outcomes for DIAAAA closes,
scans CFD/SB logs for OBI / ElasticGate / fail-open hints. Never places orders
or loosens gates. ONE anti-zombie dual restart only if BOTH desks dead AND flat.

Usage:
  PYTHONPATH=src python3 scripts/daylight_session_witness.py
  PYTHONPATH=src python3 scripts/daylight_session_witness.py --until 16:00
  PYTHONPATH=src python3 scripts/daylight_session_witness.py --report-only
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "src/data/v31-production"
LOG_DIR = DATA / "logs"
REPORT_DIR = DATA / "reports"
STATE_DIR = DATA / "state"
JOURNAL = DATA / "metrics" / "daily_journal.csv"
ML_OUTCOMES = DATA / "metrics" / "ml_trade_outcomes.jsonl"
AUTOPSY_DIR = DATA / "autopsy"
PID_FILE = STATE_DIR / "daylight_session_witness.pid"
BASELINE_FILE = STATE_DIR / "daylight_session_witness_baseline.json"
RESTART_MARK = STATE_DIR / "daylight_session_witness_restart.json"

TZ = ZoneInfo("Europe/London")
POLL_SEC = 150  # ~2.5 min
PORTS = (
    {"port": 8080, "account": "Z6BAH4", "engine": "QUANT_SNIPER", "label": "CFD"},
    {"port": 8081, "account": "Z6BAH3", "engine": "MACRO_SENTINEL", "label": "SB"},
)

LOG_SCAN_FILES = ("v32_cfd.log", "v32_sb.log", "demo_execution_trace.log", "engine.log")

OBI_UNAVAILABLE_RE = re.compile(r"obi_unavailable", re.I)
OBI_ANY_RE = re.compile(r"regime_veto_obi_|obi_|quote_proxy", re.I)
FAIL_OPEN_RE = re.compile(r"fail[_ -]?open|failopen", re.I)
ELASTIC_RE = re.compile(
    r"ElasticGate|elastic_gate|healthy[_ -]?band|elastic_band|vol_adaptive|gate_band",
    re.I,
)
LONG_RUNNER_RE = re.compile(r"long_trade_runner|LONG_TRADE|long_runner", re.I)
CASCADE_RE = re.compile(r"cascade|cap_breach|max_open", re.I)
REJECT_REASON_RE = re.compile(
    r"reason=(regime_veto_[a-z0-9_]+|[a-z0-9_]*obi[a-z0-9_]*|[a-z0-9_]+)",
    re.I,
)


def _now_london() -> datetime:
    return datetime.now(TZ)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now_london()).isoformat()


def _day_stamp(dt: datetime | None = None) -> str:
    return (dt or _now_london()).strftime("%Y-%m-%d")


def _parse_ts(s: Any, *, naive_tz: Any = timezone.utc) -> float | None:
    raw = str(s or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=naive_tz)
        return dt.timestamp()
    except Exception:
        return None


def _parse_log_ts(s: str) -> float | None:
    """Agent log lines are Europe/London wall clock without offset."""
    return _parse_ts(s, naive_tz=TZ)


def _http_json(url: str, timeout: float = 6.0) -> dict[str, Any]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "daylight_session_witness"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return {"ok": False, "error": str(exc), "_down": True}


def _git_head() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(ROOT), stderr=subprocess.DEVNULL
        )
        return out.decode().strip()
    except Exception:
        return "unknown"


def _jsonl_path(day: str | None = None) -> Path:
    return LOG_DIR / f"daylight_witness_{day or _day_stamp()}.jsonl"


def _tail_path(day: str | None = None) -> Path:
    return LOG_DIR / f"daylight_witness_{day or _day_stamp()}.log"


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def _tail(msg: str, day: str | None = None) -> None:
    line = f"{_now_london().strftime('%Y-%m-%d %H:%M:%S %Z')} | {msg}"
    path = _tail_path(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    try:
        print(line, flush=True)
    except Exception:
        pass


def _parse_until(arg: str, day: datetime) -> datetime:
    """Parse HH:MM Europe/London on the witness day."""
    raw = (arg or "16:00").strip()
    hh, mm = raw.split(":")
    return datetime(day.year, day.month, day.day, int(hh), int(mm), 0, tzinfo=TZ)


def _journal_rows() -> list[dict[str, str]]:
    if not JOURNAL.is_file():
        return []
    with JOURNAL.open(newline="", encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))


def _ml_outcomes_by_deal() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not ML_OUTCOMES.is_file():
        return out
    try:
        for line in ML_OUTCOMES.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            deal = str(row.get("deal_id") or "").strip()
            if deal:
                out[deal] = row
    except Exception:
        pass
    return out


def _autopsy_ml(deal: str) -> Any:
    path = AUTOPSY_DIR / f"{deal}.json"
    if not path.is_file():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return d.get("ml_score_at_entry")
    except Exception:
        return None


def _journal_since(start_epoch: float, *, diaaaaa_only: bool = True) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    ml_map = _ml_outcomes_by_deal()
    for row in _journal_rows():
        ts = _parse_ts(row.get("Timestamp"))
        if ts is None or ts < start_epoch - 1:
            continue
        if ts > time.time() + 120:
            continue
        deal = str(row.get("DealID") or "").strip()
        if not deal:
            continue
        if diaaaaa_only and not deal.startswith("DIAAAA"):
            continue
        key = f"{deal}|{row.get('AccountID')}|{row.get('Timestamp')}|{row.get('RealizedPnL_GBP')}"
        if key in seen:
            continue
        seen.add(key)
        try:
            pnl = float(str(row.get("RealizedPnL_GBP") or "nan"))
        except Exception:
            pnl = float("nan")
        hold_sec = None
        try:
            if row.get("HoldSec") not in (None, ""):
                hold_sec = float(row.get("HoldSec"))
        except Exception:
            hold_sec = None
        ml_row = ml_map.get(deal) or {}
        ml_score = ml_row.get("ml_score")
        if ml_score is None:
            # journal may gain MlScoreAtEntry later; autopsy fallback
            raw_j = row.get("MlScoreAtEntry") or row.get("ml_score")
            if raw_j not in (None, ""):
                try:
                    ml_score = float(raw_j)
                except Exception:
                    ml_score = raw_j
            else:
                ml_score = _autopsy_ml(deal)
        out.append(
            {
                "timestamp": row.get("Timestamp"),
                "ts": ts,
                "deal_id": deal,
                "account": row.get("AccountID") or "",
                "direction": row.get("Direction") or "",
                "pnl_gbp": pnl,
                "product": row.get("ProductType") or "",
                "engine_origin": row.get("EngineOrigin") or ml_row.get("engine_origin") or "",
                "exit_reason": row.get("ExitReason") or ml_row.get("exit_reason") or "",
                "hold_sec": hold_sec if hold_sec is not None else ml_row.get("hold_sec"),
                "style": row.get("Style") or ml_row.get("style") or "",
                "ml_score": ml_score,
                "epic": ml_row.get("epic") or "",
            }
        )
    out.sort(key=lambda r: r.get("ts") or 0)
    return out


def _poll_port(port: int) -> dict[str, Any]:
    base = f"http://127.0.0.1:{port}"
    health = _http_json(f"{base}/api/health")
    positions = _http_json(f"{base}/api/positions/live")
    ops = _http_json(f"{base}/api/desk/ops_strip")

    pos_rows = []
    if isinstance(positions.get("positions"), list):
        src = positions["positions"]
    elif isinstance((positions.get("memory_context") or {}).get("positions"), list):
        src = positions["memory_context"]["positions"]
    else:
        src = []
    for p in src:
        if not isinstance(p, dict):
            continue
        pos_rows.append(
            {
                "epic": p.get("epic") or p.get("instrument"),
                "direction": p.get("direction") or p.get("side"),
                "upl": p.get("unrealized_pnl_gbp") or p.get("pnl_gbp") or p.get("upl"),
                "deal_id": p.get("deal_id") or p.get("dealId"),
            }
        )

    return {
        "port": port,
        "down": bool(health.get("_down") or positions.get("_down")),
        "health_ok": health.get("ok"),
        "trading_healthy": health.get("trading_healthy"),
        "trade_ready": health.get("trade_ready")
        or ((health.get("iron_cage") or {}).get("trade_ready")),
        "session_status": health.get("session_status"),
        "agent_pid": health.get("agent_pid") or health.get("pid"),
        "session_id": health.get("session_id"),
        "app_mode": health.get("app_mode"),
        "positions": {
            "count": positions.get("count"),
            "verdict": positions.get("verdict"),
            "critical": positions.get("critical"),
            "total_pnl_gbp": positions.get("total_pnl_gbp"),
            "rows": pos_rows,
            "broker_open_sot": positions.get("broker_open_sot"),
        },
        "ops": {
            "entries_paused": ops.get("entries_paused"),
            "rest_pressure_level": ops.get("rest_pressure_level"),
            "cap_breach": ops.get("cap_breach"),
            "trading_path_live": ops.get("trading_path_live"),
            "desk_rag": ops.get("desk_rag"),
            "sniper_ml": ops.get("sniper_ml"),
        },
        "errors": [x for x in (health.get("error"), positions.get("error"), ops.get("error")) if x],
    }


def _scan_reject_funnel(start_epoch: float, max_bytes: int = 3_000_000) -> dict[str, Any]:
    reasons: Counter[str] = Counter()
    obi_unavail = 0
    obi_other = 0
    fail_open = 0
    elastic = 0
    long_runner = 0
    cascade = 0
    elastic_samples: list[str] = []
    fail_open_samples: list[str] = []
    post_start_obi_unavail = 0

    for name in LOG_SCAN_FILES:
        path = LOG_DIR / name
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
            with path.open("rb") as f:
                if size > max_bytes:
                    f.seek(size - max_bytes)
                    f.readline()
                text = f.read().decode("utf-8", errors="replace")
        except Exception:
            continue
        for line in text.splitlines():
            m = re.match(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", line)
            line_ts = None
            if m:
                line_ts = _parse_log_ts(m.group(1).replace(" ", "T"))
                if line_ts is not None and line_ts < start_epoch - 5:
                    continue
            # Skip undated lines from the pre-window byte tail — they inflate OBI false FAIL.
            if line_ts is None:
                continue

            if FAIL_OPEN_RE.search(line):
                fail_open += 1
                if len(fail_open_samples) < 5:
                    fail_open_samples.append(line[:240])
            if ELASTIC_RE.search(line):
                elastic += 1
                if len(elastic_samples) < 5:
                    elastic_samples.append(line[:240])
            if LONG_RUNNER_RE.search(line):
                long_runner += 1
            if CASCADE_RE.search(line):
                cascade += 1

            if OBI_UNAVAILABLE_RE.search(line):
                obi_unavail += 1
                post_start_obi_unavail += 1
            elif OBI_ANY_RE.search(line):
                obi_other += 1

            if "blocked" in line.lower() or "reject" in line.lower() or "veto" in line.lower():
                rm = REJECT_REASON_RE.search(line)
                if rm:
                    reasons[rm.group(1)] += 1
                elif OBI_UNAVAILABLE_RE.search(line):
                    reasons["obi_unavailable"] += 1

    total_rejects = sum(reasons.values())
    top = reasons.most_common(12)
    obi_unavail_reason_n = sum(
        v for k, v in reasons.items() if "obi_unavailable" in str(k).lower()
    )
    obi_share = (obi_unavail_reason_n / total_rejects) if total_rejects else 0.0

    return {
        "reject_reason_counts": dict(top),
        "reject_reason_all": dict(reasons),
        "obi_unavailable_log_hits": obi_unavail,
        "obi_unavailable_in_window": post_start_obi_unavail,
        "obi_other_log_hits": obi_other,
        "obi_unavailable_share_of_rejects": round(obi_share, 4),
        "fail_open_hits": fail_open,
        "fail_open_samples": fail_open_samples,
        "elastic_gate_hits": elastic,
        "elastic_gate_samples": elastic_samples,
        "long_trade_runner_hits": long_runner,
        "cascade_log_hits": cascade,
    }


def _books_flat(polls: list[dict[str, Any]]) -> bool:
    for p in polls:
        pos = p.get("positions") or {}
        cnt = pos.get("count")
        if cnt not in (0, None):
            return False
        sot = pos.get("broker_open_sot") or {}
        if isinstance(sot, dict) and sot.get("count") not in (0, None):
            return False
    return True


def _attempt_dual_restart_once(reason: str) -> dict[str, Any]:
    if RESTART_MARK.exists():
        return {"attempted": False, "skipped": "already_attempted", "path": str(RESTART_MARK)}
    script = ROOT / "scripts" / "v32_runtime_start.sh"
    if not script.is_file():
        return {"attempted": False, "skipped": "missing_v32_runtime_start.sh"}
    env = os.environ.copy()
    env.setdefault("IG_AGENT_CONFIG", "config/config_v31_demo_throughput.json")
    env.setdefault("PYTHONPATH", "src")
    record: dict[str, Any] = {"ts": _iso(), "reason": reason, "steps": []}
    try:
        py = ROOT / ".venv" / "bin" / "python3"
        if not py.is_file():
            py = Path(sys.executable)
        hold = subprocess.run(
            [
                str(py),
                "-c",
                "from system.shutdown_cleanup import mark_manual_stop; "
                "mark_manual_stop(source='daylight_witness_restart')",
            ],
            cwd=str(ROOT),
            env={**env, "PYTHONPATH": "src"},
            capture_output=True,
            text=True,
            timeout=30,
        )
        record["steps"].append({"mark_manual_stop": hold.returncode, "stderr": (hold.stderr or "")[:300]})
        stop = subprocess.run(
            ["bash", str(script), "stop"],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        record["steps"].append(
            {
                "stop_rc": stop.returncode,
                "stdout_tail": (stop.stdout or "")[-500:],
                "stderr_tail": (stop.stderr or "")[-500:],
            }
        )
        time.sleep(3)
        start = subprocess.run(
            ["bash", str(script), "start"],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        record["steps"].append(
            {
                "start_rc": start.returncode,
                "stdout_tail": (start.stdout or "")[-800:],
                "stderr_tail": (start.stderr or "")[-500:],
            }
        )
        time.sleep(8)
        h0 = _http_json("http://127.0.0.1:8080/api/health")
        h1 = _http_json("http://127.0.0.1:8081/api/health")
        record["post_health"] = {
            "8080": {"down": h0.get("_down"), "pid": h0.get("agent_pid")},
            "8081": {"down": h1.get("_down"), "pid": h1.get("agent_pid")},
        }
        record["attempted"] = True
        record["ok"] = (not h0.get("_down")) and (not h1.get("_down"))
    except Exception as exc:
        record["attempted"] = True
        record["ok"] = False
        record["error"] = str(exc)
    RESTART_MARK.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    return record


def _perf(blotter: list[dict[str, Any]]) -> dict[str, Any]:
    pnls: list[float] = []
    for t in blotter:
        try:
            p = float(t.get("pnl_gbp"))
            if p == p:
                pnls.append(p)
        except Exception:
            pass
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    n = len(pnls)
    ml_non_null = sum(1 for t in blotter if t.get("ml_score") is not None and t.get("ml_score") != "")
    longish = [
        t
        for t in blotter
        if (t.get("hold_sec") is not None and float(t.get("hold_sec") or 0) >= 180)
        or "long" in str(t.get("style") or "").lower()
        or "long" in str(t.get("engine_origin") or "").lower()
    ]
    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "wr": round(wins / n, 4) if n else None,
        "net_gbp": round(sum(pnls), 2) if pnls else 0.0,
        "ml_non_null": ml_non_null,
        "ml_fill_rate": round(ml_non_null / n, 4) if n else None,
        "long_style_closes": len(longish),
        "by_account": {
            acct: {
                "n": sum(1 for t in blotter if t.get("account") == acct),
                "net": round(
                    sum(
                        float(t["pnl_gbp"])
                        for t in blotter
                        if t.get("account") == acct and t.get("pnl_gbp") == t.get("pnl_gbp")
                    ),
                    2,
                ),
            }
            for acct in ("Z6BAH4", "Z6BAH3")
        },
    }


def _evaluate(
    *,
    perf: dict[str, Any],
    funnel: dict[str, Any],
    max_opens: dict[str, int],
    volume_note: str | None = None,
) -> dict[str, Any]:
    n = int(perf.get("n") or 0)
    wr = perf.get("wr")
    net = float(perf.get("net_gbp") or 0.0)
    ml_fill = perf.get("ml_fill_rate")
    obi_share = float(funnel.get("obi_unavailable_share_of_rejects") or 0.0)
    obi_hits = int(funnel.get("obi_unavailable_in_window") or 0)
    fail_open = int(funnel.get("fail_open_hits") or 0)
    cascade_opens = any(int(v or 0) > 1 for v in max_opens.values())
    cascade_logs = int(funnel.get("cascade_log_hits") or 0)

    checks = {
        "closes_ge_8": n >= 8,
        "wr_ge_50_or_net_gt_0": (wr is not None and wr >= 0.50) or net > 0,
        "ml_fill_ge_80": (ml_fill is not None and ml_fill >= 0.80) if n else False,
        "obi_not_dominating": obi_share < 0.50 or obi_hits == 0,
        "no_cascade_opens": not cascade_opens,
        "no_fail_open_stamps": fail_open == 0,
    }
    primary_ok = all(checks.values())

    fail_reasons: list[str] = []
    if n >= 8 and wr is not None and wr < 0.40 and net < -20:
        fail_reasons.append(f"deep_negative WR={wr:.1%} net£={net} on n={n}")
    if obi_share >= 0.60 and obi_hits >= 20:
        fail_reasons.append(f"OBI plane dying again (obi_unavailable share={obi_share:.0%} hits={obi_hits})")
    if cascade_opens:
        fail_reasons.append(f"cascade max_opens={max_opens}")
    if fail_open > 0:
        fail_reasons.append(f"fail-open stamps={fail_open}")
    if n >= 8 and (ml_fill is None or ml_fill < 0.50):
        fail_reasons.append(f"attribution mostly null ml_fill={ml_fill}")

    secondary = {
        "long_trade_runner_style_hold": int(perf.get("long_style_closes") or 0) > 0
        or int(funnel.get("long_trade_runner_hits") or 0) > 0,
        "elastic_gate_evidence": int(funnel.get("elastic_gate_hits") or 0) > 0,
        "net_progressing": net > 0,
    }

    # Hard FAIL reasons (OBI death / cascade / fail-open) apply even with thin volume.
    hard_fail = any(
        r.startswith("OBI plane") or r.startswith("cascade") or r.startswith("fail-open")
        for r in fail_reasons
    )
    if hard_fail:
        verdict = "FAIL"
    elif fail_reasons and n >= 8:
        verdict = "FAIL"
    elif primary_ok:
        verdict = "SUCCESS"
    elif n < 8:
        verdict = "INCONCLUSIVE"
    else:
        missing = [k for k, v in checks.items() if not v]
        if "wr_ge_50_or_net_gt_0" in missing:
            verdict = "FAIL"
        else:
            verdict = "INCONCLUSIVE"

    return {
        "verdict": verdict,
        "primary_checks": checks,
        "fail_reasons": fail_reasons,
        "secondary": secondary,
        "volume_note": volume_note
        or (
            None
            if n >= 8
            else f"Only {n} DIAAAA closes in witness window (<8) — volume too low for SUCCESS"
        ),
        "cascade_log_hits": cascade_logs,
    }


def generate_report(
    *,
    start_epoch: float,
    baseline: dict[str, Any],
    poll_summary: dict[str, Any],
    restart_info: dict[str, Any] | None,
    labeled_window: str,
    end_reason: str,
) -> tuple[Path, Path]:
    day = _day_stamp()
    md_path = REPORT_DIR / f"daylight_success_witness_{day}.md"
    json_path = REPORT_DIR / f"daylight_success_witness_{day}.json"

    blotter = _journal_since(start_epoch, diaaaaa_only=True)
    perf = _perf(blotter)
    funnel = _scan_reject_funnel(start_epoch)
    max_opens = poll_summary.get("max_opens_seen") or {}
    evaluation = _evaluate(perf=perf, funnel=funnel, max_opens=max_opens)

    pids = {}
    for d in PORTS:
        snap = _poll_port(d["port"])
        pids[str(d["port"])] = {
            "account": d["account"],
            "engine": d["engine"],
            "agent_pid": snap.get("agent_pid"),
            "session_id": snap.get("session_id"),
            "session_status": snap.get("session_status"),
            "trade_ready": snap.get("trade_ready"),
            "down": snap.get("down"),
            "opens": (snap.get("positions") or {}).get("count"),
            "verdict": (snap.get("positions") or {}).get("verdict"),
        }

    worked_plain = {
        "SUCCESS": (
            "Daylight improvements look like they worked in this window: enough real closes, "
            "non-negative edge or ≥50% WR, ML attribution mostly filled, OBI not dead, no cascade."
        ),
        "FAIL": (
            "Daylight improvements did NOT deliver success in this window — see fail reasons. "
            "Do not treat OBI restore alone as proven profitable daytime edge."
        ),
        "INCONCLUSIVE": (
            "Cannot honestly claim the daylight improvements 'worked' yet — sample too thin "
            "and/or mixed primary checks. Keep witnessing; do not loosen gates."
        ),
    }

    payload = {
        "generated_at": _iso(),
        "git_head": _git_head(),
        "window": labeled_window,
        "end_reason": end_reason,
        "start_epoch": start_epoch,
        "baseline": baseline,
        "pids": pids,
        "perf": perf,
        "blotter": blotter,
        "obi_funnel": funnel,
        "evaluation": evaluation,
        "poll_summary": {
            "polls": poll_summary.get("polls"),
            "max_opens_seen": max_opens,
            "desk_down_events": poll_summary.get("desk_down_events"),
        },
        "restart": restart_info,
        "plain_english": worked_plain.get(evaluation["verdict"], worked_plain["INCONCLUSIVE"]),
        "jsonl": str(_jsonl_path(day)),
    }

    lines: list[str] = []
    lines.append(f"# Daylight Success Witness — {day}")
    lines.append("")
    lines.append(f"- Generated: `{_iso()}`")
    lines.append(f"- Window: `{labeled_window}`")
    lines.append(f"- End reason: `{end_reason}`")
    lines.append(f"- Git HEAD: `{payload['git_head']}`")
    lines.append(f"- Config: `config/config_v31_demo_throughput.json` (DEMO)")
    lines.append("- Desks: `:8080 Z6BAH4 QUANT_SNIPER` · `:8081 Z6BAH3 MACRO_SENTINEL`")
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(f"**{evaluation['verdict']}**")
    lines.append("")
    lines.append(payload["plain_english"])
    lines.append("")
    if evaluation.get("volume_note"):
        lines.append(f"_Volume note:_ {evaluation['volume_note']}")
        lines.append("")
    if evaluation.get("fail_reasons"):
        lines.append("Fail reasons:")
        for r in evaluation["fail_reasons"]:
            lines.append(f"- {r}")
        lines.append("")

    lines.append("### Primary checks")
    lines.append("")
    for k, v in evaluation["primary_checks"].items():
        lines.append(f"- `{k}`: **{'PASS' if v else 'FAIL'}**")
    lines.append("")
    lines.append("### Secondary (informational)")
    lines.append("")
    for k, v in evaluation["secondary"].items():
        lines.append(f"- `{k}`: `{v}`")
    lines.append("")

    lines.append("## PIDs / desk state (end)")
    lines.append("")
    lines.append("| Port | Account | PID | Session | Opens | Verdict | trade_ready |")
    lines.append("|---|---|---|---|---:|---|---|")
    for port, info in pids.items():
        lines.append(
            f"| {port} | {info['account']} | {info.get('agent_pid')} | `{info.get('session_id')}` | "
            f"{info.get('opens')} | {info.get('verdict')} | {info.get('trade_ready')} |"
        )
    lines.append("")
    lines.append(
        f"- Max opens seen during witness: `{max_opens}` "
        f"(cascade if any account >1)"
    )
    lines.append(f"- Polls: {poll_summary.get('polls')}")
    if restart_info:
        lines.append(
            f"- Dual restart: attempted=`{restart_info.get('attempted')}` "
            f"ok=`{restart_info.get('ok')}` skipped=`{restart_info.get('skipped')}`"
        )
    else:
        lines.append("- Dual restart: not required")
    lines.append("")

    lines.append("## Performance (DIAAAA closes in window)")
    lines.append("")
    lines.append(
        f"- n={perf['n']} · wins={perf['wins']} · losses={perf['losses']} · "
        f"WR={perf['wr']} · net£={perf['net_gbp']}"
    )
    lines.append(
        f"- ML fill (non-null ml_score from outcomes/autopsy/journal): "
        f"{perf['ml_non_null']}/{perf['n']} = {perf['ml_fill_rate']}"
    )
    lines.append(f"- By account: `{perf['by_account']}`")
    lines.append("")

    lines.append("## Trade blotter")
    lines.append("")
    if not blotter:
        lines.append("_No DIAAAA closes in the witness window._")
        lines.append("")
    else:
        lines.append("| Time | DealID | Account | Side | PnL £ | Origin / exit | HoldSec | ML |")
        lines.append("|---|---|---|---|---:|---|---:|---|")
        for t in blotter:
            lines.append(
                f"| {t.get('timestamp')} | `{t.get('deal_id')}` | {t.get('account')} | "
                f"{t.get('direction')} | {t.get('pnl_gbp')} | "
                f"{t.get('exit_reason') or t.get('engine_origin') or '?'} | "
                f"{t.get('hold_sec')} | {t.get('ml_score')} |"
            )
        lines.append("")

    lines.append("## OBI / reject funnel")
    lines.append("")
    lines.append(
        f"- `obi_unavailable` log hits in window: **{funnel.get('obi_unavailable_in_window')}** "
        f"(share of reject reasons ≈ {funnel.get('obi_unavailable_share_of_rejects')})"
    )
    lines.append(f"- Other OBI-related hits: {funnel.get('obi_other_log_hits')}")
    lines.append(f"- Fail-open hits: **{funnel.get('fail_open_hits')}**")
    lines.append(f"- ElasticGate / healthy-band hits: {funnel.get('elastic_gate_hits')}")
    lines.append(f"- long_trade_runner log hits: {funnel.get('long_trade_runner_hits')}")
    lines.append(f"- Cascade/cap log hits: {funnel.get('cascade_log_hits')}")
    lines.append("")
    if funnel.get("reject_reason_counts"):
        lines.append("Top reject/block reasons (cheap log parse):")
        lines.append("")
        for k, v in (funnel.get("reject_reason_counts") or {}).items():
            lines.append(f"- `{k}`: {v}")
        lines.append("")
    if funnel.get("elastic_gate_samples"):
        lines.append("ElasticGate samples:")
        for s in funnel["elastic_gate_samples"]:
            lines.append(f"- `{s}`")
        lines.append("")
    if funnel.get("fail_open_samples"):
        lines.append("Fail-open samples:")
        for s in funnel["fail_open_samples"]:
            lines.append(f"- `{s}`")
        lines.append("")

    lines.append("## Critical assessment + next steps")
    lines.append("")
    lines.append(
        "Honest read: OBI quote-proxy restore (deb8d4b) and nightmare overnight gates are "
        "infrastructure; this report only scores whether daytime closes met empirical success criteria. "
        "Journal lacks a native `MlScoreAtEntry` column today — fill-rate uses "
        "`ml_trade_outcomes.jsonl` / autopsy join."
    )
    lines.append("")
    if evaluation["verdict"] == "SUCCESS":
        lines.append("1. Keep gates tight; do not loosen on one good daylight sample.")
        lines.append("2. Continue ML attribution hygiene — ensure every close lands non-null ml_score.")
        lines.append("3. Track long_trade_runner separately if scalp WR carries the day.")
    elif evaluation["verdict"] == "FAIL":
        lines.append("1. Do not loosen gates. Triage fail reasons before another overnight soak.")
        lines.append("2. If OBI died again, verify quote-proxy path on both CFD/SB processes.")
        lines.append("3. If WR/net collapsed with good attribution, problem is edge not plumbing.")
    else:
        lines.append("1. Continue witness into US cash open if still before 16:00; volume may arrive late.")
        lines.append("2. Confirm DualCore is dispatching (not only rejecting) on DOW hot path.")
        lines.append("3. Re-check post-OBI-restore reject mix — meltup_guard ≠ unavailable.")
    lines.append("")
    lines.append("---")
    lines.append(f"_jsonl: `{_jsonl_path(day)}`_")
    lines.append(f"_baseline: `{BASELINE_FILE}`_")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return md_path, json_path


def _snapshot_baseline(start_epoch: float) -> dict[str, Any]:
    journal = _journal_rows()
    dia = [r for r in journal if str(r.get("DealID", "")).startswith("DIAAAA")]
    opens = {}
    for d in PORTS:
        snap = _poll_port(d["port"])
        opens[str(d["port"])] = {
            "account": d["account"],
            "agent_pid": snap.get("agent_pid"),
            "session_id": snap.get("session_id"),
            "count": (snap.get("positions") or {}).get("count"),
            "verdict": (snap.get("positions") or {}).get("verdict"),
            "trade_ready": snap.get("trade_ready"),
            "session_status": snap.get("session_status"),
        }
    baseline = {
        "started_at_london": _iso(),
        "start_epoch": start_epoch,
        "git_head": _git_head(),
        "config": "config/config_v31_demo_throughput.json",
        "journal_rows_total": len(journal),
        "journal_diaaaaa_total": len(dia),
        "ml_outcomes_lines": (
            sum(1 for line in ML_OUTCOMES.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip())
            if ML_OUTCOMES.is_file()
            else 0
        ),
        "open_counts": opens,
        "note": "OBI quote-proxy restore deb8d4b; post-reload ALPHA FUNCTIONING probe earlier today",
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    BASELINE_FILE.write_text(json.dumps(baseline, indent=2, default=str), encoding="utf-8")
    return baseline


def run_monitor(*, until: datetime, report_only: bool = False, early_exit: bool = True) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    start_dt = _now_london()
    start_epoch = start_dt.timestamp()
    labeled_window = f"{start_dt.isoformat()} → {until.isoformat()}"

    baseline = _snapshot_baseline(start_epoch)
    _tail(
        f"START daylight_session_witness pid={os.getpid()} git={baseline.get('git_head','')[:12]} "
        f"window={labeled_window} poll={POLL_SEC}s"
    )
    _append_jsonl(
        _jsonl_path(),
        {"type": "start", "ts": _iso(), "baseline": baseline, "window": labeled_window},
    )

    if report_only:
        md, js = generate_report(
            start_epoch=start_epoch,
            baseline=baseline,
            poll_summary={"polls": 0, "max_opens_seen": {}, "desk_down_events": []},
            restart_info=None,
            labeled_window=labeled_window,
            end_reason="report_only",
        )
        _tail(f"REPORT-ONLY written {md}")
        print(md)
        return 0

    max_opens_seen: dict[str, int] = {"8080": 0, "8081": 0}
    desk_down_events: list[str] = []
    seen_deals: set[str] = set()
    polls = 0
    restart_info: dict[str, Any] | None = None
    consecutive_both_down = 0
    end_reason = "until_deadline"

    for r in _journal_since(start_epoch - 1, diaaaaa_only=True):
        seen_deals.add(str(r.get("deal_id")))

    while True:
        now = _now_london()
        polls += 1
        port_snaps = []
        for d in PORTS:
            snap = _poll_port(d["port"])
            snap["account"] = d["account"]
            snap["engine"] = d["engine"]
            snap["label"] = d["label"]
            port_snaps.append(snap)
            cnt = (snap.get("positions") or {}).get("count") or 0
            try:
                cnt_i = int(cnt)
            except Exception:
                cnt_i = 0
            max_opens_seen[str(d["port"])] = max(max_opens_seen[str(d["port"])], cnt_i)

        both_down = all(s.get("down") for s in port_snaps)
        if both_down:
            consecutive_both_down += 1
            desk_down_events.append(_iso() + " both ports down")
        else:
            consecutive_both_down = 0
            for s in port_snaps:
                if s.get("down"):
                    desk_down_events.append(
                        f"{_iso()} port={s['port']} account={s.get('account')} down"
                    )

        new_closes = []
        for r in _journal_since(start_epoch, diaaaaa_only=True):
            deal = str(r.get("deal_id"))
            if deal in seen_deals:
                continue
            seen_deals.add(deal)
            new_closes.append(r)

        funnel_snap = _scan_reject_funnel(start_epoch)
        blotter = _journal_since(start_epoch, diaaaaa_only=True)
        perf = _perf(blotter)
        evaluation = _evaluate(perf=perf, funnel=funnel_snap, max_opens=max_opens_seen)

        row = {
            "type": "poll",
            "ts": _iso(),
            "poll": polls,
            "desks": [
                {
                    "port": s["port"],
                    "account": s.get("account"),
                    "agent_pid": s.get("agent_pid"),
                    "down": s.get("down"),
                    "trade_ready": s.get("trade_ready"),
                    "session_status": s.get("session_status"),
                    "opens": (s.get("positions") or {}).get("count"),
                    "verdict": (s.get("positions") or {}).get("verdict"),
                    "upl": (s.get("positions") or {}).get("total_pnl_gbp"),
                }
                for s in port_snaps
            ],
            "new_closes": new_closes,
            "perf": perf,
            "obi": {
                "obi_unavailable_in_window": funnel_snap.get("obi_unavailable_in_window"),
                "obi_share": funnel_snap.get("obi_unavailable_share_of_rejects"),
                "fail_open": funnel_snap.get("fail_open_hits"),
                "elastic": funnel_snap.get("elastic_gate_hits"),
            },
            "evaluation": evaluation,
            "max_opens_seen": dict(max_opens_seen),
        }
        _append_jsonl(_jsonl_path(), row)

        parts = []
        for s in port_snaps:
            pos = s.get("positions") or {}
            parts.append(
                f"{s['port']}/{s.get('account')} pid={s.get('agent_pid')} "
                f"ready={s.get('trade_ready')} verdict={pos.get('verdict')} opens={pos.get('count')}"
            )
        close_s = ""
        if new_closes:
            close_s = " NEW=" + ",".join(
                f"{c.get('deal_id')}:{c.get('account')}:{c.get('pnl_gbp')}:ml={c.get('ml_score')}"
                for c in new_closes
            )
        _tail(
            f"poll#{polls} n={perf['n']} WR={perf['wr']} net£={perf['net_gbp']} "
            f"ml_fill={perf['ml_fill_rate']} obi_unavail={funnel_snap.get('obi_unavailable_in_window')} "
            f"verdict={evaluation['verdict']} || "
            + " || ".join(parts)
            + close_s
        )

        if consecutive_both_down >= 2 and restart_info is None:
            if _books_flat(port_snaps):
                _tail("ALERT both desks down + flat — ONE anti-zombie dual restart")
                restart_info = _attempt_dual_restart_once("both_ports_down_flat")
                _append_jsonl(_jsonl_path(), {"type": "restart", "ts": _iso(), "info": restart_info})
                _tail(
                    f"restart attempted={restart_info.get('attempted')} ok={restart_info.get('ok')}"
                )
            else:
                _tail("ALERT both desks down but books not flat — NOT restarting")
                desk_down_events.append(_iso() + " restart_skipped_not_flat")

        decisive = False
        if early_exit and evaluation["verdict"] == "SUCCESS" and int(perf.get("n") or 0) >= 8:
            end_reason = "early_SUCCESS"
            decisive = True
        elif early_exit and evaluation["verdict"] == "FAIL" and int(perf.get("n") or 0) >= 8:
            end_reason = "early_FAIL"
            decisive = True
        elif now >= until:
            end_reason = "until_deadline"
            decisive = True

        if decisive:
            summary = {
                "polls": polls,
                "max_opens_seen": max_opens_seen,
                "desk_down_events": desk_down_events,
            }
            labeled = f"{baseline.get('started_at_london')} → {_iso()} ({end_reason})"
            md, js = generate_report(
                start_epoch=start_epoch,
                baseline=baseline,
                poll_summary=summary,
                restart_info=restart_info,
                labeled_window=labeled,
                end_reason=end_reason,
            )
            _tail(f"REPORT written verdict={evaluation['verdict']} md={md} json={js}")
            _append_jsonl(
                _jsonl_path(),
                {
                    "type": "report",
                    "ts": _iso(),
                    "verdict": evaluation["verdict"],
                    "md": str(md),
                    "json": str(js),
                    "end_reason": end_reason,
                },
            )
            return 0

        remaining = (until - _now_london()).total_seconds()
        sleep_for = POLL_SEC
        if remaining > 0:
            sleep_for = min(POLL_SEC, max(5, remaining + 1))
        time.sleep(sleep_for)


def main() -> int:
    global POLL_SEC
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--until", default="16:00", help="Europe/London HH:MM end (default 16:00)")
    ap.add_argument("--poll-sec", type=int, default=0, help="Poll interval seconds (default 150)")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument(
        "--no-early-exit",
        action="store_true",
        help="Always run until --until even if SUCCESS/FAIL already clear",
    )
    args = ap.parse_args()
    if args.poll_sec and int(args.poll_sec) > 0:
        POLL_SEC = max(60, int(args.poll_sec))
    until = _parse_until(args.until, _now_london())
    try:
        return run_monitor(
            until=until,
            report_only=bool(args.report_only),
            early_exit=not bool(args.no_early_exit),
        )
    finally:
        try:
            (STATE_DIR / "daylight_session_witness_exit.json").write_text(
                json.dumps({"pid": os.getpid(), "exited_at": _iso()}, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
