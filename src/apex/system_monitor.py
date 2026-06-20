"""
Native System Monitor — in-process terminal log ring + triage WAL report export.

No external terminal / AppleScript — consumed by dashboard SYSTEM MONITOR tab via HTTP/IPC.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from system.engine_log import log_engine
from system.paths import logs_dir, triage_db_path

_lock = threading.RLock()
_MONITOR_TTL_SEC = 30 * 60
_monitor_lines: deque[dict[str, Any]] = deque(maxlen=600)
_last_funnel_broadcast_mono = 0.0
_FUNNEL_BROADCAST_INTERVAL = 4.0


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def append_monitor_line(
    phase: str,
    message: str,
    *,
    latency_ms: float | None = None,
) -> None:
    """Append amber terminal line — retained for 30 minutes."""
    phase_txt = str(phase or "SYS").strip().upper()
    body = str(message or "").strip()
    if latency_ms is not None:
        body = f"{body} [{float(latency_ms):.0f}ms latency]"
    line = f"[{_utc_stamp()} UTC] {phase_txt}: {body}"
    now = time.time()
    entry = {"ts": now, "ts_utc": _utc_stamp(), "phase": phase_txt, "line": line}
    with _lock:
        _monitor_lines.appendleft(entry)
        cutoff = now - _MONITOR_TTL_SEC
        while _monitor_lines and float(_monitor_lines[-1].get("ts") or 0) < cutoff:
            _monitor_lines.pop()


_health_log_min_interval = 10.0
_last_health_log_mono = 0.0


def record_health_ping(
    *,
    port: int = 9090,
    latency_ms: float | None = None,
    ok: bool = True,
) -> None:
    global _last_health_log_mono
    now = time.monotonic()
    if now - _last_health_log_mono < _health_log_min_interval:
        return
    _last_health_log_mono = now
    status = "SUCCESS" if ok else "FAIL"
    append_monitor_line(
        "PHASE 1",
        f"Port {port} Health Ping {status}",
        latency_ms=latency_ms,
    )


_last_ipc_log_mono = 0.0
_IPC_LOG_MIN_INTERVAL = 15.0


def record_ipc_handshake(*, clients: int = 0, latency_ms: float | None = None) -> None:
    global _last_ipc_log_mono
    now = time.monotonic()
    if now - _last_ipc_log_mono < _IPC_LOG_MIN_INTERVAL:
        return
    _last_ipc_log_mono = now
    append_monitor_line(
        "IPC",
        f"Core socket bridge handshake — {clients} client(s) attached",
        latency_ms=latency_ms,
    )


def _prune_stale_lines() -> None:
    cutoff = time.time() - _MONITOR_TTL_SEC
    with _lock:
        while _monitor_lines and float(_monitor_lines[-1].get("ts") or 0) < cutoff:
            _monitor_lines.pop()


def _maybe_broadcast_funnel_metrics() -> None:
    global _last_funnel_broadcast_mono
    now = time.monotonic()
    if now - _last_funnel_broadcast_mono < _FUNNEL_BROADCAST_INTERVAL:
        return
    _last_funnel_broadcast_mono = now
    try:
        from apex.operational_transparency import build_funnel_snapshot

        funnel = build_funnel_snapshot()
        append_monitor_line(
            "FUNNEL",
            (
                f"Opportunities scanned={funnel.get('opportunities_scanned', 0)} | "
                f"Spread rejections={funnel.get('spread_rejections', 0)} | "
                f"Liquidity blocks={funnel.get('liquidity_blocks', 0)} | "
                f"ML vetoes={funnel.get('ml_veto_flags', 0)} | "
                f"Executed={funnel.get('executed_trades', 0)}"
            ),
        )
    except Exception as exc:
        append_monitor_line("FUNNEL", f"Counter read skipped: {type(exc).__name__}")


def build_monitor_snapshot() -> dict[str, Any]:
    _prune_stale_lines()
    _maybe_broadcast_funnel_metrics()
    try:
        from apex.operational_transparency import build_funnel_snapshot, build_transparency_snapshot

        funnel = build_funnel_snapshot()
        transparency = build_transparency_snapshot(include_ml=False)
    except Exception:
        funnel = {}
        transparency = {}
    with _lock:
        lines = [dict(row) for row in _monitor_lines]
    bridge_sec = None
    try:
        health = (transparency or {}).get("health_grid") or {}
        bridge_sec = health.get("bridge_connected_sec_ago")
    except Exception:
        pass
    return {
        "ts": time.time(),
        "pid": os.getpid(),
        "lines": lines,
        "funnel": funnel,
        "health_grid": (transparency or {}).get("health_grid") or {},
        "bridge_sec_ago": bridge_sec,
        "ttl_sec": _MONITOR_TTL_SEC,
    }


def _read_triage_session_metrics() -> dict[str, Any]:
    path = triage_db_path()
    if not path.is_file():
        return {}
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT value FROM triage_meta WHERE key = ? LIMIT 1",
            ("session_performance",),
        )
        row = cur.fetchone()
        conn.close()
        if row and row["value"]:
            return json.loads(str(row["value"]))
    except Exception as exc:
        log_engine(f"system_monitor triage session read: {type(exc).__name__}: {exc}")
    return {}


def _read_slippage_aggregates() -> dict[str, Any]:
    path = triage_db_path()
    if not path.is_file():
        return {}
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT
                COUNT(*) AS fill_count,
                AVG(slip_distance_points) AS avg_slip_pts,
                MAX(slip_distance_points) AS max_slip_pts,
                AVG(spread_penalty_points) AS avg_spread_premium_pts,
                MAX(spread_penalty_points) AS max_spread_premium_pts,
                AVG(processing_latency_us) AS avg_latency_us
            FROM latency_metrics
            WHERE event_type = 'slippage_fill'
            """
        )
        row = cur.fetchone()
        recent = conn.execute(
            """
            SELECT epic, slip_distance_points, spread_penalty_points, session_window, timestamp
            FROM latency_metrics
            WHERE event_type = 'slippage_fill'
            ORDER BY id DESC
            LIMIT 12
            """
        ).fetchall()
        conn.close()
        base = dict(row) if row else {}
        base["recent_fills"] = [dict(r) for r in recent]
        return base
    except Exception as exc:
        log_engine(f"system_monitor slippage read: {type(exc).__name__}: {exc}")
    return {}


def _format_warmup_markdown(
    *,
    session: dict[str, Any],
    slippage: dict[str, Any],
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    sharpe = float(session.get("sharpe_ratio") or 0.0)
    expectancy = float(session.get("expectancy_gbp") or 0.0)
    trades = int(session.get("trade_count") or 0)
    wins = int(session.get("win_count") or 0)
    losses = int(session.get("loss_count") or 0)
    net = float(session.get("net_pnl_sum") or 0.0)
    drawdown = float(session.get("rolling_drawdown_gbp") or 0.0)
    avg_slip = float(slippage.get("avg_slip_pts") or 0.0)
    max_slip = float(slippage.get("max_slip_pts") or 0.0)
    avg_spread = float(slippage.get("avg_spread_premium_pts") or 0.0)
    max_spread = float(slippage.get("max_spread_premium_pts") or 0.0)
    fill_count = int(slippage.get("fill_count") or 0)

    lines = [
        "# IG Agent Apex — Performance Track Record",
        "",
        f"**Generated:** {now}  ",
        f"**Process PID:** {os.getpid()}  ",
        f"**Triage DB:** `{triage_db_path()}`  ",
        "",
        "## Rolling Session Sharpe & Expectancy",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Sharpe Ratio (rolling) | **{sharpe:.4f}** |",
        f"| Expectancy (GBP/trade) | **£{expectancy:.2f}** |",
        f"| Closed Trades | {trades} ({wins}W / {losses}L) |",
        f"| Net P&L (GBP) | **£{net:.2f}** |",
        f"| Rolling Drawdown (GBP) | £{drawdown:.2f} |",
        "",
        "## Direction-Aware Slippage & Spread-Fee Premium",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Slippage fills logged | {fill_count} |",
        f"| Avg slip distance (pts) | **{avg_slip:.3f}** |",
        f"| Max slip distance (pts) | {max_slip:.3f} |",
        f"| Avg spread-fee premium (pts) | **{avg_spread:.3f}** |",
        f"| Max spread-fee premium (pts) | {max_spread:.3f} |",
        "",
    ]
    recent = slippage.get("recent_fills") or []
    if recent:
        lines.extend(
            [
                "## Recent Slippage Fills (WAL Ledger)",
                "",
                "| Epic | Slip (pts) | Spread Premium | Session |",
                "|------|------------|----------------|---------|",
            ]
        )
        for row in recent:
            lines.append(
                f"| {row.get('epic', '—')} | "
                f"{float(row.get('slip_distance_points') or 0):.2f} | "
                f"{float(row.get('spread_penalty_points') or 0):.2f} | "
                f"{row.get('session_window') or '—'} |"
            )
        lines.append("")
    lines.extend(
        [
            "---",
            "*Exported natively from triage_v30.db — no external shell.*",
        ]
    )
    return "\n".join(lines)


def export_warmup_report() -> dict[str, Any]:
    """Pull triage WAL metrics and write logs/warmup_report_latest.md."""
    session = _read_triage_session_metrics()
    slippage = _read_slippage_aggregates()
    try:
        from analytics.triage_logger import get_triage_logger

        live = get_triage_logger().stats()
        if isinstance(live.get("session"), dict):
            session = {**session, **live["session"]}
    except Exception:
        pass
    markdown = _format_warmup_markdown(session=session, slippage=slippage)
    out_path = logs_dir() / "warmup_report_latest.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")
    append_monitor_line("EXPORT", f"Performance track record → {out_path.name}")
    log_engine(f"SystemMonitor: warmup report exported → {out_path}")
    return {
        "ok": True,
        "path": str(out_path),
        "sharpe_ratio": session.get("sharpe_ratio"),
        "trade_count": session.get("trade_count"),
        "fill_count": slippage.get("fill_count"),
    }


def reset_system_monitor_for_tests() -> None:
    global _last_funnel_broadcast_mono
    with _lock:
        _monitor_lines.clear()
        _last_funnel_broadcast_mono = 0.0
