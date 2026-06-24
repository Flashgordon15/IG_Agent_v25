#!/usr/bin/env python3
"""
live_dual_monitor.py — isolated dual-agent terminal dashboard (read-only).

Polls v30 production (:8080) and v31 sandbox (:8081) side-by-side every 2s.
Watches logs/v30_vs_v31_benchmark.json for v31_simulated.trades increments
and fires a macOS audio alert the moment a new v31 shadow trade closes.

Usage (from sandbox root):
  python3 scripts/live_dual_monitor.py

Env overrides:
  IG_V30_API_BASE=http://127.0.0.1:8080
  IG_V31_API_BASE=http://127.0.0.1:8081
  IG_DUAL_MONITOR_POLL_SEC=2
  IG_BENCHMARK_JSON=logs/v30_vs_v31_benchmark.json
  IG_ALERT_SOUND=/System/Library/Sounds/Glass.aiff
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

V30_API = os.environ.get("IG_V30_API_BASE", "http://127.0.0.1:8080").rstrip("/")
V31_API = os.environ.get("IG_V31_API_BASE", "http://127.0.0.1:8081").rstrip("/")
POLL_SEC = float(os.environ.get("IG_DUAL_MONITOR_POLL_SEC", "2"))
BENCHMARK_WATCH_SEC = float(os.environ.get("IG_BENCHMARK_WATCH_SEC", "0.05"))
ALERT_SOUND = os.environ.get(
    "IG_ALERT_SOUND", "/System/Library/Sounds/Glass.aiff"
)
V30_BASELINE_FLOOR = 55.0
V31_ELASTIC_FLOOR = 38.0

_DYN_ADAPT_RE = re.compile(
    r"\[DYNAMIC_ADAPT\]\s+epic=\S+\s+regime=(\S+)\s+sig=([\d.]+)\s+fit=([\d.]+)"
)


def _sandbox_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _benchmark_path() -> Path:
    raw = os.environ.get("IG_BENCHMARK_JSON", "logs/v30_vs_v31_benchmark.json")
    p = Path(raw)
    return p if p.is_absolute() else _sandbox_root() / p


def _engine_log_path() -> Path:
    return _sandbox_root() / "src" / "data" / "logs" / "engine.log"


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _http_json(url: str, timeout: float = 4.0) -> dict[str, Any] | None:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _play_alert() -> None:
    sound = Path(ALERT_SOUND)
    if sys.platform == "darwin" and sound.is_file():
        subprocess.Popen(
            ["afplay", str(sound)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    else:
        print("\a", end="", flush=True)


def _latest_dynamic_adapt(log_path: Path) -> tuple[str, float, float]:
    if not log_path.is_file():
        return "unknown", V31_ELASTIC_FLOOR, V31_ELASTIC_FLOOR
    try:
        size = log_path.stat().st_size
        with log_path.open("rb") as fh:
            fh.seek(max(0, size - 128_000))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return "unknown", V31_ELASTIC_FLOOR, V31_ELASTIC_FLOOR

    last: re.Match[str] | None = None
    for match in _DYN_ADAPT_RE.finditer(tail):
        last = match
    if not last:
        return "warming", V31_ELASTIC_FLOOR, V31_ELASTIC_FLOOR
    regime = last.group(1).upper()
    sig = float(last.group(2))
    fit = float(last.group(3))
    return regime, sig, fit


@dataclass
class AgentSnapshot:
    label: str
    api_base: str
    online: bool
    pid: int | None
    loops_running: bool
    ready: bool
    loop_count: int
    gate_floor: float
    gate_signal: float
    regime: str
    closed_trades: int
    session_pnl_gbp: float
    error: str


def _loop_info(health: dict[str, Any] | None) -> tuple[bool, bool, int]:
    if not health:
        return False, False, 0
    ready = bool((health.get("boot_metrics") or {}).get("ready"))
    loops_running = bool(health.get("trading_loops_running"))
    loops = (health.get("boot_metrics") or {}).get("system_state") or {}
    loop_block = loops.get("loops") if isinstance(loops, dict) else {}
    built = 0
    if isinstance(loop_block, dict):
        try:
            built = int(loop_block.get("built") or 0)
        except (TypeError, ValueError):
            built = 0
    return loops_running, ready, built


def _gate_from_fulfillment(
    fulfillment: dict[str, Any] | None, *, default_floor: float
) -> tuple[float, float]:
    if not fulfillment:
        return default_floor, default_floor
    tuning = fulfillment.get("tuning_variables") or {}
    if not isinstance(tuning, dict):
        tuning = {}
    try:
        signal = float(tuning.get("signal_threshold") or default_floor)
    except (TypeError, ValueError):
        signal = default_floor

    floor = default_floor
    by_epic = (fulfillment.get("gate_diagnostics") or {}).get("by_epic") or {}
    if isinstance(by_epic, dict):
        for row in by_epic.values():
            if not isinstance(row, dict):
                continue
            for gate in row.get("gates") or []:
                if not isinstance(gate, dict):
                    continue
                if gate.get("name") != "environment_fitness":
                    continue
                val = gate.get("value")
                if isinstance(val, dict) and val.get("fitness_min") is not None:
                    try:
                        floor = float(val["fitness_min"])
                    except (TypeError, ValueError):
                        pass
    return signal, floor


def _session_pnl_from_health(health: dict[str, Any] | None) -> float:
    if not health:
        return 0.0
    for path in (
        ("system_status", "metrics", "shadow_vs_live", "live", "net_pnl_gbp"),
        ("system_status", "shadow_analytics", "agent_sourced", "net_pnl_gbp"),
    ):
        node: Any = health
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if node is not None:
            try:
                return float(node)
            except (TypeError, ValueError):
                pass
    return 0.0


def _fetch_agent(
    *,
    label: str,
    api_base: str,
    default_floor: float,
    benchmark_slice: Callable[[dict[str, Any]], dict[str, Any]] | None,
    regime_provider: Callable[[], tuple[str, float, float]] | None = None,
) -> AgentSnapshot:
    health = _http_json(f"{api_base}/api/health")
    fulfillment = _http_json(f"{api_base}/api/unified/fulfillment")
    loops_running, ready, loop_count = _loop_info(health)
    signal, floor = _gate_from_fulfillment(fulfillment, default_floor=default_floor)
    regime = "BASELINE"

    if regime_provider:
        regime, dyn_sig, dyn_fit = regime_provider()
        signal = dyn_sig
        floor = dyn_fit
    elif default_floor <= V31_ELASTIC_FLOOR:
        regime = "DYNAMIC"

    closed_trades = 0
    session_pnl = 0.0
    bench = _read_benchmark()
    if benchmark_slice and bench:
        metrics = benchmark_slice(bench)
        if isinstance(metrics, dict):
            try:
                closed_trades = int(metrics.get("trades") or 0)
            except (TypeError, ValueError):
                closed_trades = 0
            # Benchmark ledger is source of truth — 0.0 is valid; never fall back via `or`.
            if metrics.get("net_pnl_gbp") is not None:
                try:
                    session_pnl = float(metrics.get("net_pnl_gbp"))
                except (TypeError, ValueError):
                    session_pnl = 0.0
            else:
                session_pnl = 0.0
    else:
        session_pnl = _session_pnl_from_health(health)

    online = health is not None
    pid = None
    if health and health.get("agent_pid") is not None:
        try:
            pid = int(health["agent_pid"])
        except (TypeError, ValueError):
            pid = None

    err = ""
    if not online:
        err = "API unreachable"

    return AgentSnapshot(
        label=label,
        api_base=api_base,
        online=online,
        pid=pid,
        loops_running=loops_running,
        ready=ready,
        loop_count=loop_count,
        gate_floor=floor,
        gate_signal=signal,
        regime=regime,
        closed_trades=closed_trades,
        session_pnl_gbp=session_pnl,
        error=err,
    )


def _read_benchmark() -> dict[str, Any] | None:
    path = _benchmark_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


class TradeAlertWatcher(threading.Thread):
    """Non-blocking watcher for v31_simulated.trades increments."""

    daemon = True

    def __init__(self, on_alert: Callable[[int, int], None]) -> None:
        super().__init__(name="v31-trade-alert")
        self._on_alert = on_alert
        self._last_trades: int | None = None
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        path = _benchmark_path()
        while not self._stop.is_set():
            trades = self._read_trades(path)
            if trades is not None:
                if self._last_trades is not None and trades > self._last_trades:
                    self._on_alert(self._last_trades, trades)
                self._last_trades = trades
            self._stop.wait(BENCHMARK_WATCH_SEC)

    @staticmethod
    def _read_trades(path: Path) -> int | None:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            sim = data.get("v31_simulated") or {}
            if not isinstance(sim, dict):
                return None
            return int(sim.get("trades") or 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None


def _status_dot(ok: bool) -> str:
    return "●" if ok else "○"


def _fmt_money(value: float) -> str:
    return f"£{value:+,.2f}"


def _render_column(s: AgentSnapshot, width: int) -> list[str]:
    ready_txt = "READY" if s.ready else "BOOTING"
    loop_txt = (
        f"{s.loop_count} loops RUNNING"
        if s.loops_running
        else f"{s.loop_count} loops STOPPED"
    )
    floor_note = (
        f"Dynamic {s.gate_floor:.0f}%"
        if s.label.startswith("v31")
        else f"Fixed {s.gate_floor:.0f}%"
    )
    lines = [
        s.label.center(width),
        ("─" * width),
        f"API {s.api_base}"[:width],
        f"PID {s.pid or '—'}  {_status_dot(s.online)} online",
        f"State {ready_txt}  {_status_dot(s.loops_running)} {loop_txt}"[:width],
        f"Regime {s.regime}"[:width],
        f"Gate floor {floor_note}"[:width],
        f"Signal thr {s.gate_signal:.0f}%",
        f"Trades closed {s.closed_trades}",
        f"Bench P&L {_fmt_money(s.session_pnl_gbp)}",
    ]
    if s.error:
        lines.append(f"! {s.error}"[:width])
    return lines


def _render_dashboard(v30: AgentSnapshot, v31: AgentSnapshot, alert_note: str) -> None:
    if sys.stdout.isatty():
        os.system("clear")
    col_w = 36
    gap = " │ "
    left = _render_column(v30, col_w)
    right = _render_column(v31, col_w)
    rows = max(len(left), len(right))

    print("=" * (col_w * 2 + len(gap)))
    print(" IG AGENT DUAL MONITOR — read-only | v30 production vs v31 sandbox".center(col_w * 2 + len(gap)))
    print(f" { _iso_now() }".center(col_w * 2 + len(gap)))
    print("=" * (col_w * 2 + len(gap)))
    print(
        f"{'v30 PRODUCTION':^{col_w}}{gap}{'v31 SANDBOX':^{col_w}}"
    )
    print("─" * (col_w * 2 + len(gap)))

    for i in range(rows):
        l = left[i] if i < len(left) else ""
        r = right[i] if i < len(right) else ""
        print(f"{l:<{col_w}}{gap}{r:<{col_w}}")

    print("─" * (col_w * 2 + len(gap)))
    print(f" Poll {POLL_SEC:.0f}s  |  benchmark { _benchmark_path() }")
    print(f" Audio watch {BENCHMARK_WATCH_SEC * 1000:.0f}ms  |  {alert_note}")
    print("=" * (col_w * 2 + len(gap)))


def _fetch_pair() -> tuple[AgentSnapshot, AgentSnapshot]:
    log_path = _engine_log_path()
    results: dict[str, AgentSnapshot] = {}
    errors: dict[str, Exception] = {}

    def _v30() -> None:
        try:
            results["v30"] = _fetch_agent(
                label="v30 Production",
                api_base=V30_API,
                default_floor=V30_BASELINE_FLOOR,
                benchmark_slice=lambda b: b.get("v30_live_observed") or {},
            )
        except Exception as exc:
            errors["v30"] = exc

    def _v31() -> None:
        try:
            results["v31"] = _fetch_agent(
                label="v31 Sandbox",
                api_base=V31_API,
                default_floor=V31_ELASTIC_FLOOR,
                benchmark_slice=lambda b: b.get("v31_simulated") or {},
                regime_provider=lambda: _latest_dynamic_adapt(log_path),
            )
        except Exception as exc:
            errors["v31"] = exc

    t0 = threading.Thread(target=_v30, name="fetch-v30")
    t1 = threading.Thread(target=_v31, name="fetch-v31")
    t0.start()
    t1.start()
    t0.join()
    t1.join()

    if "v30" not in results:
        results["v30"] = AgentSnapshot(
            label="v30 Production",
            api_base=V30_API,
            online=False,
            pid=None,
            loops_running=False,
            ready=False,
            loop_count=0,
            gate_floor=V30_BASELINE_FLOOR,
            gate_signal=V30_BASELINE_FLOOR,
            regime="—",
            closed_trades=0,
            session_pnl_gbp=0.0,
            error=str(errors.get("v30", "fetch failed")),
        )
    if "v31" not in results:
        results["v31"] = AgentSnapshot(
            label="v31 Sandbox",
            api_base=V31_API,
            online=False,
            pid=None,
            loops_running=False,
            ready=False,
            loop_count=0,
            gate_floor=V31_ELASTIC_FLOOR,
            gate_signal=V31_ELASTIC_FLOOR,
            regime="—",
            closed_trades=0,
            session_pnl_gbp=0.0,
            error=str(errors.get("v31", "fetch failed")),
        )
    return results["v30"], results["v31"]


def main() -> int:
    alert_state = {"note": "Listening for v31_simulated.trades increments…"}
    alert_lock = threading.Lock()

    def on_trade(prev: int, new: int) -> None:
        delta = new - prev
        _play_alert()
        with alert_lock:
            alert_state["note"] = (
                f"ALERT fired — v31 trades {prev} → {new} (+{delta}) @ { _iso_now() }"
            )

    watcher = TradeAlertWatcher(on_alert=on_trade)
    watcher.start()

    print("Starting dual monitor (Ctrl+C to exit)…")
    try:
        while True:
            v30, v31 = _fetch_pair()
            with alert_lock:
                note = alert_state["note"]
            _render_dashboard(v30, v31, note)
            time.sleep(POLL_SEC)
    except KeyboardInterrupt:
        print("\nDual monitor stopped.")
        watcher.stop()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
