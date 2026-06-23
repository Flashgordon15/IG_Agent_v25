#!/usr/bin/env python3
"""
Embedded 30-minute cockpit + trading session monitor.

Polls health, SHM linkage, gate diagnostics, and trade ledger; writes JSONL samples
and a final markdown report when no trades occur.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

EPIC_LABEL = {
    "CS.D.CFPGOLD.CFP.IP": "Gold",
    "IX.D.DOW.IFM.IP": "Wall St",
    "IX.D.NIKKEI.IFM.IP": "Nikkei",
    "CS.D.EURUSD.CFD.IP": "EUR/USD",
}

FULFILLMENT_URL = "http://127.0.0.1:8080/api/unified/fulfillment"
HEALTH_URL = "http://127.0.0.1:8080/api/health"


def _get(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=4) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _sample() -> dict[str, Any]:
    ts = datetime.now(timezone.utc).isoformat()
    row: dict[str, Any] = {"ts": ts}
    try:
        row["health"] = _get(HEALTH_URL)
    except Exception as exc:
        row["health_error"] = str(exc)
        return row
    try:
        row["fulfillment"] = _get(FULFILLMENT_URL)
    except Exception as exc:
        row["fulfillment_error"] = str(exc)
    try:
        from system.ipc.cockpit_shm_passive import read_cockpit_shm

        shm = read_cockpit_shm()
        row["shm"] = (
            {
                "link_state": shm.get("link_state"),
                "publisher_alive": shm.get("publisher_alive"),
                "agent_pid": shm.get("agent_pid"),
                "ticks_cached": shm.get("ticks_cached"),
                "write_seq": shm.get("write_seq"),
                "valve_status": shm.get("valve_status"),
            }
            if shm
            else None
        )
    except Exception as exc:
        row["shm_error"] = str(exc)
    return row


def _epic_snapshot(fulfillment: dict[str, Any]) -> dict[str, dict[str, Any]]:
    ft = fulfillment.get("alpha_frontier_tracker") or {}
    by = ft.get("by_epic") or {}
    if not by:
        gd = fulfillment.get("gate_diagnostics") or {}
        by = gd.get("by_epic") or {}
    out: dict[str, dict[str, Any]] = {}
    for epic, r in sorted(by.items()):
        zone = r.get("zone_label")
        if not zone:
            wait = str(r.get("wait_reason") or "")
            zone = (
                "WIN_ZONE"
                if r.get("all_passed")
                else "FAIL_ZONE"
                if "miss" in wait or "FAIL" in wait
                else "SCANNING"
            )
        out[epic] = {
            "label": EPIC_LABEL.get(epic, epic),
            "zone": zone,
            "direction": r.get("direction"),
            "wait_reason": r.get("wait_reason"),
            "all_passed": r.get("all_passed"),
            "injecting": r.get("injecting"),
        }
    return out


def _build_report(samples: list[dict[str, Any]], log_path: Path) -> str:
    lines: list[str] = []
    lines.append("# Cockpit Session Monitor Report")
    lines.append("")
    if not samples:
        lines.append("No samples collected.")
        return "\n".join(lines)

    start = samples[0].get("ts", "?")
    end = samples[-1].get("ts", "?")
    lines.append(f"- Window: {start} → {end}")
    lines.append(f"- Samples: {len(samples)}")
    lines.append("")

    # Agent uptime
    health_ok = sum(1 for s in samples if s.get("health", {}).get("agent_pid"))
    lines.append(f"## Agent availability")
    lines.append(f"- Health OK samples: {health_ok}/{len(samples)}")
    last_h = samples[-1].get("health") or {}
    lines.append(
        f"- Final PID: {last_h.get('agent_pid')} ready="
        f"{(last_h.get('boot_metrics') or {}).get('ready')}"
    )
    lines.append("")

    # SHM
    shm_live = sum(
        1
        for s in samples
        if (s.get("shm") or {}).get("link_state") == "LIVE"
    )
    shm_stale = sum(
        1
        for s in samples
        if (s.get("shm") or {}).get("link_state") == "STALE_SHM"
    )
    lines.append("## SHM linkage")
    lines.append(f"- LIVE samples: {shm_live}/{len(samples)}")
    lines.append(f"- STALE samples: {shm_stale}/{len(samples)}")
    lines.append("")

    # Trades
    trade_count = 0
    perf_rows: list[dict[str, Any]] = []
    for s in samples:
        ful = s.get("fulfillment") or {}
        rows = ful.get("performance_rows") or []
        if len(rows) > trade_count:
            trade_count = len(rows)
            perf_rows = list(rows)
    lines.append("## Trades")
    lines.append(f"- Performance rows at end: {trade_count}")
    if perf_rows:
        for r in perf_rows[-5:]:
            lines.append(
                f"  - {r.get('executed_at')} {r.get('epic')} {r.get('action')} "
                f"{r.get('status')} pnl={r.get('pnl_gbp')}"
            )
    lines.append("")

    # Per-epic zone / block reasons
    zone_counts: dict[str, Counter] = defaultdict(Counter)
    wait_counts: dict[str, Counter] = defaultdict(Counter)
    win_zone_samples: dict[str, int] = defaultdict(int)

    for s in samples:
        ful = s.get("fulfillment") or {}
        for epic, snap in _epic_snapshot(ful).items():
            z = str(snap.get("zone") or "UNKNOWN")
            zone_counts[epic][z] += 1
            w = str(snap.get("wait_reason") or "—")
            if w and w != "—":
                wait_counts[epic][w] += 1
            if z == "WIN_ZONE":
                win_zone_samples[epic] += 1

    lines.append("## Per-market activity")
    for epic in sorted(zone_counts.keys()):
        label = EPIC_LABEL.get(epic, epic)
        lines.append(f"### {label} (`{epic}`)")
        lines.append(f"- WIN_ZONE samples: {win_zone_samples.get(epic, 0)}/{len(samples)}")
        lines.append("- Zone distribution:")
        for z, n in zone_counts[epic].most_common():
            lines.append(f"  - {z}: {n}")
        if wait_counts[epic]:
            lines.append("- Top block reasons:")
            for reason, n in wait_counts[epic].most_common(5):
                lines.append(f"  - ({n}x) {reason}")
        lines.append("")

    # Engine log excerpts
    if log_path.is_file():
        lines.append("## Engine log (frontier / execution)")
        try:
            text = log_path.read_text(errors="replace")
            hits = [
                ln.strip()
                for ln in text.splitlines()
                if any(
                    k in ln
                    for k in (
                        "frontier dispatch failed",
                        "WIN_ZONE",
                        "execution:",
                        "TypeError",
                        "order rejected",
                        "REST budget",
                    )
                )
            ]
            for ln in hits[-20:]:
                lines.append(f"- `{ln[:240]}`")
        except Exception as exc:
            lines.append(f"- log read error: {exc}")
        lines.append("")

    # Diagnosis
    lines.append("## Diagnosis (no-trade root causes)")
    if trade_count > 0:
        lines.append("- Trades occurred during window — no no-trade diagnosis required.")
    else:
        causes: list[str] = []
        if health_ok < len(samples) * 0.9:
            causes.append("Agent was offline or unstable for part of the window.")
        if shm_live < len(samples) * 0.5 and health_ok > len(samples) * 0.5:
            causes.append("SHM not publishing despite healthy API — cockpit linkage issue.")
        if not any(win_zone_samples.values()):
            causes.append(
                "No market reached WIN_ZONE — matrix cells empty or gates failing "
                "(Nikkei/EUR/USD need prebaker fill; check signal thresholds)."
            )
        else:
            for epic, n in win_zone_samples.items():
                if n > 0:
                    top_waits = wait_counts[epic].most_common(3)
                    label = EPIC_LABEL.get(epic, epic)
                    if top_waits:
                        causes.append(
                            f"{label} hit WIN_ZONE {n}x but blocked: "
                            + "; ".join(f"{w} ({c}x)" for w, c in top_waits)
                        )
                    else:
                        causes.append(
                            f"{label} hit WIN_ZONE {n}x with no explicit wait_reason "
                            "(check execution path / demo routing)."
                        )
        if not causes:
            causes.append("All markets SCANNING/FAIL — normal quiet session or threshold too high.")
        for c in causes:
            lines.append(f"- {c}")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="30-min embedded session monitor")
    parser.add_argument("--minutes", type=float, default=30.0)
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "src/data/logs/cockpit_monitor",
    )
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    jsonl_path = args.out_dir / f"session_{stamp}.jsonl"
    report_path = args.out_dir / f"report_{stamp}.md"
    log_path = ROOT / "src/data/logs/engine.log"

    deadline = time.monotonic() + args.minutes * 60.0
    samples: list[dict[str, Any]] = []
    print(f"MONITOR: {args.minutes}min every {args.interval}s → {jsonl_path}", flush=True)

    while time.monotonic() < deadline:
        sample = _sample()
        samples.append(sample)
        with jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(sample, default=str) + "\n")
        h = sample.get("health") or {}
        shm = sample.get("shm") or {}
        ful = sample.get("fulfillment") or {}
        rows = len(ful.get("performance_rows") or [])
        epics = _epic_snapshot(ful)
        win = sum(1 for e in epics.values() if e.get("zone") == "WIN_ZONE")
        print(
            f"[{sample.get('ts')}] pid={h.get('agent_pid')} shm={shm.get('link_state')} "
            f"trades={rows} win_zones={win}/{len(epics)}",
            flush=True,
        )
        sleep_for = min(args.interval, max(0, deadline - time.monotonic()))
        if sleep_for > 0:
            time.sleep(sleep_for)

    report = _build_report(samples, log_path)
    report_path.write_text(report, encoding="utf-8")
    print(f"\nREPORT written: {report_path}", flush=True)
    print(report, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
