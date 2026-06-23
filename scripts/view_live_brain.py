#!/usr/bin/env python3
"""
IG Agent Apex v30 — Neon Command Cockpit (Darwin SHM naked reader).

Attaches to ``ig_agent_v30_shm`` via ``multiprocessing.shared_memory`` + ctypes
Structure unpack — zero HTTP, zero disk I/O, zero trading-path jitter.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from system.ipc.ring_buffer import (  # noqa: E402
    COCKPIT_SHM_NAME,
    VALVE_FIRE,
    VALVE_STALL,
    VALVE_WIN_ZONE,
    cockpit_shm_map_status,
    read_cockpit_shm,
)

REFRESH_MS = 500
STALL_SEC = 2.0


class CockpitShmReader:
    """
    Resilient Darwin SHM reader — reconnects after engine restarts without fatal path errors.
    """

    def __init__(self) -> None:
        self._last_error = ""
        self._attach_failures = 0

    def read(self) -> dict[str, Any] | None:
        try:
            view = read_cockpit_shm()
            if view is not None:
                self._attach_failures = 0
                self._last_error = ""
            return view
        except FileNotFoundError:
            self._attach_failures += 1
            self._last_error = "segment not published yet"
            return None
        except OSError as exc:
            if getattr(exc, "errno", None) == 2:
                self._attach_failures += 1
                self._last_error = "Darwin SHM segment missing (engine restart?)"
                return None
            self._last_error = f"{type(exc).__name__}: {exc}"
            return None
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            return None


R = "\033[0m"
B = "\033[1m"
BLINK = "\033[5m"
CYAN = "\033[96m"
GREEN = "\033[92m"
CRIMSON = "\033[91m"
PURPLE = "\033[95m"
MAGENTA = "\033[35m"
WHITE = "\033[97m"
ORANGE = "\033[38;5;208m"
YELLOW = "\033[93m"
DIM = "\033[2m"
BG_CRIMSON = "\033[41m\033[97m"


class VelocityTracker:
    """Client-side 2s stall detector on SHM ticks_cached / live_ram_ticks."""

    def __init__(self) -> None:
        self.last_ticks: int | None = None
        self.last_live: int | None = None
        self.last_change = time.monotonic()

    def update(self, ticks: int, live: int) -> tuple[bool, bool, float]:
        if self.last_ticks is None:
            self.last_ticks = ticks
            self.last_live = live
            self.last_change = time.monotonic()
            return False, False, 0.0

        climbing = False
        if ticks != self.last_ticks or live != self.last_live:
            climbing = ticks > self.last_ticks or live > self.last_live
            self.last_change = time.monotonic()
        self.last_ticks = ticks
        self.last_live = live

        frozen = time.monotonic() - self.last_change
        return climbing, frozen >= STALL_SEC, frozen


def _term_width() -> int:
    try:
        return max(72, shutil.get_terminal_size().columns)
    except OSError:
        return 80


def _box_line(text: str, color: str = "") -> str:
    w = _term_width() - 2
    plain = text[:w]
    return f"{color}│ {plain}{' ' * max(0, w - len(plain))}│{R}"


def _top_border(color: str) -> str:
    return f"{color}┌{'─' * (_term_width() - 2)}┐{R}"


def _mid_border(color: str) -> str:
    return f"{color}├{'─' * (_term_width() - 2)}┤{R}"


def _bot_border(color: str) -> str:
    return f"{color}└{'─' * (_term_width() - 2)}┘{R}"


def _fmt_ts(iso: str) -> str:
    if not iso:
        return "—"
    return iso.replace("T", " ")[:23]


def _render_fills(rows: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    lines.append(_box_line(f"{YELLOW}{B}[ RECENT LIVE TRACK FULFILLMENTS ]{R}", YELLOW))
    header = f"{'TIMESTAMP':<22} {'EPIC':<22} {'ACT':<5} {'ENTRY':>10} {'STATUS':<8} {'OUTCOME':<16}"
    lines.append(_box_line(f"{YELLOW}{DIM}{header}{R}", YELLOW))
    tail = list(rows)[-5:][::-1] if rows else []
    if not tail:
        lines.append(_box_line(f"{DIM}No executions recorded in SHM ledger{R}", YELLOW))
        return lines
    for row in tail:
        ts = _fmt_ts(str(row.get("executed_at") or ""))
        epic = str(row.get("epic") or "—")[:22]
        act = str(row.get("action") or "—")[:5]
        entry = row.get("entry")
        entry_s = f"{float(entry):>10.2f}" if entry is not None else f"{'—':>10}"
        status = str(row.get("status") or "—")[:8]
        result = str(row.get("result") or "—").upper()
        pnl = float(row.get("pnl_gbp") or 0)
        outcome = result + (f" {pnl:+.2f}" if pnl != 0 else "")
        line = f"{ts:<22} {epic:<22} {act:<5} {entry_s} {status:<8} {outcome:<16}"
        color = GREEN if result == "WIN" else (CRIMSON if result == "LOSS" else YELLOW)
        lines.append(_box_line(f"{color}{line}{R}", YELLOW))
    return lines


def _render_string_phases(sd: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    lines.append(_box_line(f"{CYAN}{B}[ SINGLE-STRING PHASE PROFILER ]{R}", CYAN))

    def _phase_row(n: int, label: str, ok: bool, detail: str, color_ok: str = GREEN) -> None:
        icon = "🟢" if ok else "🔴"
        col = color_ok if ok else CRIMSON
        lines.append(_box_line(f"{col}{icon} PHASE {n} — {label}{R}", CYAN))
        lines.append(_box_line(f"{DIM}{detail}{R}", CYAN))

    p1_ok = bool(sd.get("phase1_ok"))
    p1_detail = (
        f"latency {int(sd.get('phase1_latency_us') or 0):,} µs │ "
        f"ticks {int(sd.get('phase1_ticks_before') or 0):,}→{int(sd.get('phase1_ticks_after') or 0):,}"
    )
    if not p1_ok:
        p1_detail += f" │ code={sd.get('phase1_code')}"
    _phase_row(1, "FPTP INGESTION", p1_ok, p1_detail)

    p2_ok = bool(sd.get("phase2_ok"))
    p2_detail = (
        f"latency {int(sd.get('phase2_latency_us') or 0):,} µs │ "
        f"coord={int(sd.get('phase2_coordinate') or 0):,} │ "
        f"rsi={float(sd.get('phase2_rsi') or 0):.1f} atr={float(sd.get('phase2_atr') or 0):.4f}"
    )
    if not p2_ok:
        p2_detail += f" │ code={sd.get('phase2_code')}"
    _phase_row(2, "GEOMETRY QUANTIZATION", p2_ok, p2_detail)

    zone = int(sd.get("phase3_zone") or 0)
    p3_ok = zone == 1
    p3_detail = (
        f"lookup {int(sd.get('phase3_latency_us') or 0):,} µs │ zone={zone} │ "
        f"fail_streak={int(sd.get('phase3_fail_streak') or 0)} │ "
        f"thr={float(sd.get('phase3_signal_threshold') or 0):.1f} "
        f"atr×={float(sd.get('phase3_atr_multiplier') or 0):.2f}"
    )
    _phase_row(3, "ZERO-GATE TENSOR READ", p3_ok, p3_detail, color_ok=GREEN if p3_ok else ORANGE)

    p4_ok = bool(sd.get("phase4_ok", True))
    route_open = sd.get("phase4_route_open")
    if route_open is not None:
        p4_ok = bool(route_open)
    http = int(sd.get("phase4_http_status") or 0)
    route_label = "🟢 ROUTE_OPEN" if p4_ok else "🔴 BLOCKED"
    ext_err = str(sd.get("phase4_extended_error") or "").strip()
    p4_detail = (
        f"{route_label} │ latency {int(sd.get('phase4_latency_us') or 0):,} µs │ "
        f"HTTP {http if http else '—'} │ block_code={int(sd.get('phase4_block_code') or 0)}"
    )
    if ext_err:
        p4_detail += f"\n{DIM}    └─ {ext_err}{R}"
    _phase_row(4, "BROKER DELIVERY TUNNEL (SHADOW TRACER)", p4_ok, p4_detail)

    weakness = str(sd.get("weakness_msg") or "").strip()
    if weakness:
        wp = int(sd.get("weakness_phase") or 0)
        blink = f"{BG_CRIMSON}{BLINK}{weakness}{R}"
        lines.append(_box_line(blink if wp else f"{CRIMSON}{weakness}{R}", CRIMSON))
    return lines


def render_frame(view: dict[str, Any], velocity: VelocityTracker, frame: int) -> str:
    w = _term_width()
    pid = view.get("agent_pid", "—")
    align = view.get("memory_alignment", "WARMING")

    ticks = int(view.get("ticks_cached") or 0)
    live = int(view.get("live_ram_ticks") or 0)
    climbing, client_stall, frozen = velocity.update(ticks, live)
    server_stall = bool(view.get("stall_active")) or int(view.get("valve_status") or 0) == VALVE_STALL
    stalled = client_stall or server_stall

    signal_thr = view.get("signal_threshold", "—")
    atr_mult = view.get("atr_multiplier", "—")
    vector_density = int(view.get("vector_density") or 0)

    valve = int(view.get("valve_status") or 0)
    injecting = bool(view.get("injecting")) or valve == VALVE_FIRE
    win_zone = valve == VALVE_WIN_ZONE or int(view.get("zone") or 0) == 1
    zone = int(view.get("zone") or 0)
    coord = int(view.get("coordinate") or 0)
    lookup_ns = int(view.get("lookup_ns") or 0)

    blink_on = frame % 2 == 0
    lines: list[str] = []

    lines.append(_top_border(CYAN))
    lines.append(_box_line(f"{CYAN}{B}=== IG AGENT APEX V30 UNIFIED COMMAND COCKPIT ==={R}", CYAN))
    align_label = "TRUE SYNC" if str(align).upper() == "TRUE SYNC" or view.get("memory_aligned") else str(align)
    lines.append(
        _box_line(
            f"{CYAN}PID {B}{pid}{R}{CYAN}  │  MEMORY ALIGNMENT: {B}{align_label}{R}  "
            f"{DIM}│ SHM {COCKPIT_SHM_NAME} (Darwin){R}",
            CYAN,
        )
    )
    lines.append(_mid_border(CYAN))

    lines.append(
        _box_line(
            f"{GREEN if not stalled else CRIMSON}{B}STAGE 1 — INGESTION PIPELINE{R}",
            GREEN if not stalled else CRIMSON,
        )
    )
    if stalled:
        alert = "🔴 FEED STALL DETECTED - RE-BINDING WEBSOCKETS"
        style = f"{BG_CRIMSON}{BLINK}{alert}{R}" if blink_on else f"{CRIMSON}{B}{alert}{R}"
        lines.append(_box_line(style, CRIMSON))
        lines.append(
            _box_line(
                f"{CRIMSON}frozen {frozen:.1f}s │ ticks_cached={ticks:,} │ live_ram={live:,}{R}",
                CRIMSON,
            )
        )
    else:
        arrow = "▲" if climbing else "═"
        lines.append(
            _box_line(
                f"{GREEN}{B}🟢 ALL FEEDS ACTIVE │ RACING LINE: SECURE{R}  "
                f"{GREEN}{arrow} ticks_cached {B}{ticks:,}{R}{GREEN} │ live_ram {B}{live:,}{R}",
                GREEN,
            )
        )
    lines.append(_mid_border(PURPLE))

    lines.append(_box_line(f"{PURPLE}{B}STAGE 2 & 3 — BRAIN CALIBRATION{R}", PURPLE))
    lines.append(
        _box_line(
            f"{MAGENTA}signal_threshold {B}{signal_thr}{R}{MAGENTA}  │  "
            f"atr_multiplier {B}{atr_mult}{R}{MAGENTA}  │  "
            f"vector_density {B}{vector_density:,}{R}",
            PURPLE,
        )
    )
    lines.append(
        _box_line(
            f"{DIM}matrix coordinate {coord:,}/131072 │ naked lookup {lookup_ns:,} ns{R}",
            PURPLE,
        )
    )
    lines.append(_mid_border(WHITE))

    sd = view.get("string_diag") or {}
    if sd:
        lines.extend(_render_string_phases(sd))
        lines.append(_mid_border(WHITE))

    lines.append(_box_line(f"{WHITE}{B}STAGE 4 — MASTER EXECUTION VALVE{R}", WHITE))
    last_pnl = view.get("last_trade_pnl")
    if last_pnl is not None:
        lines.append(
            _box_line(
                f"{YELLOW}last_trade_pnl {B}{float(last_pnl):+.2f} GBP{R}",
                WHITE,
            )
        )
    if injecting or win_zone:
        fire = "🔥 FIRE: INJECTING LIVE IG PRODUCTION ORDER"
        style = f"{ORANGE}{B}{BLINK}{fire}{R}" if blink_on else f"{ORANGE}{B}{fire}{R}"
        lines.append(_box_line(style, ORANGE))
        lines.append(
            _box_line(f"{ORANGE}WIN_ZONE │ coordinate {coord:,} │ zone flag = {zone}{R}", ORANGE)
        )
    else:
        scan = "实时 SCANNING ALPHAS"
        lines.append(_box_line(f"{WHITE}{B}{scan}{R}", WHITE))
        lines.append(_box_line(f"{DIM}valve_status={valve} │ zone={zone}{R}", WHITE))
    lines.append(_mid_border(YELLOW))

    lines.extend(_render_fills(view.get("performance_rows") or []))
    lines.append(_bot_border(YELLOW))

    seq = view.get("write_seq", "—")
    pulse = view.get("pulse_serial", "—")
    footer = f"{DIM}SHM naked read {REFRESH_MS}ms │ seq {seq} │ pulse {pulse}{R}"
    lines.append(footer)
    return "\n".join(lines)


def _offline_screen(reader: CockpitShmReader | None = None) -> str:
    w = _term_width()
    status = cockpit_shm_map_status()
    ns = status.get("namespace", COCKPIT_SHM_NAME)
    err = ""
    if reader is not None and reader._last_error:
        err = f"\n║  {reader._last_error[: max(0, w - 6)]}{' ' * max(0, w - 8 - len(reader._last_error[: max(0, w - 6)]))}║"
    return (
        f"{CRIMSON}{B}╔{'═' * (w - 2)}╗\n"
        f"║  COCKPIT WAITING — attach {ns:<{max(0, w - 30)}}{' ' * max(0, w - 30 - len(str(ns)))}║\n"
        f"║  (agent fulfillment thread publishes SHM every 500ms){' ' * max(0, w - 58)}║"
        f"{err}\n"
        f"╚{'═' * (w - 2)}╝{R}"
    )


def run_cockpit() -> None:
    velocity = VelocityTracker()
    reader = CockpitShmReader()
    frame = 0

    if not sys.stdout.isatty():
        print("[view_live_brain] warning: stdout is not a TTY — ANSI colors may not render")

    while True:
        view = reader.read()
        frame += 1
        screen = render_frame(view, velocity, frame) if view else _offline_screen(reader)

        sys.stdout.write("\033[2J\033[H")
        sys.stdout.write(screen)
        sys.stdout.write("\n")
        sys.stdout.flush()
        time.sleep(REFRESH_MS / 1000.0)


def main() -> int:
    try:
        run_cockpit()
    except KeyboardInterrupt:
        sys.stdout.write(f"\n{CYAN}[view_live_brain] cockpit disengaged{R}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
