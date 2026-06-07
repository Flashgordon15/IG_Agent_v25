#!/usr/bin/env python3
"""Generate overnight + v26 progress morning report."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.append(str(ROOT / "v26"))

OUT_DIR = ROOT / "docs" / "morning"


def _read_engine_tail(n: int = 5000) -> str:
    p = ROOT / "src" / "data" / "logs" / "engine.log"
    if not p.is_file():
        return ""
    try:
        return "\n".join(
            p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        )
    except OSError:
        return ""


def _overnight_stats(tail: str) -> dict:
    submitted = len(re.findall(r"action=SUBMITTED", tail))
    all_pass = len(re.findall(r"ALL GATES PASSED", tail))
    fill_close = tail.count("TRADE CLOSED")
    stale = len(re.findall(r"Quote stream stale", tail))
    market_closed = len(re.findall(r"market closed", tail))
    return {
        "gates_passed_attempts": all_pass,
        "orders_submitted": submitted,
        "trades_closed_log": fill_close,
        "stale_quote_blocks": stale,
        "market_closed_blocks": market_closed,
    }


def _health() -> dict:
    try:
        import urllib.request

        with urllib.request.urlopen(
            "http://localhost:8080/api/health", timeout=10
        ) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main() -> int:
    from expectancy.engine import (
        collect_fills,
        compute_setup_stats,
        portfolio_summary,
        write_snapshot,
    )
    from ingest.lake_reader import summarize_day

    day_local = datetime.now().strftime("%Y-%m-%d")
    day_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"MORNING_REPORT_{day_local}.md"

    health = _health()
    v25_sum = summarize_day(day_utc)
    shadow_path = ROOT / "data_lake" / "shadow_v26" / f"{day_utc}.jsonl"
    shadow_lines = 0
    if shadow_path.is_file():
        shadow_lines = sum(1 for _ in shadow_path.open() if _.strip())

    tail = _read_engine_tail()
    overnight = _overnight_stats(tail)

    fills = collect_fills(days=3)
    pf = portfolio_summary(fills)
    setups = compute_setup_stats(fills)[:8]
    try:
        snap_path = write_snapshot(days=14)
    except Exception:
        snap_path = None

    # shadow compare inline
    sc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "shadow_compare.py"), "--day", day_utc],
        cwd=str(ROOT),
        env={
            **dict(__import__("os").environ),
            "PYTHONPATH": f"{ROOT / 'src'}:{ROOT / 'v26'}",
        },
        capture_output=True,
        text=True,
    )

    lines = [
        f"# Morning Report — {day_local}",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} local",
        "",
        "## Overnight status",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Agent health OK | {health.get('ok', False)} |",
        f"| Trading healthy | {health.get('trading_healthy', 'n/a')} |",
        f"| Quotes fresh | {health.get('quotes_fresh', 'n/a')} ({health.get('quotes_fresh_count', 0)}/{health.get('quotes_total', 4)}) |",
        f"| Points / issues | {health.get('issues', [])} |",
        f"| Feeder events (UTC {day_utc}) | {v25_sum.total_events} |",
        f"| v26 shadow intents | {shadow_lines} |",
        f"| Gate pass → trade attempts | {overnight['gates_passed_attempts']} |",
        f"| Orders SUBMITTED (recent log) | {overnight['orders_submitted']} |",
        f"| Trades closed (log lines) | {overnight['trades_closed_log']} |",
        "",
        "## P&L (rolling fills from feeder)",
        "",
        f"- Trades: **{pf['n']}** | WR: **{pf['wr']:.1%}** | E£/trade: **{pf['e_gbp']:+.2f}** | Total: **£{pf['total_pnl_gbp']:+.2f}**",
        "",
    ]
    if setups:
        lines.append("### Top setups")
        lines.append("")
        for s in setups:
            lines.append(
                f"- `{s.setup_key[:50]}` — n={s.n} E£={s.e_gbp:+.2f} WR={s.wr:.0%} [{s.status}]"
            )
        lines.append("")

    lines.extend(
        [
            "## v25 vs v26 shadow",
            "",
            "```",
            (sc.stdout or sc.stderr or "(no output)").strip(),
            "```",
            "",
            "## Path to v26 (next steps)",
            "",
            "1. **This week** — Keep v25 feeder running; daily `shadow_compare --process --expectancy`",
            "2. **Phase 2** — Independent S2 momentum + FX strategies in shadow",
            "3. **Phase 3** — Portfolio allocator + certification replay (L1)",
            "4. **Phase 4** — Demo L5 soak (10/14 days ≥ £1k) before live cash",
            "",
            "## Recommendations toward £1,000/day",
            "",
            "| Priority | Action |",
            "|----------|--------|",
            "| P0 | Prove edge in data — ban setups with negative E£ (expectancy engine)",
            "| P0 | Japan asia_early only — threshold 85% is working as designed",
            "| P1 | Expand to 6–8 epics after replay WR ≥ 52% each",
            "| P1 | ML veto mode (block low-prob) not blend — deploy in v26 shadow first",
            "| P2 | £50k capital envelope + 12–18 qualified trades/day at £50–70 E£",
            "| P2 | Certification ladder L1→L5 — no live scale without L5 demo pass",
            "| P3 | Live micro at 25% size after L5; measure slippage vs demo",
            "",
            f"Expectancy snapshot: `{snap_path}`" if snap_path else "",
            "",
            "---",
            "*Auto-generated by scripts/morning_report_v26.py*",
        ]
    )

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
