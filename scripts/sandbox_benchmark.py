#!/usr/bin/env python3
"""
sandbox_benchmark.py — read-only v30 vs v31 shadow broker benchmark.

SAFETY: Never imports execution_engine, live_executor, or IG REST clients.
        Never POSTs orders. Observes the live v30 Vanguard on :8080 via HTTP
        and log tail only; simulates v31 nimble exits in local memory.

Usage (from sandbox root):
  PYTHONPATH=src python3 scripts/sandbox_benchmark.py
  PYTHONPATH=src python3 scripts/sandbox_benchmark.py --api-base http://127.0.0.1:8080
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# v31 nimble vs v30 baseline (points) — shadow simulation only
# ---------------------------------------------------------------------------
V31_STOP_PTS = 3.0
V31_LIMIT_PTS = 5.0
V30_STOP_PTS = 10.0
V30_LIMIT_PTS = 20.0

DEFAULT_API_BASE = os.environ.get("IG_BENCHMARK_API_BASE", "http://127.0.0.1:8080")
POLL_SEC = float(os.environ.get("IG_BENCHMARK_POLL_SEC", "1.0"))
SERIALIZE_SEC = 60.0
DEFAULT_SIZE = float(os.environ.get("IG_BENCHMARK_SIZE", "1.0"))
GBP_PER_POINT = float(os.environ.get("IG_BENCHMARK_GBP_PER_POINT", "1.0"))

# Execution / telemetry anchors (read-only documentation paths in this repo)
EXECUTION_MODULE = "src/execution/live_executor.py"
EXECUTION_ENGINE = "src/execution/execution_engine.py"
FULFILLMENT_API = "/api/unified/fulfillment"
HEALTH_API = "/api/health"


def _sandbox_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _logs_dir() -> Path:
    d = _sandbox_root() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _engine_log_candidates() -> list[Path]:
    root = _sandbox_root()
    return [
        Path.home()
        / "Library/Application Support/IG Agent Apex/v30-production/logs/agent_restart.log",
        root / "src/data/logs/agent_restart.log",
        root / "src/data/logs/engine.log",
    ]


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _http_json(url: str, timeout: float = 5.0) -> dict[str, Any] | None:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, dict) else None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


@dataclass
class ShadowTrade:
    epic: str
    direction: str
    entry: float
    entry_ts: float
    stop_pts: float
    limit_pts: float
    size: float
    version: str  # "v31" | "v30_shadow"
    signal_key: str
    exit: float = 0.0
    exit_ts: float = 0.0
    exit_reason: str = ""
    pnl_points: float = 0.0
    pnl_gbp: float = 0.0

    def stop_price(self) -> float:
        if self.direction == "BUY":
            return self.entry - self.stop_pts
        return self.entry + self.stop_pts

    def limit_price(self) -> float:
        if self.direction == "BUY":
            return self.entry + self.limit_pts
        return self.entry - self.limit_pts

    def mark_exit(self, price: float, reason: str, ts: float) -> None:
        self.exit = price
        self.exit_ts = ts
        self.exit_reason = reason
        if self.direction == "BUY":
            self.pnl_points = (price - self.entry) * self.size
        else:
            self.pnl_points = (self.entry - price) * self.size
        self.pnl_gbp = self.pnl_points * GBP_PER_POINT

    @property
    def duration_sec(self) -> float:
        if self.exit_ts <= 0:
            return max(0.0, time.time() - self.entry_ts)
        return max(0.0, self.exit_ts - self.entry_ts)

    @property
    def closed(self) -> bool:
        return self.exit_reason != ""


@dataclass
class ShadowPortfolio:
    """In-memory shadow broker — never touches IG REST."""
    open_trades: list[ShadowTrade] = field(default_factory=list)
    closed_trades: list[ShadowTrade] = field(default_factory=list)
    seen_signals: set[str] = field(default_factory=set)
    seen_v30_deals: set[str] = field(default_factory=set)

    def open_trade(
        self,
        *,
        epic: str,
        direction: str,
        entry: float,
        stop_pts: float,
        limit_pts: float,
        version: str,
        signal_key: str,
        size: float = DEFAULT_SIZE,
    ) -> ShadowTrade | None:
        if signal_key in self.seen_signals:
            return None
        if any(t.epic == epic and not t.closed for t in self.open_trades):
            return None
        if direction not in ("BUY", "SELL") or entry <= 0:
            return None
        trade = ShadowTrade(
            epic=epic,
            direction=direction,
            entry=entry,
            entry_ts=time.time(),
            stop_pts=stop_pts,
            limit_pts=limit_pts,
            size=size,
            version=version,
            signal_key=signal_key,
        )
        self.seen_signals.add(signal_key)
        self.open_trades.append(trade)
        return trade

    def mark_quotes(self, quotes: dict[str, dict[str, Any]]) -> list[ShadowTrade]:
        closed_now: list[ShadowTrade] = []
        still_open: list[ShadowTrade] = []
        now = time.time()
        for trade in self.open_trades:
            q = quotes.get(trade.epic) or {}
            bid = float(q.get("bid") or 0)
            offer = float(q.get("offer") or 0)
            if bid <= 0 or offer <= 0:
                still_open.append(trade)
                continue
            mark = bid if trade.direction == "SELL" else offer
            stop = trade.stop_price()
            limit = trade.limit_price()
            hit_stop = (trade.direction == "BUY" and mark <= stop) or (
                trade.direction == "SELL" and mark >= stop
            )
            hit_limit = (trade.direction == "BUY" and mark >= limit) or (
                trade.direction == "SELL" and mark <= limit
            )
            if hit_stop:
                trade.mark_exit(stop, "stop", now)
                self.closed_trades.append(trade)
                closed_now.append(trade)
            elif hit_limit:
                trade.mark_exit(limit, "limit", now)
                self.closed_trades.append(trade)
                closed_now.append(trade)
            else:
                still_open.append(trade)
        self.open_trades = still_open
        return closed_now

    def trades_for(self, version: str) -> list[ShadowTrade]:
        return [t for t in self.closed_trades if t.version == version]


@dataclass
class V30LiveLedger:
    """Observed closed deals from v30 fulfillment performance_rows."""
    closed: list[dict[str, Any]] = field(default_factory=list)
    seen_deals: set[str] = field(default_factory=set)
    total_pnl_gbp: float = 0.0

    def ingest_performance_rows(self, rows: list[dict[str, Any]]) -> int:
        new_count = 0
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            deal = str(row.get("deal_id") or "").strip()
            if not deal or deal in self.seen_deals:
                continue
            status = str(row.get("status") or "CLOSED").upper()
            if status != "CLOSED":
                continue
            self.seen_deals.add(deal)
            pnl = float(row.get("pnl_gbp") or 0.0)
            self.total_pnl_gbp += pnl
            self.closed.append(
                {
                    "deal_id": deal,
                    "epic": row.get("epic"),
                    "direction": row.get("direction"),
                    "pnl_gbp": pnl,
                    "result": row.get("result"),
                    "executed_at": row.get("executed_at"),
                    "closed_at": row.get("closed_at"),
                    "entry": row.get("entry"),
                    "exit": row.get("exit"),
                }
            )
            new_count += 1
        return new_count


class LogTail:
    """Read-only incremental tail of v30 engine log (EXEC SUBMIT cross-ref)."""

    def __init__(self) -> None:
        self._path: Path | None = None
        self._offset = 0
        self._submit_lines: list[str] = []

    def _resolve_path(self) -> Path | None:
        if self._path and self._path.exists():
            return self._path
        for p in _engine_log_candidates():
            if p.exists():
                self._path = p
                if self._offset == 0:
                    self._offset = max(0, p.stat().st_size - 256_000)
                return p
        return None

    def poll(self) -> list[str]:
        path = self._resolve_path()
        if path is None:
            return []
        try:
            size = path.stat().st_size
            if size < self._offset:
                self._offset = 0
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self._offset)
                chunk = fh.read()
                self._offset = fh.tell()
        except OSError:
            return []
        fresh: list[str] = []
        for line in chunk.splitlines():
            if "EXEC SUBMIT" in line or "SUBMIT_TRUTH" in line:
                self._submit_lines.append(line)
                fresh.append(line)
        self._submit_lines = self._submit_lines[-200:]
        return fresh


def _parse_log_submit(line: str) -> dict[str, str] | None:
    """Extract epic/dir from EXEC SUBMIT log line."""
    out: dict[str, str] = {}
    for token in line.replace("|", " ").split():
        if token.startswith("epic="):
            out["epic"] = token.split("=", 1)[1]
        elif token.startswith("dir="):
            out["direction"] = token.split("=", 1)[1].upper()
        elif token.startswith("market=") and "epic" not in out:
            out["market"] = token.split("=", 1)[1]
    if out.get("epic") and out.get("direction") in ("BUY", "SELL"):
        return out
    return None


def _entry_price(quotes: dict[str, dict[str, Any]], epic: str, direction: str) -> float:
    q = quotes.get(epic) or {}
    bid = float(q.get("bid") or 0)
    offer = float(q.get("offer") or 0)
    if direction == "BUY":
        return offer if offer > 0 else float(q.get("mid") or 0)
    return bid if bid > 0 else float(q.get("mid") or 0)


def _detect_entry_signals(
    fulfillment: dict[str, Any],
    log_lines: list[str],
) -> list[dict[str, Any]]:
    """Mirror v30 entry intent from frontier injector + gate pass (read-only)."""
    signals: list[dict[str, Any]] = []
    quotes = fulfillment.get("market_quotes") or {}
    frontier = fulfillment.get("alpha_frontier_tracker") or {}
    by_epic = frontier.get("by_epic") or {}
    gate_by = (fulfillment.get("gate_diagnostics") or {}).get("by_epic") or {}

    for epic, row in by_epic.items():
        if not isinstance(row, dict):
            continue
        direction = str(row.get("direction") or "").upper()
        injecting = bool(row.get("injecting"))
        all_passed = bool(row.get("all_passed"))
        gate = gate_by.get(epic) if isinstance(gate_by, dict) else {}
        if isinstance(gate, dict) and gate.get("all_passed"):
            all_passed = True
        if not injecting and not (all_passed and direction in ("BUY", "SELL")):
            continue
        if direction not in ("BUY", "SELL"):
            continue
        ts = str(row.get("updated_at") or _iso_now())
        signals.append(
            {
                "epic": epic,
                "direction": direction,
                "source": "frontier_inject" if injecting else "gate_all_passed",
                "signal_key": f"{epic}:{direction}:{ts}",
            }
        )

    for line in log_lines:
        parsed = _parse_log_submit(line)
        if not parsed:
            continue
        epic = parsed["epic"]
        direction = parsed["direction"]
        signals.append(
            {
                "epic": epic,
                "direction": direction,
                "source": "log_exec_submit",
                "signal_key": f"log:{epic}:{direction}:{hash(line) & 0xFFFFFFFF:08x}",
            }
        )
    return signals


def _metrics(trades: list[ShadowTrade]) -> dict[str, Any]:
    closed = [t for t in trades if t.closed]
    if not closed:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": 0.0,
            "net_pnl_gbp": 0.0,
            "profit_factor": 0.0,
            "avg_duration_sec": 0.0,
            "avg_win_duration_sec": 0.0,
            "avg_loss_duration_sec": 0.0,
        }
    wins = [t for t in closed if t.pnl_gbp > 0]
    losses = [t for t in closed if t.pnl_gbp <= 0]
    gross_win = sum(t.pnl_gbp for t in wins)
    gross_loss = abs(sum(t.pnl_gbp for t in losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
    win_durs = [t.duration_sec for t in wins]
    loss_durs = [t.duration_sec for t in losses]
    return {
        "trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / len(closed), 2),
        "net_pnl_gbp": round(sum(t.pnl_gbp for t in closed), 2),
        "profit_factor": round(pf, 3),
        "avg_duration_sec": round(sum(t.duration_sec for t in closed) / len(closed), 1),
        "avg_win_duration_sec": round(sum(win_durs) / len(win_durs), 1) if win_durs else 0.0,
        "avg_loss_duration_sec": round(sum(loss_durs) / len(loss_durs), 1) if loss_durs else 0.0,
    }


def _v30_live_metrics(ledger: V30LiveLedger) -> dict[str, Any]:
    rows = ledger.closed
    if not rows:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": 0.0,
            "net_pnl_gbp": 0.0,
            "profit_factor": 0.0,
        }
    wins = [r for r in rows if float(r.get("pnl_gbp") or 0) > 0]
    losses = [r for r in rows if float(r.get("pnl_gbp") or 0) <= 0]
    gross_win = sum(float(r.get("pnl_gbp") or 0) for r in wins)
    gross_loss = abs(sum(float(r.get("pnl_gbp") or 0) for r in losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
    return {
        "trades": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / len(rows), 2),
        "net_pnl_gbp": round(ledger.total_pnl_gbp, 2),
        "profit_factor": round(pf, 3),
    }


def _render_dashboard(
    *,
    api_base: str,
    health: dict[str, Any] | None,
    v31: dict[str, Any],
    v30_shadow: dict[str, Any],
    v30_live: dict[str, Any],
    open_v31: int,
    open_v30s: int,
    last_signal: str,
) -> None:
    os.system("clear" if sys.stdout.isatty() else "true")
    alive = health.get("trading_loops_running") if health else False
    healthy = health.get("trading_healthy") if health else False
    print("=" * 72)
    print(" IG AGENT SHADOW BENCHMARK — read-only | no broker orders")
    print(f" API: {api_base}  |  loops={alive}  healthy={healthy}  |  {_iso_now()}")
    print(f" Execution (observed only): {EXECUTION_MODULE}")
    print(f" Telemetry: {FULFILLMENT_API} + engine log tail")
    print("=" * 72)
    print(f" {'':28} {'v31 SIM (3/5pt)':>18} {'v30 SHADOW (10/20)':>18} {'v30 LIVE £':>14}")
    print("-" * 72)
    print(
        f" {'Net P&L':28} "
        f"{v31['net_pnl_gbp']:>17.2f}£ "
        f"{v30_shadow['net_pnl_gbp']:>17.2f}£ "
        f"{v30_live['net_pnl_gbp']:>13.2f}£"
    )
    print(
        f" {'Win rate %':28} "
        f"{v31['win_rate_pct']:>17.1f} "
        f"{v30_shadow['win_rate_pct']:>17.1f} "
        f"{v30_live['win_rate_pct']:>13.1f}"
    )
    print(
        f" {'Profit factor':28} "
        f"{v31['profit_factor']:>17.3f} "
        f"{v30_shadow['profit_factor']:>17.3f} "
        f"{v30_live['profit_factor']:>13.3f}"
    )
    print(
        f" {'Avg duration (s)':28} "
        f"{v31['avg_duration_sec']:>17.1f} "
        f"{v30_shadow['avg_duration_sec']:>17.1f} "
        f"{'n/a':>13}"
    )
    print(
        f" {'Avg win hold (s)':28} "
        f"{v31['avg_win_duration_sec']:>17.1f} "
        f"{v30_shadow['avg_win_duration_sec']:>17.1f} "
        f"{'—':>13}"
    )
    print(
        f" {'Trades closed':28} "
        f"{v31['trades']:>17} "
        f"{v30_shadow['trades']:>17} "
        f"{v30_live['trades']:>13}"
    )
    print("-" * 72)
    print(f" Open shadow positions: v31={open_v31}  v30_shadow={open_v30s}")
    print(f" Last signal: {last_signal or '—'}")
    print(f" Serialize: {_logs_dir() / 'v30_vs_v31_benchmark.json'} every {int(SERIALIZE_SEC)}s")
    print("=" * 72)


def _serialize(
    *,
    v31: dict[str, Any],
    v30_shadow: dict[str, Any],
    v30_live: dict[str, Any],
    portfolio: ShadowPortfolio,
    ledger: V30LiveLedger,
    api_base: str,
) -> None:
    payload = {
        "updated_at": _iso_now(),
        "api_base": api_base,
        "params": {
            "v31_stop_pts": V31_STOP_PTS,
            "v31_limit_pts": V31_LIMIT_PTS,
            "v30_stop_pts": V30_STOP_PTS,
            "v30_limit_pts": V30_LIMIT_PTS,
            "gbp_per_point": GBP_PER_POINT,
            "poll_sec": POLL_SEC,
        },
        "v31_simulated": v31,
        "v30_shadow_same_signals": v30_shadow,
        "v30_live_observed": v30_live,
        "open_shadow": [
            asdict(t)
            for t in portfolio.open_trades
            if not t.closed
        ],
        "recent_v30_deals": ledger.closed[-20:],
    }
    out = _logs_dir() / "v30_vs_v31_benchmark.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(out)


def run_benchmark(api_base: str) -> int:
    print("SHADOW BROKER MODE — no IG REST order endpoints will be called.")
    portfolio = ShadowPortfolio()
    ledger = V30LiveLedger()
    log_tail = LogTail()
    last_signal = ""
    last_serialize = 0.0

    fulfillment_url = f"{api_base.rstrip('/')}{FULFILLMENT_API}"
    health_url = f"{api_base.rstrip('/')}{HEALTH_API}"

    while True:
        health = _http_json(health_url)
        fulfillment = _http_json(fulfillment_url) or {}
        quotes = fulfillment.get("market_quotes") or {}
        if not isinstance(quotes, dict):
            quotes = {}

        log_lines = log_tail.poll()
        ledger.ingest_performance_rows(fulfillment.get("performance_rows") or [])

        for sig in _detect_entry_signals(fulfillment, log_lines):
            epic = sig["epic"]
            direction = sig["direction"]
            entry = _entry_price(quotes, epic, direction)
            if entry <= 0:
                continue
            key = sig["signal_key"]
            v31t = portfolio.open_trade(
                epic=epic,
                direction=direction,
                entry=entry,
                stop_pts=V31_STOP_PTS,
                limit_pts=V31_LIMIT_PTS,
                version="v31",
                signal_key=f"v31:{key}",
            )
            portfolio.open_trade(
                epic=epic,
                direction=direction,
                entry=entry,
                stop_pts=V30_STOP_PTS,
                limit_pts=V30_LIMIT_PTS,
                version="v30_shadow",
                signal_key=f"v30s:{key}",
            )
            if v31t:
                last_signal = f"{epic} {direction} @ {entry:.2f} ({sig['source']})"

        portfolio.mark_quotes(quotes)

        v31_m = _metrics(portfolio.trades_for("v31"))
        v30s_m = _metrics(portfolio.trades_for("v30_shadow"))
        v30l_m = _v30_live_metrics(ledger)
        open_v31 = sum(1 for t in portfolio.open_trades if t.version == "v31")
        open_v30s = sum(1 for t in portfolio.open_trades if t.version == "v30_shadow")

        _render_dashboard(
            api_base=api_base,
            health=health,
            v31=v31_m,
            v30_shadow=v30s_m,
            v30_live=v30l_m,
            open_v31=open_v31,
            open_v30s=open_v30s,
            last_signal=last_signal,
        )

        now = time.time()
        if now - last_serialize >= SERIALIZE_SEC:
            _serialize(
                v31=v31_m,
                v30_shadow=v30s_m,
                v30_live=v30l_m,
                portfolio=portfolio,
                ledger=ledger,
                api_base=api_base,
            )
            last_serialize = now

        time.sleep(POLL_SEC)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only v30 vs v31 shadow benchmark (no broker orders)."
    )
    parser.add_argument(
        "--api-base",
        default=DEFAULT_API_BASE,
        help=f"Live Vanguard API root (default: {DEFAULT_API_BASE})",
    )
    args = parser.parse_args()
    try:
        run_benchmark(args.api_base)
    except KeyboardInterrupt:
        print("\nShadow benchmark stopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
