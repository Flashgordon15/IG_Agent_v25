#!/usr/bin/env python3
"""Soak monitor: 5 profitable real closes (RealizedPnL_GBP > 0).

Success bar:
  - real DIAAAA* DealIDs
  - journal RealizedPnL_GBP > 0
  - cascades == 0 (CFD opens never > 1)
  - prefer consecutive; minimum = 5 profitable total in window
"""
from __future__ import annotations

import csv
import json
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


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
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


def journal_profits(start_epoch: float):
    if not JOURNAL.is_file():
        return [], 0
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
        settled.append(
            {
                "deal": deal,
                "pnl": pnl,
                "account": str(row.get("AccountID") or ""),
                "product": str(row.get("ProductType") or ""),
                "origin": str(row.get("EngineOrigin") or ""),
                "direction": str(row.get("Direction") or ""),
                "ts": str(row.get("Timestamp") or ""),
                "ts_epoch": ts,
                "entry": str(row.get("EntryPrice") or ""),
                "exit": str(row.get("ExitPrice") or ""),
                "profitable": pnl > 0,
            }
        )
    settled.sort(key=lambda r: r["ts_epoch"])
    profits = [r for r in settled if r["profitable"]]
    # trailing consecutive profitable streak
    streak = 0
    for r in reversed(settled):
        if r["profitable"]:
            streak += 1
        else:
            break
    return profits, settled, streak


def cfd_open_count(pos_body: dict) -> int:
    try:
        rows = pos_body.get("positions") or pos_body.get("rows") or []
        n = 0
        for r in rows:
            epic = str(
                r.get("epic")
                or (r.get("market") or {}).get("epic")
                or ""
            )
            acct = str(r.get("account_id") or r.get("AccountID") or "")
            # CFD lane account
            if "Z6BAH4" in acct or "CFD" in str(r.get("product_type") or "").upper():
                n += 1
            elif not acct and epic:
                n += 1  # conservatively count unnamed as open risk
        sot = int((pos_body.get("broker_open_sot") or {}).get("count") or 0)
        ts = int((pos_body.get("trade_support") or {}).get("broker_open") or 0)
        return max(int(pos_body.get("count") or 0), sot, ts, n)
    except Exception:
        return int(pos_body.get("count") or 0)


def flatten_all(reason: str) -> None:
    log(f"CASCADE/STOP flatten reason={reason}")
    try:
        import subprocess
        import os

        env = os.environ.copy()
        env["CORE_DETACHED"] = "FALSE"
        env["APP_MODE"] = "DEMO"
        env["IG_AGENT_CONFIG"] = "config/config_v31_demo_throughput.json"
        env["PYTHONPATH"] = str(ROOT / "src")
        subprocess.run(
            [
                str(ROOT / ".venv/bin/python3"),
                str(ROOT / "scripts/emergency_book_unwind.py"),
                "--yes",
            ],
            cwd=str(ROOT),
            env=env,
            timeout=120,
            check=False,
        )
    except Exception as e:
        log(f"flatten failed: {e}")


def main() -> int:
    start = time.time()
    SESSION.parent.mkdir(parents=True, exist_ok=True)
    SESSION.write_text(
        json.dumps(
            {
                "start_epoch": start,
                "start_iso": datetime.now(tz=timezone.utc).isoformat(),
                "goal": GOAL,
                "mode": "profitable",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    log(f"SOAK START goal={GOAL} profitable closes (DIAAAA* RealizedPnL_GBP>0)")
    cfd_max = 0
    cascades = 0

    while time.time() - start < MAX_SEC:
        pos = get("http://127.0.0.1:8080/api/positions/live")
        opens = cfd_open_count(pos if isinstance(pos, dict) else {})
        cfd_max = max(cfd_max, opens)
        if opens > 1:
            cascades += 1
            log(f"CASCADE opens={opens} — STOP+flatten")
            flatten_all(f"cascade_opens={opens}")
            RESULT.write_text(
                json.dumps(
                    {
                        "status": "BLOCKED",
                        "reason": "cascade",
                        "opens": opens,
                        "cfd_max_opens": cfd_max,
                        "cascades": cascades,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            return 2

        profits, settled, streak = journal_profits(start)
        log(
            f"progress profits={len(profits)}/{GOAL} streak={streak} "
            f"settled={len(settled)} opens={opens} cfd_max={cfd_max} cascades={cascades}"
        )
        if profits:
            log(f"  profits={json.dumps(profits)}")

        if len(profits) >= GOAL and cascades == 0 and cfd_max <= 1:
            payload = {
                "status": "SUCCESS",
                "milestone": f"{GOAL} profitable real live closes",
                "ended": datetime.now(tz=timezone.utc).isoformat(),
                "streak": streak,
                "profits": profits[:GOAL] if streak < GOAL else profits[-GOAL:],
                "all_profits": profits,
                "cfd_max_opens": cfd_max,
                "cascades": cascades,
                "note": (
                    f"{GOAL} profitable DIAAAA closes; "
                    f"trailing_streak={streak}; cfd_max={cfd_max}"
                ),
            }
            RESULT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            log(f"SUCCESS {json.dumps(payload['profits'], indent=2)}")
            return 0

        time.sleep(POLL)

    profits, settled, streak = journal_profits(start)
    RESULT.write_text(
        json.dumps(
            {
                "status": "TIMEOUT",
                "profits": profits,
                "settled": settled,
                "streak": streak,
                "cfd_max_opens": cfd_max,
                "cascades": cascades,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    log(f"TIMEOUT profits={len(profits)} streak={streak}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
