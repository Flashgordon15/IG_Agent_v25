#!/usr/bin/env python3
"""Daily operator digest — plain-English morning briefing (~07:30 local via launchd).

Writes:
  docs/morning/DAILY_DIGEST_YYYY-MM-DD.md  (archive)
  docs/morning/DAILY_DIGEST_LATEST.md      (always open this one)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

_LONDON = ZoneInfo("Europe/London")
OUT_DIR = ROOT / "docs" / "morning"

SESSION_PLAN = [
    ("00:00–06:59", "asia_early", "Japan 225"),
    ("07:00–11:59", "london_morning", "Gold, EUR/USD, GBP/USD"),
    ("12:00–15:59", "london_us_overlap", "All except Japan (peak liquidity)"),
    ("16:00–21:59", "us_afternoon", "US indices, oil, FX, gold"),
    ("22:00+", "late", "Flat — no new entries"),
]


def _now_local() -> datetime:
    return datetime.now(_LONDON)


def _read_engine_tail(n: int = 8000) -> str:
    p = ROOT / "src" / "data" / "logs" / "engine.log"
    if not p.is_file():
        return ""
    try:
        return "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def _overnight_stats(tail: str) -> dict[str, int]:
    return {
        "gates_passed": len(re.findall(r"ALL GATES PASSED", tail)),
        "orders_submitted": len(re.findall(r"action=SUBMITTED", tail)),
        "trades_closed": tail.count("TRADE CLOSED"),
        "stale_quotes": len(re.findall(r"Quote stream stale", tail)),
    }


def _agent_health() -> dict:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/api/health", timeout=5) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return {"ok": False, "reachable": False}


def _cert_one_liner() -> str:
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "certify_ladder.py")],
            cwd=str(ROOT),
            env={**dict(__import__("os").environ), "PYTHONPATH": "src:v26"},
            capture_output=True,
            text=True,
            timeout=120,
        )
        lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
        return " · ".join(lines[:3]) if lines else "(cert ladder unavailable)"
    except (subprocess.TimeoutExpired, OSError):
        return "(cert ladder timed out)"


def _trade_forecast(prof: dict, feeder: dict, soak: bool, agent_up: bool) -> list[str]:
    trades_14 = int(prof.get("trades") or 0)
    daily_avg = round(trades_14 / 14, 1) if trades_14 else 0
    lines = [
        f"- **Baseline:** ~{daily_avg} closes/day from 14d ledger (recent active days were 8–10).",
    ]
    if not agent_up:
        lines.append("- **Today (agent down):** expect **0 trades** until main.py is running.")
        return lines
    if soak:
        lines.append(
            "- **With demo soak + agent up:** realistic **4–10 closes** if sessions fire "
            "(Japan morning + US afternoon historically busiest)."
        )
    else:
        lines.append(
            "- **Without soak:** fewer probes; rotation/fitness gates may cap flow."
        )
    tr = int(feeder.get("trade_ready") or 0)
    intents = int(feeder.get("order_intents") or 0)
    if tr > 0 and intents == 0:
        lines.append(
            f"- **Watch:** {tr} trade-ready evals but **0 order intents** — restart agent "
            "if this persists into the first session window."
        )
    return lines


def build_digest(*, write_snapshot: bool = True) -> tuple[dict, list[str]]:
    from api.roadmap_progress import build_roadmap_progress
    from roadmap_morning_report import _find_prev_row, format_markdown_section

    payload = build_roadmap_progress(history_days=14, write_snapshot=write_snapshot)
    prev = _find_prev_row(payload)

    out = ROOT / "data_lake" / "state" / "roadmap_progress_latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    now = _now_local()
    day = str(payload.get("day") or now.strftime("%Y-%m-%d"))
    prof = payload.get("profitability_14d") or {}
    feeder = payload.get("feeder_today") or {}
    relax = payload.get("relaxation") or {}
    soak = bool(relax.get("demo_soak_mode"))
    overall = int(payload.get("overall_pct") or 0)
    milestone = str(payload.get("milestone") or "M0")

    prev_overall = int(prev.get("overall_pct") or 0) if prev else None
    delta = overall - prev_overall if prev_overall is not None else None
    delta_txt = f" ({delta:+d} vs yesterday)" if delta is not None else ""

    health = _agent_health()
    agent_up = bool(health.get("ok"))
    overnight = _overnight_stats(_read_engine_tail())
    cert = _cert_one_liner()

    ranked = list((payload.get("gate_blockers_7d") or {}).get("top") or [])
    top_gate = str(ranked[0].get("gate") or "none") if ranked else "none"
    top_pct = float(ranked[0].get("pct") or 0) if ranked else 0.0

    try:
        from data.ml_training_store import MLTrainingStore

        ml_rows = int(MLTrainingStore().record_count())
    except Exception:
        ml_rows = 0

    lines: list[str] = [
        f"# Daily Operator Digest — {day}",
        "",
        f"*Generated {now.strftime('%A %d %b %Y, %H:%M %Z')}*",
        "",
        "## At a glance",
        "",
        f"| | |",
        f"|---|---|",
        f"| Roadmap progress | **{overall}%**{delta_txt} · milestone **{milestone}** |",
        f"| Agent running | **{'Yes' if agent_up else 'No — start before sessions'}** |",
        f"| Demo soak | **{'ON' if soak else 'OFF'}** |",
        f"| 14d P&L | **£{float(prof.get('net_gbp') or 0):+.2f}** · WR **{float(prof.get('wr_pct') or 0):.1f}%** · {int(prof.get('trades') or 0)} closes |",
        f"| Today (feeder) | trade_ready **{int(feeder.get('trade_ready') or 0)}** · intents **{int(feeder.get('order_intents') or 0)}** · closes **{int(feeder.get('fill_closes') or 0)}** |",
        f"| ML training rows | **{ml_rows}** (target 500+) |",
        f"| Top gate blocker (7d) | **{top_gate}** ({top_pct:.0f}%) |",
        "",
        "## Overnight (recent engine.log tail)",
        "",
        f"- Gates passed: **{overnight['gates_passed']}**",
        f"- Orders submitted: **{overnight['orders_submitted']}**",
        f"- Trades closed (log): **{overnight['trades_closed']}**",
        f"- Stale quote blocks: **{overnight['stale_quotes']}**",
        "",
        "## Certification",
        "",
        cert,
        "",
        "## Today's session map (BST)",
        "",
        "| Window | Session | Markets |",
        "|--------|---------|---------|",
    ]
    for window, session, markets in SESSION_PLAN:
        lines.append(f"| {window} | `{session}` | {markets} |")
    lines.extend(["", "## Trade outlook", ""])
    lines.extend(_trade_forecast(prof, feeder, soak, agent_up))
    lines.extend(["", *format_markdown_section(payload, prev)])
    lines.extend(
        [
            "## If you only have 2 minutes",
            "",
            "1. **Agent up?** Dashboard → Live tab, or `curl -s localhost:8080/api/health`.",
            "2. **First session window:** Japan from ~00:00 BST; London FX/Gold from 07:00; overlap 12:00.",
            "3. **Check intents > 0** during a session — if trade_ready > 0 but intents stay 0, restart once.",
            "4. Read full archive: `docs/morning/DAILY_DIGEST_LATEST.md`",
            "",
            "---",
            "*Scheduled job: `com.igagent.v29digest` · `scripts/daily_operator_digest.py`*",
        ]
    )
    return payload, lines


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily operator digest (07:30)")
    parser.add_argument(
        "--no-snapshot",
        action="store_true",
        help="Skip writing roadmap history snapshot",
    )
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    _, lines = build_digest(write_snapshot=not args.no_snapshot)

    day = _now_local().strftime("%Y-%m-%d")
    dated = OUT_DIR / f"DAILY_DIGEST_{day}.md"
    latest = OUT_DIR / "DAILY_DIGEST_LATEST.md"
    body = "\n".join(lines) + "\n"
    dated.write_text(body, encoding="utf-8")
    latest.write_text(body, encoding="utf-8")

    print(f"Wrote {dated}")
    print(f"Wrote {latest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
