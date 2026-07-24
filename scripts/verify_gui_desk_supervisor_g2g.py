#!/usr/bin/env python3
"""Live G2G verify for GUI desk supervisor Phase 1+2.

Exit 0 when scorecard is PASS or WATCH-only (no unfixed STUCK_BUG).
Exit 1 on FAIL / STUCK / invalid JSON / missing desk planes.
Read-only by default; optional --heal-dry-run plans heals without mutate.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _tcp(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_json(url: str, timeout: float = 3.0) -> tuple[dict[str, Any] | None, str | None]:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "g2g-verify/1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw.strip() else {}
            return (data if isinstance(data, dict) else {"_raw": data}), None
    except Exception as exc:
        return None, f"{type(exc).__name__}:{exc}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="G2G verify GUI desk supervisor")
    p.add_argument("--heal-dry-run", action="store_true", help="Also plan Phase-2 heals (no execute)")
    p.add_argument("--json-stdout", action="store_true")
    args = p.parse_args(argv)

    from runtime.gui_desk_supervisor import run_once

    payload = run_once(write=True, heal=False, heal_dry_run=bool(args.heal_dry_run))
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "", stuck: bool = False) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "stuck": stuck})

    # Supervisor JSON valid
    score = str(payload.get("score") or "")
    add("supervisor_json", bool(payload.get("schema_version")), f"score={score} schema={payload.get('schema_version')}")
    add("score_not_unknown", score in ("PASS", "WATCH", "FAIL"), score)

    # Desk planes
    add("ui_3000", _tcp("127.0.0.1", 3000), "Quantum Terminal")
    h0, e0 = _http_json("http://127.0.0.1:8080/api/health")
    h1, e1 = _http_json("http://127.0.0.1:8081/api/health")
    add("agent_8080", h0 is not None, e0 or f"pid={h0.get('agent_pid') if h0 else None}")
    add("agent_8081", h1 is not None, e1 or f"pid={h1.get('agent_pid') if h1 else None}")

    a2 = payload.get("a2") or {}
    add(
        "a2_cfd_paused_sb_armed",
        bool(a2.get("marker_active") and a2.get("cfd_trading_paused") is True and a2.get("sb_trading_paused") is not True)
        or (not a2.get("marker_active")),
        f"marker={a2.get('marker_active')} cfd_paused={a2.get('cfd_trading_paused')} sb_paused={a2.get('sb_trading_paused')}",
    )

    stuck = payload.get("stuck_plane") or {}
    sb_loops = stuck.get("sb_loops") or {}
    sb_accepting = sb_loops.get("accepting_ticks") is True
    # If SB paused intentionally, accepting false is OK
    if a2.get("sb_trading_paused") is True:
        add("sb_loops_accepting", True, "sb paused — N/A")
    else:
        add("sb_loops_accepting", sb_accepting, json.dumps(sb_loops), stuck=not sb_accepting)

    ranked = stuck.get("ranked") or {}
    add(
        "ranked_mode_on",
        bool(ranked.get("active")) or str(ranked.get("mode") or "") == "ranked",
        f"active={ranked.get('active')} promoted={ranked.get('promoted')}",
    )

    # STUCK_BUG class findings must not remain unfixed
    stuck_titles = [
        str(f.get("title") or "")
        for f in (payload.get("findings") or [])
        if f.get("severity") == "fail"
        and (
            "STUCK" in str(f.get("title") or "").upper()
            or "not accepting ticks" in str(f.get("title") or "").lower()
            or "paused_at_boot" in str(f.get("title") or "").lower()
            or "hung API" in str(f.get("title") or "")
        )
    ]
    add("no_unfixed_stuck_bug", len(stuck_titles) == 0, "; ".join(stuck_titles) or "none", stuck=bool(stuck_titles))

    # Chip path present
    chip = payload.get("dashboard_chip") or {}
    add("dashboard_chip_shape", "state_path" in chip and "visible" in chip, str(chip.get("label")))

    # Handoff when needs_code
    if payload.get("needs_code"):
        handoff = payload.get("cursor_handoff")
        add("cursor_handoff_present", isinstance(handoff, dict) and bool(handoff.get("blurb")), "needs_code")
    else:
        add("cursor_handoff_present", True, "needs_code=false (null ok)")

    fail_checks = [c for c in checks if not c["ok"]]
    stuck_fail = [c for c in fail_checks if c.get("stuck")]
    # G2G: all live checks ok AND score PASS or WATCH (WATCH ok if no stuck)
    score_ok = score in ("PASS", "WATCH") and not stuck_fail
    g2g = score_ok and not fail_checks

    # WATCH-only with soft fails on optional a2 posture when marker absent is already handled
    report = {
        "g2g": "YES" if g2g else "NO",
        "score": score,
        "checks": checks,
        "pids": {
            "cfd": (h0 or {}).get("agent_pid"),
            "sb": (h1 or {}).get("agent_pid"),
        },
        "a2": a2,
        "sb_loops": sb_loops,
        "ranked": ranked,
        "heal": payload.get("heal"),
        "json": (payload.get("_written") or {}).get("json"),
        "chip_path": chip.get("state_path"),
    }

    if args.json_stdout:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"G2G={report['g2g']} score={score}")
        for c in checks:
            mark = "PASS" if c["ok"] else "FAIL"
            print(f"  [{mark}] {c['name']}: {c['detail']}")
        print(f"pids cfd={report['pids']['cfd']} sb={report['pids']['sb']}")
        print(f"json={report.get('json')}")
    return 0 if g2g else 1


if __name__ == "__main__":
    raise SystemExit(main())
