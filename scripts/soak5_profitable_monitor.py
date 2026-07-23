#!/usr/bin/env python3
"""Dual-engine soak monitor: 5 consecutive profitable closes (RealizedPnL_GBP > 0).

Covers:
  - CFD QUANT_SNIPER  :8080 / Z6BAH4
  - SB  MACRO_SENTINEL :8081 / Z6BAH3

Success bar:
  - real DIAAAA* DealIDs from either account
  - consecutive streak of 5 with RealizedPnL_GBP > 0 (loss resets streak)
  - per-account opens ≤ hard-cap (CFD=1; SB independent)
  - cascades == 0
  - prefer evidence both engines participated (document if SB silent but healthy)
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "src/data/v31-production"
JOURNAL = DATA / "metrics" / "daily_journal.csv"
LOG = DATA / "logs" / "soak5_profit_monitor.log"
RESULT = DATA / "state" / "soak5_profit_result.json"
SESSION = DATA / "state" / "soak5_profit_session.json"
POLL = 45
MAX_SEC = 4 * 3600
GOAL = 5
CFD_URL = "http://127.0.0.1:8080"
SB_URL = "http://127.0.0.1:8081"
CFD_ACCT = "Z6BAH4"
SB_ACCT = "Z6BAH3"
CFD_HARD_CAP = 1
SB_HARD_CAP = 10  # independent; cascade = > hard-cap, not merge with CFD


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {msg}"
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass


def get(url: str, t: float = 6):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "soak5_profit"})
        with urllib.request.urlopen(req, timeout=t) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def parse_ts(s):
    s = str(s or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _infer_style(row: dict) -> str:
    """Best-effort scalp vs long label for the final report."""
    origin = str(row.get("EngineOrigin") or row.get("origin") or "").upper()
    reason = str(row.get("ExitReason") or row.get("exit_reason") or "").lower()
    hold = row.get("HoldSec") or row.get("hold_sec")
    try:
        hold_f = float(hold) if hold not in (None, "") else None
    except Exception:
        hold_f = None
    if "LONG" in origin or "long_runner" in reason or "target_profit" in reason:
        if hold_f is not None and hold_f >= 180:
            return "long"
    if hold_f is not None:
        return "long" if hold_f >= 180 else "scalp"
    if "MICRO" in origin or "SCALP" in origin or "SNIPER" in origin:
        return "scalp"
    if "SENTINEL" in origin or "MACRO" in origin:
        return "long" if (hold_f or 0) >= 180 else "scalp_or_macro"
    return "unknown"


def journal_profits(start_epoch: float):
    if not JOURNAL.is_file():
        return [], [], 0
    rows = list(
        csv.DictReader(JOURNAL.open(newline="", encoding="utf-8", errors="replace"))
    )
    settled = []
    seen = set()
    for row in rows:
        ts = parse_ts(row.get("Timestamp") or "")
        if ts is None or ts < start_epoch - 2:
            continue
        deal = str(row.get("DealID") or "").strip()
        if not deal.startswith("DIAAAA"):
            continue
        if deal in seen:
            continue
        if ts > time.time() + 120:
            continue
        try:
            pnl = float(str(row.get("RealizedPnL_GBP") or "nan"))
        except Exception:
            continue
        if pnl != pnl:  # NaN
            continue
        seen.add(deal)
        acct = str(row.get("AccountID") or "")
        origin = str(row.get("EngineOrigin") or "")
        entry = {
            "deal": deal,
            "pnl": pnl,
            "account": acct,
            "product": str(row.get("ProductType") or ""),
            "origin": origin,
            "direction": str(row.get("Direction") or ""),
            "ts": str(row.get("Timestamp") or ""),
            "ts_epoch": ts,
            "entry": str(row.get("EntryPrice") or ""),
            "exit": str(row.get("ExitPrice") or ""),
            "exit_reason": str(row.get("ExitReason") or ""),
            "hold_sec": str(row.get("HoldSec") or row.get("DurationSec") or ""),
            "profitable": pnl > 0,
            "style": _infer_style(row),
            "lane": (
                "CFD"
                if CFD_ACCT in acct or "SNIPER" in origin.upper()
                else (
                    "SB"
                    if SB_ACCT in acct or "SENTINEL" in origin.upper()
                    else "unknown"
                )
            ),
        }
        settled.append(entry)
    settled.sort(key=lambda r: r["ts_epoch"])
    profits = [r for r in settled if r["profitable"]]
    streak = 0
    for r in reversed(settled):
        if r["profitable"]:
            streak += 1
        else:
            break
    return profits, settled, streak


def open_count(pos_body: dict, account_id: str | None = None) -> int:
    try:
        rows = pos_body.get("positions") or pos_body.get("rows") or []
        n = 0
        for r in rows:
            acct = str(r.get("account_id") or r.get("AccountID") or "")
            if account_id and account_id not in acct and acct:
                continue
            epic = str(
                r.get("epic") or (r.get("market") or {}).get("epic") or ""
            )
            if epic or acct:
                n += 1
        sot = int((pos_body.get("broker_open_sot") or {}).get("count") or 0)
        ts = int((pos_body.get("trade_support") or {}).get("broker_open") or 0)
        cnt = int(pos_body.get("count") or 0)
        if account_id:
            return max(n, sot if n or sot else 0, ts if n or ts else 0, cnt if n else 0)
        return max(cnt, sot, ts, n)
    except Exception:
        return int(pos_body.get("count") or 0)


def flatten_all(reason: str) -> None:
    log(f"CASCADE/STOP flatten reason={reason}")
    env = os.environ.copy()
    env["CORE_DETACHED"] = "FALSE"
    env["APP_MODE"] = "DEMO"
    env["IG_AGENT_CONFIG"] = "config/config_v31_demo_throughput.json"
    env["PYTHONPATH"] = str(ROOT / "src")
    for extra in ({}, {"IG_ACCOUNT_ID": CFD_ACCT}, {"IG_ACCOUNT_ID": SB_ACCT}):
        try:
            e = env.copy()
            e.update(extra)
            subprocess.run(
                [
                    str(ROOT / ".venv/bin/python3"),
                    str(ROOT / "scripts/emergency_book_unwind.py"),
                    "--yes",
                ],
                cwd=str(ROOT),
                env=e,
                timeout=120,
                check=False,
            )
        except Exception as exc:
            log(f"flatten failed acct={extra}: {exc}")


def lane_health(base: str) -> dict:
    h = get(f"{base}/api/health")
    p = get(f"{base}/api/positions/live")
    return {"health": h if isinstance(h, dict) else {}, "positions": p if isinstance(p, dict) else {}}


def main() -> int:
    SESSION.parent.mkdir(parents=True, exist_ok=True)
    start = time.time()
    if SESSION.is_file():
        try:
            prev = json.loads(SESSION.read_text(encoding="utf-8"))
            if float(prev.get("start_epoch") or 0) > 0:
                start = float(prev["start_epoch"])
        except Exception:
            pass
    SESSION.write_text(
        json.dumps(
            {
                "start_epoch": start,
                "start_iso": datetime.fromtimestamp(start, tz=timezone.utc).isoformat(),
                "goal": GOAL,
                "mode": "consecutive_profitable_dual",
                "ports": {"cfd": 8080, "sb": 8081},
                "accounts": {"cfd": CFD_ACCT, "sb": SB_ACCT},
                "monitor_boot_iso": datetime.now(tz=timezone.utc).isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    log(
        f"SOAK START dual-engine goal={GOAL} consecutive profitable "
        f"(CFD:{CFD_URL} {CFD_ACCT} / SB:{SB_URL} {SB_ACCT})"
    )
    cfd_max = 0
    sb_max = 0
    cascades = 0
    sb_saw_open = False
    sb_healthy = False

    while time.time() - start < MAX_SEC:
        try:
            cfd = lane_health(CFD_URL)
            sb = lane_health(SB_URL)
            cfd_opens = open_count(cfd["positions"], CFD_ACCT)
            # SB API is account-scoped; count all opens on :8081
            sb_opens = open_count(sb["positions"], None)
            if not cfd["positions"].get("error"):
                # fallback if account field absent on CFD payload
                cfd_opens = max(cfd_opens, open_count(cfd["positions"], None))
            cfd_max = max(cfd_max, cfd_opens)
            sb_max = max(sb_max, sb_opens)
            if sb_opens > 0:
                sb_saw_open = True
            sb_h = sb["health"]
            sb_healthy = bool(
                sb_h.get("ok")
                or sb_h.get("status") in ("ok", "ready", "OPERATIONAL", "healthy")
                or sb_h.get("trade_ready")
            )

            if cfd_opens > CFD_HARD_CAP:
                cascades += 1
                log(f"CASCADE CFD opens={cfd_opens}>{CFD_HARD_CAP} — STOP+flatten")
                flatten_all(f"cascade_cfd_opens={cfd_opens}")
                RESULT.write_text(
                    json.dumps(
                        {
                            "status": "BLOCKED",
                            "reason": "cascade_cfd",
                            "cfd_opens": cfd_opens,
                            "sb_opens": sb_opens,
                            "cfd_max_opens": cfd_max,
                            "sb_max_opens": sb_max,
                            "cascades": cascades,
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                return 2
            if sb_opens > SB_HARD_CAP:
                cascades += 1
                log(f"CASCADE SB opens={sb_opens}>{SB_HARD_CAP} — STOP+flatten")
                flatten_all(f"cascade_sb_opens={sb_opens}")
                RESULT.write_text(
                    json.dumps(
                        {
                            "status": "BLOCKED",
                            "reason": "cascade_sb",
                            "cfd_opens": cfd_opens,
                            "sb_opens": sb_opens,
                            "cascades": cascades,
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                return 2

            profits, settled, streak = journal_profits(start)
            lanes = {r["lane"] for r in profits}
            styles = {r["style"] for r in profits}
            log(
                f"progress streak={streak}/{GOAL} profits={len(profits)} "
                f"settled={len(settled)} cfd_opens={cfd_opens} sb_opens={sb_opens} "
                f"cfd_max={cfd_max} sb_max={sb_max} cascades={cascades} "
                f"lanes={sorted(lanes)} styles={sorted(styles)} sb_healthy={sb_healthy}"
            )
            if settled:
                tail = settled[-3:]
                log(f"  recent={json.dumps(tail)}")

            if streak >= GOAL and cascades == 0 and cfd_max <= CFD_HARD_CAP:
                streak_rows = []
                for r in reversed(settled):
                    if r["profitable"]:
                        streak_rows.append(r)
                        if len(streak_rows) >= GOAL:
                            break
                    else:
                        break
                streak_rows = list(reversed(streak_rows))
                cfd_n = sum(1 for r in streak_rows if r["lane"] == "CFD")
                sb_n = sum(1 for r in streak_rows if r["lane"] == "SB")
                note_parts = [
                    f"{GOAL} consecutive profitable DIAAAA closes",
                    f"cfd_max={cfd_max} sb_max={sb_max}",
                ]
                if sb_n == 0:
                    note_parts.append(
                        "SB silent in streak — "
                        + (
                            "lane healthy/OPERATIONAL but no profitable close"
                            if sb_healthy
                            else "SB health unclear"
                        )
                        + ("; saw opens earlier" if sb_saw_open else "; no SB opens observed")
                    )
                payload = {
                    "status": "SUCCESS",
                    "milestone": f"{GOAL} consecutive profitable dual-engine closes",
                    "ended": datetime.now(tz=timezone.utc).isoformat(),
                    "streak": streak,
                    "streak_deals": streak_rows,
                    "all_profits": profits,
                    "cfd_max_opens": cfd_max,
                    "sb_max_opens": sb_max,
                    "cascades": cascades,
                    "lanes_in_streak": {"CFD": cfd_n, "SB": sb_n},
                    "styles_in_streak": sorted({r["style"] for r in streak_rows}),
                    "sb_healthy": sb_healthy,
                    "sb_saw_open": sb_saw_open,
                    "note": "; ".join(note_parts),
                }
                RESULT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                log(f"SUCCESS {json.dumps(payload['streak_deals'], indent=2)}")
                return 0
        except Exception as exc:
            log(f"monitor loop error: {type(exc).__name__}: {exc}")
        time.sleep(POLL)

    profits, settled, streak = journal_profits(start)
    RESULT.write_text(
        json.dumps(
            {
                "status": "TIMEOUT",
                "profits": profits,
                "settled": settled[-20:],
                "streak": streak,
                "cfd_max_opens": cfd_max,
                "sb_max_opens": sb_max,
                "cascades": cascades,
                "sb_healthy": sb_healthy,
                "sb_saw_open": sb_saw_open,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    log(f"TIMEOUT streak={streak} profits={len(profits)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
