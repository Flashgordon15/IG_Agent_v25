"""
Session-end summary file and macOS notification (Fix 6).

Written once after FLATTEN CONFIRMED — all positions closed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from data.ml_training_store import MLTrainingStore
from system.engine_log import log_engine
from system.paths import logs_dir, project_root

if TYPE_CHECKING:
    from data.learning_store import LearningStore
    from trading.points_engine import PointsEngine
    from trading.session_manager import SessionManager

_BST = ZoneInfo("Europe/London")
_SUMMARY_SEP = "─────────────────────────────────────"


@dataclass(frozen=True)
class SessionTradeStats:
    total: int = 0
    wins: int = 0
    losses: int = 0
    pnl_gbp: float = 0.0


class SessionTickTracker:
    """Per-session counters for block reasons, stream LIVE ticks, and errors."""

    def __init__(self) -> None:
        self._session_open_key: str | None = None
        self._block_reasons: Counter[str] = Counter()
        self._live_ticks = 0
        self._session_ticks = 0
        self._errors = 0
        self._summary_written = False

    def reset_for_session(self, open_time: datetime | None) -> None:
        key = open_time.isoformat() if open_time is not None else None
        if key == self._session_open_key:
            return
        self._session_open_key = key
        self._block_reasons.clear()
        self._live_ticks = 0
        self._session_ticks = 0
        self._errors = 0
        self._summary_written = False

    def record_tick(self, *, block_reason: str | None, stream_live: bool) -> None:
        self._session_ticks += 1
        if stream_live:
            self._live_ticks += 1
        reason = (block_reason or "").strip()
        if reason:
            self._block_reasons[reason] += 1

    def record_error(self) -> None:
        self._errors += 1

    @property
    def summary_written(self) -> bool:
        return self._summary_written

    def mark_written(self) -> None:
        self._summary_written = True

    def stream_uptime_pct(self) -> float:
        if self._session_ticks <= 0:
            return 0.0
        return 100.0 * self._live_ticks / self._session_ticks

    def top_block_reason(self) -> str:
        if not self._block_reasons:
            return "none"
        return self._block_reasons.most_common(1)[0][0]

    @property
    def error_count(self) -> int:
        return self._errors


def _to_bst(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(_BST)


def format_bst_time(dt: datetime) -> str:
    local = _to_bst(dt)
    return local.strftime("%H:%M")


def format_summary_date(dt: datetime) -> str:
    return _to_bst(dt).strftime("%Y-%m-%d")


def summary_filename_for(dt: datetime) -> str:
    return f"session_summary_{_to_bst(dt).strftime('%Y%m%d')}.txt"


def _parse_trade_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(text[:26], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _trade_in_window(
    closed_at: str | None,
    *,
    since: datetime,
    until: datetime,
) -> bool:
    ts = _parse_trade_timestamp(closed_at)
    if ts is None:
        return False
    if ts.tzinfo is None:
        since_cmp = since.replace(tzinfo=None) if since.tzinfo else since
        until_cmp = until.replace(tzinfo=None) if until.tzinfo else until
        return since_cmp <= ts <= until_cmp
    since_ = since if since.tzinfo else since.replace(tzinfo=ts.tzinfo)
    until_ = until if until.tzinfo else until.replace(tzinfo=ts.tzinfo)
    return since_ <= ts <= until_


def collect_session_trades(
    store: LearningStore | None,
    *,
    open_time: datetime | None,
    close_time: datetime,
) -> SessionTradeStats:
    if store is None or open_time is None:
        return SessionTradeStats()
    try:
        rows = store.recent_confirmed_closed_trades(limit=200)
    except Exception as e:
        log_engine(f"session_summary trades query failed: {type(e).__name__}: {e}")
        return SessionTradeStats()

    wins = losses = 0
    pnl = 0.0
    total = 0
    for row in rows:
        if not _trade_in_window(
            row.get("closed_at"), since=open_time, until=close_time
        ):
            continue
        total += 1
        result = str(row.get("result") or "").upper()
        trade_pnl = float(row.get("pnl") or row.get("ig_pnl_currency") or 0.0)
        pnl += trade_pnl
        if result == "WIN":
            wins += 1
        elif result == "LOSS":
            losses += 1
        elif trade_pnl > 0:
            wins += 1
        elif trade_pnl < 0:
            losses += 1
    return SessionTradeStats(total=total, wins=wins, losses=losses, pnl_gbp=pnl)


def count_ml_records_in_session(
    ml_store: MLTrainingStore | None,
    *,
    open_time: datetime | None,
    close_time: datetime,
) -> int:
    if ml_store is None:
        return 0
    path = ml_store._path  # noqa: SLF001 — session-scoped count for summary
    if open_time is None or not path.exists():
        try:
            return ml_store.record_count()
        except Exception:
            return 0
    count = 0
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                exit_time = row.get("exit_time")
                if _trade_in_window(
                    str(exit_time) if exit_time else None,
                    since=open_time,
                    until=close_time,
                ):
                    count += 1
    except Exception as e:
        log_engine(f"session_summary ml count failed: {type(e).__name__}: {e}")
        return 0
    return count


def count_engine_errors_since(since: datetime | None) -> int:
    if since is None:
        return 0
    log_path = logs_dir() / "engine.log"
    if not log_path.is_file():
        return 0
    count = 0
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if " error" not in line.lower() and "failed" not in line.lower():
                    continue
                ts_part = line.split("|", 1)[0].strip()
                ts = _parse_trade_timestamp(ts_part)
                if ts is None:
                    continue
                if ts >= since.replace(tzinfo=None) if since.tzinfo else since:
                    count += 1
    except Exception:
        return 0
    return count


def build_summary_text(
    *,
    close_time: datetime,
    open_time: datetime | None,
    trades: SessionTradeStats,
    points_delta: float,
    points_state: str,
    error_count: int,
    ml_records: int,
    stream_pct: float,
    top_block: str,
) -> str:
    date_str = format_summary_date(close_time)
    open_label = format_bst_time(open_time) if open_time else "—"
    close_label = format_bst_time(close_time)
    win_rate = (100.0 * trades.wins / trades.total) if trades.total else 0.0
    return (
        "IG Agent v29 — Session Summary\n"
        f"Date: {date_str}\n"
        f"Session: {open_label} — {close_label} BST\n"
        f"{_SUMMARY_SEP}\n"
        f"Trades:      {trades.total} ({trades.wins}W / {trades.losses}L)\n"
        f"Win rate:    {win_rate:.1f}%\n"
        f"P&L:         £{trades.pnl_gbp:+.2f}\n"
        f"Points:      {points_delta:+.1f} pts\n"
        f"Final state: {points_state}\n"
        f"{_SUMMARY_SEP}\n"
        f"Errors:      {error_count}\n"
        f"ML records:  {ml_records}\n"
        f"Stream uptime: {stream_pct:.1f}%\n"
        f"Top block reason: {top_block}\n"
        f"{_SUMMARY_SEP}\n"
    )


def notify_macos(message: str) -> None:
    try:
        safe = message.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{safe}" with title "IG Agent v29"',
            ],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


def _launch_reconcile_pending_trades() -> None:
    script = project_root() / "scripts" / "reconcile_pending_trades.py"
    if not script.is_file():
        return
    try:
        log_engine("reconcile_pending_trades: RUN start")
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            cwd=str(project_root()),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        def _wait() -> None:
            try:
                rc = proc.wait()
                if rc == 0:
                    log_engine("reconcile_pending_trades: RUN complete")
                else:
                    log_engine(f"reconcile_pending_trades: RUN exited {rc}")
            except Exception as exc:
                log_engine(
                    f"reconcile_pending_trades: wait failed: "
                    f"{type(exc).__name__}: {exc}"
                )

        threading.Thread(target=_wait, daemon=True).start()
    except Exception as exc:
        log_engine(
            f"reconcile_pending_trades: launch failed: {type(exc).__name__}: {exc}"
        )


def write_session_end_summary(
    *,
    session: SessionManager,
    store: LearningStore | None,
    points: PointsEngine,
    tracker: SessionTickTracker,
    close_at: datetime,
    ml_store: MLTrainingStore | None = None,
) -> Path | None:
    if tracker.summary_written:
        return None

    raw_open = session.session_open_time
    open_time = raw_open if isinstance(raw_open, datetime) else None
    trades = collect_session_trades(store, open_time=open_time, close_time=close_at)
    ps = points.snapshot()
    try:
        points_delta = float(ps.session_score)
    except (TypeError, ValueError):
        points_delta = 0.0
    raw_state = ps.nominal_state
    points_state = (
        raw_state
        if raw_state in ("HEALTHY", "CAUTION", "WARNING", "STOP")
        else "HEALTHY"
    )
    ml_records = count_ml_records_in_session(
        ml_store, open_time=open_time, close_time=close_at
    )
    log_errors = count_engine_errors_since(open_time)
    error_count = max(tracker.error_count, log_errors)
    stream_pct = tracker.stream_uptime_pct()
    top_block = tracker.top_block_reason()

    body = build_summary_text(
        close_time=close_at,
        open_time=open_time,
        trades=trades,
        points_delta=points_delta,
        points_state=points_state,
        error_count=error_count,
        ml_records=ml_records,
        stream_pct=stream_pct,
        top_block=top_block,
    )

    out_dir = logs_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / summary_filename_for(close_at)
    path.write_text(body, encoding="utf-8")

    notify_msg = (
        f"{trades.wins}W/{trades.losses}L £{trades.pnl_gbp:+.2f} {points_state}"
    )
    notify_macos(notify_msg)
    log_engine(
        f"Session summary written: {trades.wins}W/{trades.losses}L "
        f"£{trades.pnl_gbp:+.2f}"
    )
    tracker.mark_written()
    _launch_reconcile_pending_trades()
    return path
