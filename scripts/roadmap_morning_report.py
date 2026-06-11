#!/usr/bin/env python3
"""Morning roadmap snapshot + Telegram delta summary (07:05 local via launchd)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_LONDON = ZoneInfo("Europe/London")
OUT_DIR = ROOT / "docs" / "morning"


def _configure_telegram() -> None:
    from system.config import Config
    from system.config_loader import _sync_operating_mode_from_credentials
    from system.config_validator import apply_config_defaults
    from system.paths import config_dir
    from system.telegram_notifier import configure_telegram

    cfg_path = config_dir() / "config_v25.json"
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    merged = apply_config_defaults(raw)
    _sync_operating_mode_from_credentials(merged)
    configure_telegram(Config(_data=merged))


def _yesterday_iso(day: str) -> str:
    dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (dt - timedelta(days=1)).strftime("%Y-%m-%d")


def _find_prev_row(payload: dict[str, object]) -> dict[str, object] | None:
    day = str(payload.get("day") or "")
    if not day:
        return None
    target = _yesterday_iso(day)
    for row in reversed(list(payload.get("history") or [])):
        if str(row.get("day") or "") == target:
            return row
    return None


def _delta(cur: int | float, prev: int | float | None) -> str:
    if prev is None:
        return ""
    d = int(round(float(cur) - float(prev)))
    if d > 0:
        return f"+{d}"
    if d < 0:
        return str(d)
    return "±0"


def _section_map(row: dict[str, object] | None) -> dict[str, int]:
    if not row:
        return {}
    out: dict[str, int] = {}
    for sec in row.get("sections") or []:
        sid = str(sec.get("id") or "")
        if sid:
            out[sid] = int(sec.get("pct") or 0)
    return out


def format_telegram_summary(
    payload: dict[str, object], prev: dict[str, object] | None
) -> str:
    now_local = datetime.now(_LONDON).strftime("%H:%M BST")
    overall = int(payload.get("overall_pct") or 0)
    milestone = str(payload.get("milestone") or "M0")
    delta = _delta(overall, prev.get("overall_pct") if prev else None)
    delta_line = f" ({delta} vs yesterday)" if delta else ""

    cur_secs = _section_map(payload)
    prev_secs = _section_map(prev)
    sec_bits: list[str] = []
    for sid, title in (
        ("certification", "Cert"),
        ("edge", "Edge"),
        ("coverage", "Cov"),
        ("flow", "Flow"),
    ):
        pct = cur_secs.get(sid, 0)
        d = _delta(pct, prev_secs.get(sid))
        sec_bits.append(f"{title} {pct}%" + (f" ({d})" if d else ""))

    prof = payload.get("profitability_14d") or {}
    feeder = payload.get("feeder_today") or {}
    gates = payload.get("gate_blockers_7d") or {}
    top = list(gates.get("top") or [])
    top_gate = str(top[0].get("gate") or "none") if top else "none"
    top_pct = float(top[0].get("pct") or 0) if top else 0.0

    relax = payload.get("relaxation") or {}
    soak = bool(relax.get("demo_soak_mode") or relax.get("demo_soak_enabled"))
    net = float(prof.get("net_gbp") or 0)
    wr = float(prof.get("wr_pct") or 0)
    tr = int(feeder.get("trade_ready") or 0)
    intents = int(feeder.get("order_intents") or 0)

    lines = [
        f"📊 Roadmap {now_local} · {overall}%{delta_line} · {milestone}",
        " · ".join(sec_bits),
        f"Blocker: {top_gate} {top_pct:.0f}% · Soak {'ON' if soak else 'OFF'}",
        f"Today: trade_ready {tr} · intents {intents}",
        f"14d: £{net:+.2f} · WR {wr:.0f}%",
    ]
    return "\n".join(lines)


def format_markdown_section(
    payload: dict[str, object], prev: dict[str, object] | None
) -> list[str]:
    overall = int(payload.get("overall_pct") or 0)
    milestone = str(payload.get("milestone") or "M0")
    delta = _delta(overall, prev.get("overall_pct") if prev else None)
    delta_txt = f" ({delta} vs yesterday)" if delta else ""

    lines = [
        "## Roadmap progress (£1k/day cert)",
        "",
        f"- **Overall:** {overall}%{delta_txt} · milestone **{milestone}**",
        "",
        "| Section | Today | Δ vs yesterday |",
        "|---------|-------|----------------|",
    ]
    cur_secs = _section_map(payload)
    prev_secs = _section_map(prev)
    titles = {
        "certification": "Certification",
        "edge": "Edge & ML",
        "coverage": "Coverage",
        "flow": "Trading flow",
    }
    for sid, title in titles.items():
        pct = cur_secs.get(sid, 0)
        d = _delta(pct, prev_secs.get(sid))
        d_cell = d if d else "—"
        lines.append(f"| {title} | {pct}% | {d_cell} |")

    prof = payload.get("profitability_14d") or {}
    feeder = payload.get("feeder_today") or {}
    lines.extend(
        [
            "",
            f"- 14d net: **£{float(prof.get('net_gbp') or 0):+.2f}** · "
            f"WR **{float(prof.get('wr_pct') or 0):.1f}%** · "
            f"trades **{int(prof.get('trades') or 0)}**",
            f"- Today: trade_ready **{int(feeder.get('trade_ready') or 0)}** · "
            f"intents **{int(feeder.get('order_intents') or 0)}** · "
            f"closes **{int(feeder.get('fill_closes') or 0)}**",
            "",
        ]
    )
    return lines


def send_telegram_summary(text: str) -> bool:
    from system.telegram_notifier import get_telegram_notifier

    notifier = get_telegram_notifier()
    if notifier is None or not notifier.enabled:
        print("Telegram disabled — skipped morning roadmap message")
        return False
    ok = notifier.send_now(text)
    print("Telegram sent" if ok else "Telegram send failed")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Morning roadmap snapshot + Telegram")
    parser.add_argument(
        "--no-telegram",
        action="store_true",
        help="Skip Telegram (snapshot + markdown only)",
    )
    parser.add_argument(
        "--no-markdown",
        action="store_true",
        help="Skip writing docs/morning/ROADMAP_*.md",
    )
    args = parser.parse_args()

    from api.roadmap_progress import build_roadmap_progress

    payload = build_roadmap_progress(history_days=14, write_snapshot=True)
    prev = _find_prev_row(payload)

    out = ROOT / "data_lake" / "state" / "roadmap_progress_latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    day = str(payload.get("day") or datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    overall = payload.get("overall_pct")
    print(f"Roadmap overall: {overall}% · milestone {payload.get('milestone')}")
    print(f"Wrote {out}")

    if not args.no_markdown:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        md_path = OUT_DIR / f"ROADMAP_{day}.md"
        body = [
            f"# Roadmap Morning — {day}",
            "",
            f"Generated: {datetime.now(_LONDON).strftime('%Y-%m-%d %H:%M:%S %Z')}",
            "",
        ]
        body.extend(format_markdown_section(payload, prev))
        body.append("---")
        body.append("*Auto-generated by scripts/roadmap_morning_report.py*")
        md_path.write_text("\n".join(body) + "\n", encoding="utf-8")
        print(f"Wrote {md_path}")

    if not args.no_telegram:
        _configure_telegram()
        text = format_telegram_summary(payload, prev)
        send_telegram_summary(text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
