#!/usr/bin/env python3
"""Overnight dual-desk monitor + 08:00 Europe/London trading report.

Ports / accounts (DEMO):
  :8080  Z6BAH4  QUANT_SNIPER   (CFD)
  :8081  Z6BAH3  MACRO_SENTINEL (SB)

Observe-only by default. If BOTH desks are dead AND books are flat, attempts
ONE anti-zombie dual restart via v32_runtime_start.sh (never kill -9).

Usage:
  PYTHONPATH=src python3 scripts/overnight_desk_monitor.py
  PYTHONPATH=src python3 scripts/overnight_desk_monitor.py --report-only
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "src/data/v31-production"
LOG_DIR = DATA / "logs"
REPORT_DIR = DATA / "reports"
STATE_DIR = DATA / "state"
JOURNAL = DATA / "metrics" / "daily_journal.csv"
AUTOPSY_DIR = DATA / "autopsy"
ML_STORE = DATA / "ml_training_store.jsonl"
ML_STORE_LEGACY = ROOT / "src/data/ml_training_store.jsonl"
ML_AUTO_TRAIN = DATA / "ml_auto_train.csv"
PID_FILE = STATE_DIR / "overnight_desk_monitor.pid"
BASELINE_FILE = STATE_DIR / "overnight_desk_monitor_baseline.json"
RESTART_MARK = STATE_DIR / "overnight_desk_monitor_restart.json"

TZ = ZoneInfo("Europe/London")
POLL_SEC = 180  # 3 minutes
PORTS = (
    {"port": 8080, "account": "Z6BAH4", "engine": "QUANT_SNIPER", "label": "CFD"},
    {"port": 8081, "account": "Z6BAH3", "engine": "MACRO_SENTINEL", "label": "SB"},
)

ML_LOG_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"interim_scorer",
        r"ml_scorer",
        r"ml_decision",
        r"sniper_ml",
        r"MicroScalper",
        r"long_trade_runner",
        r"LONG_TRADE",
        r"Core B|core_b",
        r"setup_memory",
        r"profit_policy",
        r"ml_gate|ML_GATE",
        r"streak[_ ]?protect",
        r"entries_paused",
        r"HTTP 429|status.?429",
        r"mutex",
        r"cascade|cap_breach|max_open",
        r"auto_train|ml_training",
    )
]

LOG_SCAN_FILES = (
    "ig_agent.log",
    "engine.log",
    "v32_sb.log",
    "v32_cfd.log",
    "production.log",
    "strategy_eval.log",
    "demo_execution_trace.log",
)


def _now_london() -> datetime:
    return datetime.now(TZ)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now_london()).isoformat()


def _parse_ts(s: Any) -> float | None:
    raw = str(s or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _http_json(url: str, timeout: float = 6.0) -> dict[str, Any]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "overnight_desk_monitor"})
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


def _day_stamp(dt: datetime | None = None) -> str:
    return (dt or _now_london()).strftime("%Y-%m-%d")


def _jsonl_path(day: str | None = None) -> Path:
    return LOG_DIR / f"overnight_monitor_{day or _day_stamp()}.jsonl"


def _tail_path(day: str | None = None) -> Path:
    return LOG_DIR / f"overnight_monitor_{day or _day_stamp()}.log"


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


def _file_meta(path: Path) -> dict[str, Any]:
    if not path.exists():
        # follow symlink target for ML store
        return {"path": str(path), "exists": False}
    try:
        st = path.stat()
        lines = 0
        if path.suffix in {".jsonl", ".csv", ".log"} and st.st_size < 50_000_000:
            with path.open("rb") as f:
                lines = sum(1 for _ in f)
        return {
            "path": str(path),
            "exists": True,
            "mtime": st.st_mtime,
            "mtime_iso": datetime.fromtimestamp(st.st_mtime, TZ).isoformat(),
            "size": st.st_size,
            "lines": lines,
        }
    except Exception as exc:
        return {"path": str(path), "exists": True, "error": str(exc)}


def _journal_rows() -> list[dict[str, str]]:
    if not JOURNAL.is_file():
        return []
    with JOURNAL.open(newline="", encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))


def _journal_since(start_epoch: float, *, diaaaaa_only: bool = False) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
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
        out.append(
            {
                "timestamp": row.get("Timestamp"),
                "ts": ts,
                "deal_id": deal,
                "account": row.get("AccountID") or "",
                "direction": row.get("Direction") or "",
                "pnl_gbp": pnl,
                "product": row.get("ProductType") or "",
                "engine_origin": row.get("EngineOrigin") or "",
                "entry_price": row.get("EntryPrice"),
                "exit_price": row.get("ExitPrice"),
            }
        )
    out.sort(key=lambda r: r.get("ts") or 0)
    return out


def _autopsy_since(start_epoch: float) -> list[dict[str, Any]]:
    if not AUTOPSY_DIR.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in AUTOPSY_DIR.glob("DIAAAA*.json"):
        try:
            d = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        exit_ts = _parse_ts(d.get("exit_time")) or _parse_ts(d.get("entry_time"))
        # autopsy times are often naive local — treat as London if naive
        if exit_ts is None:
            raw = str(d.get("exit_time") or d.get("entry_time") or "")
            try:
                dt = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
                exit_ts = dt.timestamp()
            except Exception:
                # fall back to mtime
                exit_ts = path.stat().st_mtime
        if exit_ts < start_epoch - 1:
            continue
        out.append(
            {
                "deal_id": d.get("deal_id") or path.stem,
                "entry_time": d.get("entry_time"),
                "exit_time": d.get("exit_time"),
                "ts": exit_ts,
                "side": d.get("side"),
                "pnl_gbp": d.get("pnl_gbp"),
                "exit_reason": d.get("exit_reason"),
                "ml_score_at_entry": d.get("ml_score_at_entry"),
                "confidence_at_entry": d.get("confidence_at_entry"),
                "setup_key": d.get("setup_key"),
                "regime_at_entry": d.get("regime_at_entry"),
                "size": d.get("size"),
                "entry_price": d.get("entry_price"),
                "exit_price": d.get("exit_price"),
                "source": "autopsy",
            }
        )
    out.sort(key=lambda r: r.get("ts") or 0)
    return out


def _infer_style(engine_origin: str, exit_reason: str = "", hold_sec: float | None = None) -> str:
    origin = (engine_origin or "").upper()
    reason = (exit_reason or "").lower()
    if "LONG" in origin or "long_runner" in reason or "long_trade" in reason:
        return "long"
    if hold_sec is not None:
        return "long" if hold_sec >= 180 else "scalp"
    if "MICRO" in origin or "SCALP" in origin or "SNIPER" in origin:
        return "scalp"
    if "SENTINEL" in origin or "MACRO" in origin:
        return "macro"
    if "dynamic_limit" in reason or "open_position" in reason:
        return "supervised_exit"
    return "unknown"


def _max_consec_wins(pnls: list[float]) -> int:
    best = cur = 0
    for p in pnls:
        if p > 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _scan_logs_since(start_epoch: float, max_bytes: int = 4_000_000) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    samples: dict[str, list[str]] = defaultdict(list)
    feed_429 = 0
    long_runner = 0
    micro = 0
    ml_gate_pass = 0
    ml_gate_fail = 0
    interim = 0
    mutex = 0
    cascade = 0

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
            # cheap time filter: only count lines that look recent OR match patterns
            matched = False
            tags: list[str] = []
            for pat in ML_LOG_PATTERNS:
                if pat.search(line):
                    matched = True
                    tags.append(pat.pattern.split("|")[0][:40])
            if not matched:
                continue
            # Prefer lines with timestamps >= start when parseable
            m = re.match(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", line)
            if m:
                ts = _parse_ts(m.group(1).replace(" ", "T"))
                if ts is not None and ts < start_epoch - 5:
                    continue
            low = line.lower()
            if "429" in low:
                feed_429 += 1
            if "long_trade" in low or "long_runner" in low:
                long_runner += 1
            if "microscalper" in low:
                micro += 1
            if "interim_scorer" in low or "ml_scorer" in low:
                interim += 1
            if "mutex" in low:
                mutex += 1
            if "cascade" in low or "cap_breach" in low:
                cascade += 1
            if re.search(r"ml.?gate.*(pass|approved|ok)", low):
                ml_gate_pass += 1
            if re.search(r"ml.?gate.*(fail|reject|block)", low):
                ml_gate_fail += 1
            for tag in tags:
                counts[tag] += 1
                if len(samples[tag]) < 3:
                    samples[tag].append(line[:240])

    return {
        "pattern_counts": dict(counts),
        "samples": {k: v for k, v in samples.items()},
        "feed_429_hits": feed_429,
        "long_trade_runner_hits": long_runner,
        "microscalper_hits": micro,
        "interim_ml_scorer_hits": interim,
        "mutex_hits": mutex,
        "cascade_hits": cascade,
        "ml_gate_pass_hits": ml_gate_pass,
        "ml_gate_fail_hits": ml_gate_fail,
    }


def _poll_port(port: int) -> dict[str, Any]:
    base = f"http://127.0.0.1:{port}"
    health = _http_json(f"{base}/api/health")
    positions = _http_json(f"{base}/api/positions/live")
    ops = _http_json(f"{base}/api/desk/ops_strip")
    sniper = _http_json(f"{base}/api/desk/sniper_ml")
    regime = _http_json(f"{base}/api/regime_state")

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
                "epic": p.get("epic") or p.get("instrument") or p.get("market"),
                "direction": p.get("direction") or p.get("side"),
                "upl": p.get("unrealized_pnl_gbp")
                or p.get("pnl_gbp")
                or p.get("upl")
                or p.get("profit_loss"),
                "deal_id": p.get("deal_id") or p.get("dealId"),
            }
        )

    markets = regime.get("markets") if isinstance(regime, dict) else None
    regime_summary = []
    if isinstance(markets, list):
        for m in markets[:8]:
            if not isinstance(m, dict):
                continue
            gate = m.get("strategy_gate") or {}
            regime_summary.append(
                {
                    "epic": m.get("epic"),
                    "state": m.get("state_label") or m.get("state"),
                    "allow_entries": (gate or {}).get("allow_entries"),
                    "mode": (gate or {}).get("mode"),
                }
            )

    sniper_ml = ops.get("sniper_ml") if isinstance(ops, dict) else None
    if not sniper_ml and isinstance(sniper, dict):
        sniper_ml = sniper.get("by_epic") or sniper

    return {
        "port": port,
        "down": bool(health.get("_down") or positions.get("_down")),
        "health_ok": health.get("ok"),
        "trading_healthy": health.get("trading_healthy"),
        "trade_ready": health.get("trade_ready")
        or ((health.get("iron_cage") or {}).get("trade_ready")),
        "session_status": health.get("session_status"),
        "agent_pid": health.get("agent_pid") or health.get("pid"),
        "app_mode": health.get("app_mode"),
        "config_overlay": health.get("config_overlay"),
        "paths_armed": health.get("paths_armed") or health.get("engine_paths_armed"),
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
            "rest_pressure": ops.get("rest_pressure"),
            "rest_pressure_level": ops.get("rest_pressure_level"),
            "rest_calls_last_minute": ops.get("rest_calls_last_minute"),
            "cap_breach": ops.get("cap_breach"),
            "max_open_positions": ops.get("max_open_positions"),
            "broker_open_snapshot": ops.get("broker_open_snapshot"),
            "desk_rag": ops.get("desk_rag"),
            "trading_path_live": ops.get("trading_path_live"),
            "trading_path_blockers": ops.get("trading_path_blockers"),
            "sniper_ml": sniper_ml,
            "order_mutex": ops.get("order_mutex"),
            "dual_engine_operational": ops.get("dual_engine_operational"),
            "healing_active": ops.get("healing_active"),
            "orchestrator": {
                "ok": (ops.get("orchestrator") or {}).get("ok"),
                "heal_attempts_in_window": (ops.get("orchestrator") or {}).get(
                    "heal_attempts_in_window"
                ),
                "last_fault": (ops.get("orchestrator") or {}).get("last_fault"),
            }
            if isinstance(ops.get("orchestrator"), dict)
            else ops.get("orchestrator"),
            "composite_status": ops.get("composite_status"),
        },
        "regime": regime_summary,
        "errors": [x for x in (health.get("error"), positions.get("error"), ops.get("error")) if x],
    }


def _snapshot_baseline(start_epoch: float) -> dict[str, Any]:
    journal_all = _journal_rows()
    dia = [r for r in journal_all if str(r.get("DealID", "")).startswith("DIAAAA")]
    opens = {}
    for d in PORTS:
        snap = _poll_port(d["port"])
        opens[str(d["port"])] = {
            "account": d["account"],
            "engine": d["engine"],
            "count": (snap.get("positions") or {}).get("count"),
            "verdict": (snap.get("positions") or {}).get("verdict"),
            "trading_healthy": snap.get("trading_healthy"),
        }
    ml_path = ML_STORE if ML_STORE.exists() else ML_STORE_LEGACY
    baseline = {
        "started_at_london": _iso(),
        "start_epoch": start_epoch,
        "git_head": _git_head(),
        "config": "config/config_v31_demo_throughput.json",
        "journal_path": str(JOURNAL),
        "journal_rows_total": len(journal_all),
        "journal_diaaaaa_total": len(dia),
        "journal_baseline_epoch": start_epoch,
        "open_counts": opens,
        "ml_training_store": _file_meta(ml_path),
        "ml_auto_train": _file_meta(ML_AUTO_TRAIN),
        "report_deadline_london": "2026-07-24T08:00:00+01:00",
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    BASELINE_FILE.write_text(json.dumps(baseline, indent=2, default=str), encoding="utf-8")
    return baseline


def _books_flat(polls: list[dict[str, Any]]) -> bool:
    for p in polls:
        pos = p.get("positions") or {}
        cnt = pos.get("count")
        if cnt not in (0, None):
            return False
        sot = pos.get("broker_open_sot") or {}
        if isinstance(sot, dict) and sot.get("count") not in (0, None):
            return False
        verdict = str(pos.get("verdict") or "").upper()
        if verdict and verdict not in {"FLAT", "UNKNOWN", ""}:
            # allow UNKNOWN when count 0
            if verdict != "FLAT" and (cnt or 0) > 0:
                return False
    return True


def _attempt_dual_restart_once(reason: str) -> dict[str, Any]:
    """ONE anti-zombie dual restart if hold/flat clear. Never kill -9."""
    if RESTART_MARK.exists():
        return {"attempted": False, "skipped": "already_attempted", "path": str(RESTART_MARK)}
    script = ROOT / "scripts" / "v32_runtime_start.sh"
    if not script.is_file():
        return {"attempted": False, "skipped": "missing_v32_runtime_start.sh"}
    env = os.environ.copy()
    env.setdefault("IG_AGENT_CONFIG", "config/config_v31_demo_throughput.json")
    env.setdefault("PYTHONPATH", "src")
    # Engage hold then stop+start via v32 helper (uses mark_manual_stop + TERM).
    record: dict[str, Any] = {
        "ts": _iso(),
        "reason": reason,
        "steps": [],
    }
    try:
        py = ROOT / ".venv" / "bin" / "python3"
        if not py.is_file():
            py = Path(sys.executable)
        hold = subprocess.run(
            [
                str(py),
                "-c",
                "from system.shutdown_cleanup import mark_manual_stop; mark_manual_stop(source='overnight_monitor_restart')",
            ],
            cwd=str(ROOT),
            env={**env, "PYTHONPATH": "src"},
            capture_output=True,
            text=True,
            timeout=30,
        )
        record["steps"].append(
            {"mark_manual_stop": hold.returncode, "stderr": (hold.stderr or "")[:300]}
        )
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
        # verify
        time.sleep(8)
        h0 = _http_json("http://127.0.0.1:8080/api/health")
        h1 = _http_json("http://127.0.0.1:8081/api/health")
        record["post_health"] = {
            "8080": {
                "down": h0.get("_down"),
                "trading_healthy": h0.get("trading_healthy"),
                "pid": h0.get("agent_pid") or h0.get("pid"),
            },
            "8081": {
                "down": h1.get("_down"),
                "trading_healthy": h1.get("trading_healthy"),
                "pid": h1.get("agent_pid") or h1.get("pid"),
            },
        }
        record["attempted"] = True
        record["ok"] = (not h0.get("_down")) and (not h1.get("_down"))
    except Exception as exc:
        record["attempted"] = True
        record["ok"] = False
        record["error"] = str(exc)
    RESTART_MARK.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    return record


def _load_baseline() -> dict[str, Any]:
    if BASELINE_FILE.is_file():
        try:
            return json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _merge_blotter(start_epoch: float) -> list[dict[str, Any]]:
    journal = _journal_since(start_epoch, diaaaaa_only=False)
    dia_j = [r for r in journal if str(r.get("deal_id", "")).startswith("DIAAAA")]
    autopsy = _autopsy_since(start_epoch)
    by_deal: dict[str, dict[str, Any]] = {}
    for r in autopsy:
        by_deal[str(r["deal_id"])] = dict(r)
    for r in dia_j:
        deal = str(r["deal_id"])
        cur = by_deal.get(deal, {})
        cur.update(
            {
                "deal_id": deal,
                "timestamp": r.get("timestamp") or cur.get("exit_time"),
                "ts": r.get("ts") or cur.get("ts"),
                "account": r.get("account") or cur.get("account"),
                "direction": r.get("direction") or cur.get("side"),
                "side": r.get("direction") or cur.get("side"),
                "pnl_gbp": r.get("pnl_gbp") if r.get("pnl_gbp") == r.get("pnl_gbp") else cur.get("pnl_gbp"),
                "engine_origin": r.get("engine_origin") or cur.get("engine_origin"),
                "product": r.get("product") or cur.get("product"),
                "exit_reason": cur.get("exit_reason") or r.get("engine_origin"),
                "ml_score_at_entry": cur.get("ml_score_at_entry"),
                "setup_key": cur.get("setup_key"),
                "source": "journal+autopsy" if deal in by_deal else "journal",
            }
        )
        style = _infer_style(str(cur.get("engine_origin") or ""), str(cur.get("exit_reason") or ""))
        # SB MACRO tends longer; CFD QUANT tends scalp
        acct = str(cur.get("account") or "")
        eng = str(cur.get("engine_origin") or "").upper()
        if style == "unknown":
            if acct == "Z6BAH3" or "SENTINEL" in eng or "MACRO" in eng:
                style = "macro_or_long"
            elif acct == "Z6BAH4" or "SNIPER" in eng:
                style = "scalp"
        cur["style"] = style
        by_deal[deal] = cur
    # Include autopsy-only DIAAAA even if journal lagging
    for deal, cur in list(by_deal.items()):
        if "account" not in cur or not cur.get("account"):
            # best-effort: size 0.5 often CFD; leave blank if unknown
            cur["account"] = cur.get("account") or ""
        if "style" not in cur:
            cur["style"] = _infer_style(str(cur.get("engine_origin") or ""), str(cur.get("exit_reason") or ""))
        by_deal[deal] = cur
    blotter = sorted(by_deal.values(), key=lambda r: r.get("ts") or 0)
    # Prefer DIAAAA only for critical blotter section
    return [r for r in blotter if str(r.get("deal_id", "")).startswith("DIAAAA")]


def _perf_table(blotter: list[dict[str, Any]], max_opens_seen: dict[str, int]) -> list[dict[str, Any]]:
    rows = []
    for d in PORTS:
        acct = d["account"]
        trades = [t for t in blotter if t.get("account") == acct]
        # if account missing on autopsy-only, also match by engine
        if not trades and d["engine"]:
            trades = [
                t
                for t in blotter
                if d["engine"].split("_")[0] in str(t.get("engine_origin") or "").upper()
            ]
        pnls = []
        for t in trades:
            try:
                pnls.append(float(t.get("pnl_gbp")))
            except Exception:
                pass
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        styles = Counter(str(t.get("style") or "unknown") for t in trades)
        scalp_n = styles.get("scalp", 0) + styles.get("supervised_exit", 0)
        long_n = styles.get("long", 0) + styles.get("macro_or_long", 0) + styles.get("macro", 0)
        rows.append(
            {
                "account": acct,
                "engine": d["engine"],
                "label": d["label"],
                "trades": len(trades),
                "wins": wins,
                "losses": losses,
                "net_gbp": round(sum(pnls), 2) if pnls else 0.0,
                "max_consecutive_wins": _max_consec_wins(pnls),
                "scalp_count": scalp_n,
                "long_count": long_n,
                "style_counts": dict(styles),
                "max_opens_seen": max_opens_seen.get(str(d["port"]), 0),
            }
        )
    return rows


def _baseline_window_stats(end_epoch: float, days: int = 7) -> dict[str, Any]:
    start = end_epoch - days * 86400
    rows = _journal_since(start, diaaaaa_only=True)
    # exclude overnight window from baseline if possible
    overnight_start = end_epoch  # caller passes overnight start as separate
    pnls = []
    for r in rows:
        try:
            pnls.append(float(r["pnl_gbp"]))
        except Exception:
            continue
    wins = sum(1 for p in pnls if p > 0)
    n = len(pnls)
    return {
        "window_days": days,
        "trades": n,
        "wins": wins,
        "losses": sum(1 for p in pnls if p < 0),
        "win_rate": round(wins / n, 4) if n else None,
        "net_gbp": round(sum(pnls), 2) if pnls else 0.0,
        "expectancy": round(sum(pnls) / n, 4) if n else None,
        "note": "DIAAAA journal rows in trailing window (includes overnight if overlapping)",
    }


def _ml_score_distribution(blotter: list[dict[str, Any]], sniper_snapshots: list[Any]) -> dict[str, Any]:
    scores = []
    for t in blotter:
        s = t.get("ml_score_at_entry")
        if s is None:
            continue
        try:
            scores.append(float(s))
        except Exception:
            pass
    approved_snap = 0
    rejected_snap = 0
    snap_scores = []

    def _ingest_sniper_row(row: dict[str, Any]) -> None:
        nonlocal approved_snap, rejected_snap, snap_scores
        if "approved" in row:
            if row.get("approved"):
                approved_snap += 1
            else:
                rejected_snap += 1
        if row.get("p_success") is not None:
            try:
                snap_scores.append(float(row["p_success"]))
            except Exception:
                pass

    for snap in sniper_snapshots:
        if not isinstance(snap, dict):
            continue
        if "approved" in snap or "p_success" in snap:
            _ingest_sniper_row(snap)
        by_epic = snap.get("by_epic") if isinstance(snap.get("by_epic"), dict) else None
        if by_epic:
            for row in by_epic.values():
                if isinstance(row, dict):
                    _ingest_sniper_row(row)
    return {
        "autopsy_ml_scores_on_closes": {
            "n": len(scores),
            "mean": round(sum(scores) / len(scores), 4) if scores else None,
            "min": min(scores) if scores else None,
            "max": max(scores) if scores else None,
        },
        "live_sniper_ml_poll_approvals": {
            "approved_hits": approved_snap,
            "rejected_hits": rejected_snap,
            "p_success_mean": round(sum(snap_scores) / len(snap_scores), 4) if snap_scores else None,
            "p_success_n": len(snap_scores),
        },
    }


def generate_report(
    *,
    start_epoch: float,
    baseline: dict[str, Any],
    poll_history_summary: dict[str, Any],
    restart_info: dict[str, Any] | None,
    labeled_window: str,
) -> tuple[Path, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_day = "2026-07-24"
    md_path = REPORT_DIR / f"trading_report_{report_day}_0800.md"
    json_path = REPORT_DIR / f"trading_report_{report_day}_0800.json"

    blotter = _merge_blotter(start_epoch)
    max_opens = poll_history_summary.get("max_opens_seen") or {}
    perf = _perf_table(blotter, max_opens)
    overnight_pnls = []
    for t in blotter:
        try:
            overnight_pnls.append(float(t.get("pnl_gbp")))
        except Exception:
            pass
    overnight_net = round(sum(overnight_pnls), 2) if overnight_pnls else 0.0
    overnight_wins = sum(1 for p in overnight_pnls if p > 0)
    overnight_n = len(overnight_pnls)
    overnight_wr = round(overnight_wins / overnight_n, 4) if overnight_n else None

    # Prior 7d ending at start of overnight (cleaner baseline)
    prior = _journal_since(start_epoch - 7 * 86400, diaaaaa_only=True)
    prior = [r for r in prior if (r.get("ts") or 0) < start_epoch]
    prior_pnls = []
    for r in prior:
        try:
            prior_pnls.append(float(r["pnl_gbp"]))
        except Exception:
            pass
    prior_n = len(prior_pnls)
    prior_wins = sum(1 for p in prior_pnls if p > 0)
    prior_wr = round(prior_wins / prior_n, 4) if prior_n else None
    prior_exp = round(sum(prior_pnls) / prior_n, 4) if prior_n else None
    overnight_exp = round(sum(overnight_pnls) / overnight_n, 4) if overnight_n else None

    improving = "insufficient_data"
    if overnight_n >= 8 and prior_n >= 20:
        if overnight_exp is not None and prior_exp is not None:
            if overnight_exp > prior_exp and (overnight_wr or 0) >= (prior_wr or 0):
                improving = "Y"
            elif overnight_exp < prior_exp and (overnight_wr or 0) <= (prior_wr or 0):
                improving = "N"
            else:
                improving = "mixed"
    elif overnight_n < 8:
        improving = "insufficient_data"

    log_scan = _scan_logs_since(start_epoch)
    ml_path = ML_STORE if ML_STORE.exists() else ML_STORE_LEGACY
    ml_meta_now = _file_meta(ml_path)
    ml_auto_now = _file_meta(ML_AUTO_TRAIN)
    ml_base = baseline.get("ml_training_store") or {}
    ml_auto_base = baseline.get("ml_auto_train") or {}
    ml_store_grew = (ml_meta_now.get("lines") or 0) > (ml_base.get("lines") or 0)
    ml_auto_grew = (ml_auto_now.get("mtime") or 0) > (ml_auto_base.get("mtime") or 0)

    sniper_alive = bool(poll_history_summary.get("sniper_ml_seen"))
    ml_log_alive = (
        log_scan.get("interim_ml_scorer_hits", 0) > 0
        or log_scan.get("microscalper_hits", 0) > 0
        or sniper_alive
    )
    ml_operating = "Y" if (sniper_alive or ml_log_alive) else "N"

    # Stability rollup
    stability = poll_history_summary.get("stability") or {}
    desk_down_events = poll_history_summary.get("desk_down_events") or []

    # Executive verdict
    risk_events = []
    if desk_down_events:
        risk_events.append(f"desk_down_events={len(desk_down_events)}")
    if stability.get("cap_breach_events"):
        risk_events.append(f"cap_breach_events={stability.get('cap_breach_events')}")
    if stability.get("cascade_log_hits"):
        risk_events.append(f"cascade_log_hits={stability.get('cascade_log_hits')}")
    if log_scan.get("feed_429_hits", 0) > 20:
        risk_events.append(f"feed_429_hits={log_scan.get('feed_429_hits')}")
    if stability.get("entries_paused_events"):
        risk_events.append(f"entries_paused_events={stability.get('entries_paused_events')}")

    success = True
    fail_reasons = []
    if desk_down_events and not (restart_info or {}).get("ok", True):
        # down and restart failed or not recovered
        if any(True for _ in desk_down_events):
            # check current health
            h0 = _http_json("http://127.0.0.1:8080/api/health")
            h1 = _http_json("http://127.0.0.1:8081/api/health")
            if h0.get("_down") or h1.get("_down"):
                success = False
                fail_reasons.append("desk still down at report time")
    if overnight_net < -150:
        success = False
        fail_reasons.append(f"overnight net £{overnight_net} below -150")
    if stability.get("cap_breach_events", 0) > 0:
        fail_reasons.append("cap breach observed")

    ready_for_day = success and ml_operating == "Y"
    if poll_history_summary.get("both_paths_live_at_end") is False:
        ready_for_day = False
        fail_reasons.append("trading path not live at end")

    verdict = "SUCCESS" if success and not fail_reasons else ("FAIL" if not success else "CAUTION")

    # Recommendations
    recs: list[str] = []
    if log_scan.get("feed_429_hits", 0) > 10:
        recs.append(
            "P1: Finnhub/multi_feed HTTP 429 noise — confirm rest_poll primary path unaffected; throttle secondary WS reconnects."
        )
    if not ml_store_grew and not ml_auto_grew:
        recs.append(
            "P1: ml_training_store / ml_auto_train showed little overnight growth — verify auto_trainer heartbeat and row appends on closes."
        )
    if overnight_n < 5:
        recs.append(
            "P2: Overnight sample thin — do not retune thresholds on this window; extend soak through London cash open."
        )
    sb = next((p for p in perf if p["account"] == "Z6BAH3"), None)
    cfd = next((p for p in perf if p["account"] == "Z6BAH4"), None)
    if sb and sb["trades"] == 0:
        recs.append(
            "P2: SB MACRO_SENTINEL (Z6BAH3) silent overnight — check entry hour gate / regime allow_entries / hard caps vs CFD sniper dominance."
        )
    if cfd and cfd["net_gbp"] < 0 and cfd["trades"] >= 3:
        recs.append(
            "P2: CFD QUANT_SNIPER net negative — review sniper_ml threshold (0.68 INDEX) vs taken fills; check scratch vs runner mix."
        )
    if log_scan.get("long_trade_runner_hits", 0) == 0:
        recs.append(
            "P3: No long_trade_runner log hits — confirm SB long mode arming (3m profit → 4R / 40% giveback) during London session."
        )
    if improving == "N":
        recs.append(
            "P1: Overnight expectancy/WR worse than prior 7d — freeze aggressive overlay tweaks; prefer observe another session."
        )
    if not recs:
        recs.append("P3: Continue DEMO soak; no urgent config change from overnight alone.")

    payload = {
        "report_id": f"trading_report_{report_day}_0800",
        "generated_at_london": _iso(),
        "window": labeled_window,
        "start_epoch": start_epoch,
        "git_head": baseline.get("git_head") or _git_head(),
        "config": "config/config_v31_demo_throughput.json",
        "executive": {
            "verdict": verdict,
            "success": success,
            "ready_for_day": ready_for_day,
            "fail_reasons": fail_reasons,
            "risk_events": risk_events,
            "overnight_net_gbp": overnight_net,
            "overnight_trades": overnight_n,
            "overnight_win_rate": overnight_wr,
            "ml_operating": ml_operating,
            "improving": improving,
        },
        "dual_strategy_performance": perf,
        "blotter": blotter,
        "ml_ai": {
            "operating": ml_operating,
            "sniper_ml_seen_in_polls": sniper_alive,
            "log_scan": log_scan,
            "ml_training_store_baseline": ml_base,
            "ml_training_store_now": ml_meta_now,
            "ml_training_store_grew": ml_store_grew,
            "ml_auto_train_baseline": ml_auto_base,
            "ml_auto_train_now": ml_auto_now,
            "ml_auto_train_grew": ml_auto_grew,
            "score_distribution": _ml_score_distribution(
                blotter, poll_history_summary.get("sniper_snapshots") or []
            ),
            "prior_7d": {
                "trades": prior_n,
                "wins": prior_wins,
                "win_rate": prior_wr,
                "expectancy": prior_exp,
                "net_gbp": round(sum(prior_pnls), 2) if prior_pnls else 0.0,
            },
            "overnight": {
                "trades": overnight_n,
                "wins": overnight_wins,
                "win_rate": overnight_wr,
                "expectancy": overnight_exp,
                "net_gbp": overnight_net,
            },
            "improving": improving,
            "gaps": [],
        },
        "stability": {
            **stability,
            "desk_down_events": desk_down_events,
            "restart": restart_info,
            "feed_429_hits": log_scan.get("feed_429_hits"),
            "mutex_hits": log_scan.get("mutex_hits"),
        },
        "recommendations": recs,
        "poll_summary": {
            "polls": poll_history_summary.get("polls"),
            "max_opens_seen": max_opens,
            "both_paths_live_at_end": poll_history_summary.get("both_paths_live_at_end"),
        },
        "baseline": baseline,
    }

    # ML gaps
    gaps = []
    if not sniper_alive:
        gaps.append("sniper_ml never observed approved/p_success on ops_strip during polls")
    if log_scan.get("interim_ml_scorer_hits", 0) == 0:
        gaps.append("no interim_scorer/ml_scorer log lines in overnight scan window")
    if not ml_store_grew:
        gaps.append("ml_training_store row count did not increase overnight")
    if log_scan.get("feed_429_hits", 0) > 0:
        gaps.append("multi_feed Finnhub HTTP 429 present (secondary feed; primary rest_poll may still be ok)")
    autopsy_scores = payload["ml_ai"]["score_distribution"]["autopsy_ml_scores_on_closes"]["n"]
    if overnight_n and autopsy_scores == 0:
        gaps.append("closes lack ml_score_at_entry in autopsy (scores null) — entry ML attribution weak")
    payload["ml_ai"]["gaps"] = gaps

    # Markdown
    lines: list[str] = []
    lines.append(f"# Trading Report — {report_day} 08:00 Europe/London")
    lines.append("")
    lines.append(f"- Generated: `{_iso()}`")
    lines.append(f"- Window: `{labeled_window}`")
    lines.append(f"- Git HEAD: `{payload['git_head']}`")
    lines.append(f"- Config: `config/config_v31_demo_throughput.json` (DEMO)")
    lines.append(f"- Desks: `:8080 Z6BAH4 QUANT_SNIPER` · `:8081 Z6BAH3 MACRO_SENTINEL`")
    lines.append("")
    lines.append("## A. Executive verdict")
    lines.append("")
    lines.append(f"**Verdict:** {verdict}")
    lines.append("")
    lines.append(
        f"- Overnight success: `{'Y' if success else 'N'}`"
        f" · Ready for day: `{'Y' if ready_for_day else 'N'}`"
        f" · ML/AI operating: `{ml_operating}`"
        f" · Improving vs prior 7d: `{improving}`"
    )
    lines.append(
        f"- Overnight net: **£{overnight_net}** on {overnight_n} DIAAAA closes"
        + (f" (WR {overnight_wr:.1%})" if overnight_wr is not None else "")
    )
    if risk_events:
        lines.append(f"- Risk events: {', '.join(risk_events)}")
    else:
        lines.append("- Risk events: none material beyond routine feed 429 noise (if any)")
    if fail_reasons:
        lines.append(f"- Caution/fail reasons: {'; '.join(fail_reasons)}")
    lines.append("")
    lines.append("## B. Dual strategy performance")
    lines.append("")
    lines.append(
        "| Account | Engine | Trades | Wins | Losses | Net £ | Max consecutive wins | Scalp vs long | Max opens |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---|---:|")
    for p in perf:
        lines.append(
            f"| {p['account']} ({p['label']}) | {p['engine']} | {p['trades']} | {p['wins']} | {p['losses']} | "
            f"{p['net_gbp']} | {p['max_consecutive_wins']} | scalp={p['scalp_count']} / long={p['long_count']} | "
            f"{p['max_opens_seen']} |"
        )
    lines.append("")
    lines.append("## C. Trade blotter (DIAAAA closes overnight)")
    lines.append("")
    if not blotter:
        lines.append("_No DIAAAA closes in the overnight window._")
        lines.append("")
    else:
        lines.append(
            "| Time (UTC journal / autopsy) | DealID | Account | Side | PnL £ | Exit / origin | Style | ML score |"
        )
        lines.append("|---|---|---|---|---:|---|---|---|")
        for t in blotter:
            ts = t.get("timestamp") or t.get("exit_time") or ""
            lines.append(
                f"| {ts} | `{t.get('deal_id')}` | {t.get('account') or '?'} | {t.get('side') or t.get('direction') or '?'} | "
                f"{t.get('pnl_gbp')} | {t.get('exit_reason') or t.get('engine_origin') or '?'} | {t.get('style')} | "
                f"{t.get('ml_score_at_entry')} |"
            )
        lines.append("")

    lines.append("## D. ML / AI operating assessment")
    lines.append("")
    lines.append(f"- **ML/AI alive:** `{ml_operating}`")
    lines.append(
        f"- sniper_ml seen in polls: `{sniper_alive}` · interim/ml_scorer log hits: `{log_scan.get('interim_ml_scorer_hits')}` · "
        f"MicroScalper hits: `{log_scan.get('microscalper_hits')}` · long_trade_runner hits: `{log_scan.get('long_trade_runner_hits')}`"
    )
    lines.append(
        f"- ml_training_store grew: `{ml_store_grew}` (lines {ml_base.get('lines')} → {ml_meta_now.get('lines')})"
    )
    lines.append(
        f"- ml_auto_train mtime advanced: `{ml_auto_grew}` (`{ml_auto_base.get('mtime_iso')}` → `{ml_auto_now.get('mtime_iso')}`)"
    )
    lines.append(
        f"- Live sniper approval polls: approved={payload['ml_ai']['score_distribution']['live_sniper_ml_poll_approvals']['approved_hits']} "
        f"rejected={payload['ml_ai']['score_distribution']['live_sniper_ml_poll_approvals']['rejected_hits']} "
        f"mean p_success={payload['ml_ai']['score_distribution']['live_sniper_ml_poll_approvals']['p_success_mean']}"
    )
    lines.append(
        f"- Autopsy ml_score_at_entry on closes: n={autopsy_scores} "
        f"mean={payload['ml_ai']['score_distribution']['autopsy_ml_scores_on_closes']['mean']}"
    )
    lines.append(
        f"- Prior 7d (pre-window DIAAAA): n={prior_n} WR={prior_wr} exp£={prior_exp} net£={payload['ml_ai']['prior_7d']['net_gbp']}"
    )
    lines.append(
        f"- Overnight: n={overnight_n} WR={overnight_wr} exp£={overnight_exp} net£={overnight_net} · improving=`{improving}`"
    )
    lines.append("")
    lines.append("### Dual ML paths")
    lines.append("")
    if cfd:
        lines.append(
            f"- CFD QUANT_SNIPER (Z6BAH4): trades={cfd['trades']} net£={cfd['net_gbp']} "
            f"scalp={cfd['scalp_count']} long={cfd['long_count']}"
        )
    if sb:
        lines.append(
            f"- SB MACRO_SENTINEL (Z6BAH3): trades={sb['trades']} net£={sb['net_gbp']} "
            f"scalp={sb['scalp_count']} long={sb['long_count']}"
        )
    lines.append(
        f"- long_trade_runner log evidence: `{log_scan.get('long_trade_runner_hits')}` hits "
        f"(SB expected primary; CFD may also engage)."
    )
    lines.append("")
    if gaps:
        lines.append("### Gaps / why ML may look weak")
        lines.append("")
        for g in gaps:
            lines.append(f"- {g}")
        lines.append("")
    if log_scan.get("pattern_counts"):
        lines.append("### Log pattern counts (overnight scan)")
        lines.append("")
        for k, v in sorted(log_scan["pattern_counts"].items(), key=lambda kv: -kv[1])[:20]:
            lines.append(f"- `{k}`: {v}")
        lines.append("")

    lines.append("## E. Stability")
    lines.append("")
    lines.append(f"- Polls: {poll_history_summary.get('polls')}")
    lines.append(f"- Desk down events: {len(desk_down_events)}")
    if desk_down_events:
        for e in desk_down_events[:10]:
            lines.append(f"  - {e}")
    lines.append(f"- Cap breach events: {stability.get('cap_breach_events', 0)}")
    lines.append(f"- Entries paused events: {stability.get('entries_paused_events', 0)}")
    lines.append(f"- REST pressure elevated events: {stability.get('rest_pressure_events', 0)}")
    lines.append(f"- Heal attempts observed: {stability.get('heal_attempts_seen', 0)}")
    lines.append(f"- Feed 429 log hits: {log_scan.get('feed_429_hits')}")
    lines.append(f"- Mutex log hits: {log_scan.get('mutex_hits')}")
    lines.append(f"- Cascade/cap log hits: {log_scan.get('cascade_hits')}")
    if restart_info:
        lines.append(
            f"- Dual restart attempted: `{restart_info.get('attempted')}` ok=`{restart_info.get('ok')}` "
            f"skipped=`{restart_info.get('skipped')}`"
        )
    else:
        lines.append("- Dual restart attempted: `N` (not required)")
    lines.append("")

    lines.append("## F. Critical assessment + recommendations")
    lines.append("")
    lines.append(
        "Honest read: treat overnight as a soak signal, not a promotion gate. "
        "DIAAAA blotter is authoritative for closed risk; short broker deal codes in journal are ignored for section C."
    )
    lines.append("")
    for r in recs:
        lines.append(f"1. {r}")
    lines.append("")
    lines.append("---")
    lines.append(f"_Monitor jsonl: `{_jsonl_path(_day_stamp(datetime.fromtimestamp(start_epoch, TZ)))}` / `{_jsonl_path()}`_")
    lines.append(f"_Baseline: `{BASELINE_FILE}`_")

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return md_path, json_path


def run_monitor(*, report_only: bool = False) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    start_dt = _now_london()
    start_epoch = start_dt.timestamp()

    # Report target: 08:00 London on 2026-07-24
    report_at = datetime(2026, 7, 24, 8, 0, 0, tzinfo=TZ)
    late = start_dt >= report_at
    labeled_window = (
        f"{start_dt.isoformat()} → report ASAP (past 08:00)"
        if late
        else f"{start_dt.isoformat()} → {report_at.isoformat()}"
    )

    baseline = _snapshot_baseline(start_epoch)
    _tail(
        f"START overnight_desk_monitor pid={os.getpid()} git={baseline.get('git_head','')[:12]} "
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
            poll_history_summary={
                "polls": 0,
                "max_opens_seen": {},
                "stability": {},
                "desk_down_events": [],
                "sniper_ml_seen": False,
                "sniper_snapshots": [],
                "both_paths_live_at_end": True,
            },
            restart_info=None,
            labeled_window=labeled_window,
        )
        _tail(f"REPORT-ONLY written {md}")
        print(md)
        return 0

    max_opens_seen: dict[str, int] = {"8080": 0, "8081": 0}
    stability = {
        "cap_breach_events": 0,
        "entries_paused_events": 0,
        "rest_pressure_events": 0,
        "heal_attempts_seen": 0,
        "cascade_log_hits": 0,
    }
    desk_down_events: list[str] = []
    sniper_snapshots: list[Any] = []
    sniper_ml_seen = False
    seen_deals: set[str] = set()
    polls = 0
    restart_info: dict[str, Any] | None = None
    consecutive_both_down = 0

    # seed seen deals so we only report NEW closes
    for r in _journal_since(start_epoch - 1, diaaaaa_only=False):
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
            ops = snap.get("ops") or {}
            if ops.get("cap_breach"):
                stability["cap_breach_events"] += 1
            if ops.get("entries_paused"):
                stability["entries_paused_events"] += 1
            if ops.get("rest_pressure") or str(ops.get("rest_pressure_level") or "").upper() in {
                "HIGH",
                "ELEVATED",
                "CRITICAL",
            }:
                stability["rest_pressure_events"] += 1
            orch = ops.get("orchestrator") or {}
            if isinstance(orch, dict) and orch.get("heal_attempts_in_window"):
                try:
                    stability["heal_attempts_seen"] = max(
                        stability["heal_attempts_seen"], int(orch.get("heal_attempts_in_window") or 0)
                    )
                except Exception:
                    pass
            sm = ops.get("sniper_ml")
            if isinstance(sm, dict) and (sm.get("p_success") is not None or sm.get("by_epic")):
                sniper_ml_seen = True
                if len(sniper_snapshots) < 40:
                    sniper_snapshots.append(sm)

        both_down = all(s.get("down") for s in port_snaps)
        if both_down:
            consecutive_both_down += 1
            desk_down_events.append(_iso() + " both ports down")
        else:
            consecutive_both_down = 0
            for s in port_snaps:
                if s.get("down"):
                    desk_down_events.append(
                        f"{_iso()} port={s['port']} account={s.get('account')} down err={s.get('errors')}"
                    )

        # new journal closes
        new_closes = []
        for r in _journal_since(start_epoch, diaaaaa_only=False):
            deal = str(r.get("deal_id"))
            if deal in seen_deals:
                continue
            seen_deals.add(deal)
            new_closes.append(r)

        # ML file heartbeat
        ml_path = ML_STORE if ML_STORE.exists() else ML_STORE_LEGACY
        ml_meta = _file_meta(ml_path)
        ml_auto = _file_meta(ML_AUTO_TRAIN)

        row = {
            "type": "poll",
            "ts": _iso(),
            "poll": polls,
            "desks": port_snaps,
            "new_closes": new_closes,
            "ml_training_store": ml_meta,
            "ml_auto_train": ml_auto,
            "max_opens_seen": dict(max_opens_seen),
        }
        # rotate jsonl day stamp at midnight
        _append_jsonl(_jsonl_path(), row)

        # human line
        parts = []
        for s in port_snaps:
            pos = s.get("positions") or {}
            ops = s.get("ops") or {}
            sm = ops.get("sniper_ml") or {}
            p_succ = sm.get("p_success") if isinstance(sm, dict) else None
            parts.append(
                f"{s['port']}/{s.get('account')} healthy={s.get('trading_healthy')} "
                f"verdict={pos.get('verdict')} opens={pos.get('count')} paused={ops.get('entries_paused')} "
                f"rest={ops.get('rest_pressure_level')} sniper_p={p_succ} rag={ops.get('desk_rag')}"
            )
        close_s = ""
        if new_closes:
            close_s = " NEW_CLOSES=" + ",".join(
                f"{c.get('deal_id')}:{c.get('account')}:{c.get('pnl_gbp')}" for c in new_closes
            )
        _tail(f"poll#{polls} " + " || ".join(parts) + close_s)

        # ONE restart if both dead for 2 consecutive polls and flat
        if consecutive_both_down >= 2 and restart_info is None:
            if _books_flat(port_snaps):
                _tail("ALERT both desks down + flat — attempting ONE anti-zombie dual restart")
                restart_info = _attempt_dual_restart_once("both_ports_down_flat")
                _append_jsonl(_jsonl_path(), {"type": "restart", "ts": _iso(), "info": restart_info})
                _tail(f"restart result attempted={restart_info.get('attempted')} ok={restart_info.get('ok')}")
            else:
                _tail("ALERT both desks down but books not confirmed flat — NOT restarting")
                desk_down_events.append(_iso() + " restart_skipped_not_flat")

        # Report time?
        if now >= report_at or late:
            # end-state path live check
            both_live = True
            for s in port_snaps:
                ops = s.get("ops") or {}
                if s.get("down") or ops.get("trading_path_live") is False:
                    both_live = False
            # enrich stability with log scan cascade
            scan = _scan_logs_since(start_epoch)
            stability["cascade_log_hits"] = scan.get("cascade_hits", 0)

            summary = {
                "polls": polls,
                "max_opens_seen": max_opens_seen,
                "stability": stability,
                "desk_down_events": desk_down_events,
                "sniper_ml_seen": sniper_ml_seen,
                "sniper_snapshots": sniper_snapshots,
                "both_paths_live_at_end": both_live,
            }
            if late:
                labeled = f"{baseline.get('started_at_london')} → {_iso()} (ASAP; past 08:00 target)"
            else:
                labeled = f"{baseline.get('started_at_london')} → {_iso()} (08:00 London target)"
            md, js = generate_report(
                start_epoch=start_epoch,
                baseline=baseline,
                poll_history_summary=summary,
                restart_info=restart_info,
                labeled_window=labeled,
            )
            _tail(f"REPORT written md={md} json={js}")
            _append_jsonl(
                _jsonl_path(),
                {"type": "report", "ts": _iso(), "md": str(md), "json": str(js)},
            )
            # If we were late, we still ran one poll then report; exit.
            # If on-time, exit after report.
            return 0

        # sleep until next poll, but wake near report_at
        remaining = (report_at - _now_london()).total_seconds()
        sleep_for = POLL_SEC
        if remaining > 0:
            sleep_for = min(POLL_SEC, max(5, remaining + 1))
        time.sleep(sleep_for)


def main() -> int:
    global POLL_SEC
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report-only", action="store_true", help="Generate report immediately from current window start")
    ap.add_argument("--poll-sec", type=int, default=0, help="Poll interval seconds (default 180)")
    args = ap.parse_args()
    if args.poll_sec and int(args.poll_sec) > 0:
        POLL_SEC = max(60, int(args.poll_sec))
    try:
        return run_monitor(report_only=bool(args.report_only))
    finally:
        try:
            (STATE_DIR / "overnight_desk_monitor_exit.json").write_text(
                json.dumps({"pid": os.getpid(), "exited_at": _iso()}, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
