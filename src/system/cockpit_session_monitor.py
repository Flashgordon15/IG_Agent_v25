"""In-process cockpit + trading session monitor (embedded after G5 ready)."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.paths import logs_dir, project_root

_MONITOR_THREAD: threading.Thread | None = None
_MONITOR_STOP = threading.Event()

EPIC_LABEL = {
    "CS.D.CFPGOLD.CFP.IP": "Gold",
    "IX.D.DOW.IFM.IP": "Wall St",
    "IX.D.NIKKEI.IFM.IP": "Nikkei",
    "CS.D.EURUSD.CFD.IP": "EUR/USD",
}

FULFILLMENT_URL = "http://127.0.0.1:8080/api/unified/fulfillment"
HEALTH_URL = "http://127.0.0.1:8080/api/health"


def _get(url: str) -> dict[str, Any]:
    if os.environ.get("IG_AGENT_IN_PROCESS", "").strip() == "1":
        if "fulfillment" in url:
            from system.unified_fulfillment_cache import get_fulfillment_payload

            payload = get_fulfillment_payload()
            return payload if isinstance(payload, dict) else {}
        if "/api/health" in url:
            from api.agent_health import build_health_status

            payload = build_health_status()
            return payload if isinstance(payload, dict) else {}
    with urllib.request.urlopen(url, timeout=4) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _epic_rows(fulfillment: dict[str, Any]) -> dict[str, dict[str, Any]]:
    ft = fulfillment.get("alpha_frontier_tracker") or {}
    by = ft.get("by_epic") or {}
    if not by:
        gd = fulfillment.get("gate_diagnostics") or {}
        by = gd.get("by_epic") or {}
    out: dict[str, dict[str, Any]] = {}
    for epic, r in sorted(by.items()):
        zone = r.get("zone_label")
        if not zone and r.get("all_passed"):
            zone = "WIN_ZONE"
        elif not zone:
            wait = str(r.get("wait_reason") or "")
            zone = "FAIL_ZONE" if "FAIL" in wait or "miss" in wait else "SCANNING"
        out[epic] = {
            "label": EPIC_LABEL.get(epic, epic),
            "zone": zone,
            "direction": r.get("direction"),
            "wait_reason": r.get("wait_reason"),
            "all_passed": r.get("all_passed"),
        }
    return out


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
            }
            if shm
            else None
        )
    except Exception as exc:
        row["shm_error"] = str(exc)
    try:
        from intelligence.matrix_prebaker import matrix_compiler_telemetry

        row["matrix"] = matrix_compiler_telemetry()
    except Exception as exc:
        row["matrix_error"] = str(exc)
    return row


def _build_report(samples: list[dict[str, Any]]) -> str:
    lines = ["# Cockpit Session Monitor Report", ""]
    if not samples:
        return "\n".join(lines + ["No samples."])

    lines.append(f"- Window: {samples[0].get('ts')} → {samples[-1].get('ts')}")
    lines.append(f"- Samples: {len(samples)}")
    lines.append("")

    trade_count = 0
    for s in samples:
        ful = s.get("fulfillment") or {}
        trade_count = max(trade_count, len(ful.get("performance_rows") or []))
    lines.append(f"## Trades: {trade_count}")
    lines.append("")

    zone_counts: dict[str, Counter] = defaultdict(Counter)
    wait_counts: dict[str, Counter] = defaultdict(Counter)
    win_hits: dict[str, int] = defaultdict(int)
    matrix_ok = 0

    for s in samples:
        mat = s.get("matrix") or {}
        if int(mat.get("cells_populated") or 0) > 0:
            matrix_ok += 1
        for epic, snap in _epic_rows(s.get("fulfillment") or {}).items():
            z = str(snap.get("zone") or "UNKNOWN")
            zone_counts[epic][z] += 1
            if z == "WIN_ZONE":
                win_hits[epic] += 1
            w = str(snap.get("wait_reason") or "")
            if w:
                wait_counts[epic][w] += 1

    lines.append("## Alpha matrix")
    lines.append(f"- Samples with populated cells: {matrix_ok}/{len(samples)}")
    last_mat = samples[-1].get("matrix") or {}
    lines.append(
        f"- Final: cells={last_mat.get('cells_populated')} "
        f"status={last_mat.get('status')}"
    )
    lines.append("")

    lines.append("## Per-market")
    for epic in sorted(zone_counts.keys()):
        label = EPIC_LABEL.get(epic, epic)
        lines.append(f"### {label}")
        lines.append(f"- WIN_ZONE hits: {win_hits.get(epic, 0)}")
        for z, n in zone_counts[epic].most_common():
            lines.append(f"  - {z}: {n}")
        for w, n in wait_counts[epic].most_common(3):
            lines.append(f"  - block ({n}x): {w}")
        lines.append("")

    if trade_count == 0:
        lines.append("## No-trade diagnosis")
        if matrix_ok < len(samples) * 0.5:
            lines.append(
                "- **Alpha matrix never populated** — prebaker did not compile "
                "(blocks all epics with ALPHA_MATRIX: miss)."
            )
        if not any(win_hits.values()):
            lines.append("- No WIN_ZONE during window — gates/matrix never armed.")
        else:
            for epic, n in win_hits.items():
                if n > 0 and wait_counts[epic]:
                    label = EPIC_LABEL.get(epic, epic)
                    top = wait_counts[epic].most_common(1)[0]
                    lines.append(f"- {label}: WIN_ZONE {n}x but blocked — {top[0]}")

    return "\n".join(lines)


def _monitor_loop(*, minutes: float, interval: float, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    jsonl_path = out_dir / f"session_{stamp}.jsonl"
    report_path = out_dir / f"report_{stamp}.md"
    deadline = time.monotonic() + minutes * 60.0
    samples: list[dict[str, Any]] = []

    log_engine(
        f"cockpit_session_monitor: started {minutes}min interval={interval}s "
        f"path={jsonl_path}"
    )

    while not _MONITOR_STOP.is_set() and time.monotonic() < deadline:
        sample = _sample()
        samples.append(sample)
        with jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(sample, default=str) + "\n")

        ful = sample.get("fulfillment") or {}
        epics = _epic_rows(ful)
        win = sum(1 for e in epics.values() if e.get("zone") == "WIN_ZONE")
        trades = len(ful.get("performance_rows") or [])
        mat = sample.get("matrix") or {}
        log_engine(
            f"cockpit_session_monitor: trades={trades} win_zones={win} "
            f"matrix_cells={mat.get('cells_populated')} shm="
            f"{(sample.get('shm') or {}).get('link_state')}"
        )

        sleep_for = min(interval, max(0, deadline - time.monotonic()))
        if sleep_for <= 0:
            break
        _MONITOR_STOP.wait(sleep_for)

    report = _build_report(samples)
    report_path.write_text(report, encoding="utf-8")
    log_engine(f"cockpit_session_monitor: report written {report_path}")
    if not any(
        len((s.get("fulfillment") or {}).get("performance_rows") or []) > 0
        for s in samples
    ):
        log_engine(f"cockpit_session_monitor NO TRADES —\n{report}")


def start_cockpit_session_monitor(
    *,
    minutes: float | None = None,
    interval_sec: float = 60.0,
) -> None:
    """Daemon: poll fulfillment + SHM; write report on completion."""
    global _MONITOR_THREAD
    if _MONITOR_THREAD is not None and _MONITOR_THREAD.is_alive():
        return
    if os.environ.get("IG_COCKPIT_SESSION_MONITOR", "1").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return

    dur = minutes
    if dur is None:
        try:
            dur = float(os.environ.get("IG_COCKPIT_MONITOR_MINUTES", "30"))
        except ValueError:
            dur = 30.0

    out_dir = logs_dir() / "cockpit_monitor"
    _MONITOR_STOP.clear()

    def _run() -> None:
        try:
            _monitor_loop(minutes=dur, interval=interval_sec, out_dir=out_dir)
        except Exception as exc:
            log_engine(f"cockpit_session_monitor error: {type(exc).__name__}: {exc}")

    _MONITOR_THREAD = threading.Thread(
        target=_run,
        name="cockpit-session-monitor",
        daemon=True,
    )
    _MONITOR_THREAD.start()


def stop_cockpit_session_monitor() -> None:
    _MONITOR_STOP.set()
