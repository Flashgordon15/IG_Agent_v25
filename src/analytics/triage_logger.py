"""
Asynchronous SQLite triage logger — v30 Apex analytics plane (Worker D ledger).

Hot-path producers enqueue dict payloads; a detached asyncio worker persists rows
via aiosqlite without blocking tick execution or math workers.

Database path (v30 monolith):
  ~/Library/Application Support/IG Agent Apex/v30-production/analytics/triage_v30.db
"""

from __future__ import annotations

import asyncio
import json
import math
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from system.engine_log import log_engine

_SCHEMA_VERSION = 2
_QUEUE_MAX = 4096
_FLOAT64 = np.float64

_CLOSED_POSITIONS_DDL = """
CREATE TABLE IF NOT EXISTS closed_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket TEXT NOT NULL,
    asset TEXT NOT NULL,
    size REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    direction TEXT NOT NULL,
    gross_pnl REAL NOT NULL,
    net_pnl REAL NOT NULL,
    exit_timestamp TEXT NOT NULL,
    epic TEXT,
    result TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_closed_positions_ticket ON closed_positions(ticket);
CREATE INDEX IF NOT EXISTS idx_closed_positions_exit_ts ON closed_positions(exit_timestamp);
"""

_LATENCY_METRICS_DDL = """
CREATE TABLE IF NOT EXISTS latency_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    tick_arrival_us REAL NOT NULL,
    processing_latency_us REAL NOT NULL,
    slip_distance_points REAL,
    spread_penalty_points REAL,
    node_env TEXT NOT NULL,
    epic TEXT,
    event_type TEXT NOT NULL DEFAULT 'tick',
    session_window TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_latency_timestamp ON latency_metrics(timestamp);
CREATE INDEX IF NOT EXISTS idx_latency_node_env ON latency_metrics(node_env);
CREATE INDEX IF NOT EXISTS idx_latency_epic ON latency_metrics(epic);
"""

_META_DDL = """
CREATE TABLE IF NOT EXISTS triage_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_ML_FEATURE_EXECUTIONS_DDL = """
CREATE TABLE IF NOT EXISTS ml_feature_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    deal_ref TEXT,
    epic TEXT,
    direction TEXT,
    win_probability REAL,
    model_verdict TEXT,
    feature_vector BLOB,
    result TEXT,
    net_pnl REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ml_feature_deal ON ml_feature_executions(deal_ref);
CREATE INDEX IF NOT EXISTS idx_ml_feature_ts ON ml_feature_executions(timestamp);
"""

_OVERLAP_SESSIONS = frozenset({"london_us_overlap", "london_morning", "us_afternoon"})


def resolve_triage_db_path() -> Path:
    """Dynamic DB path — v30 isolated analytics namespace first."""
    import os

    env_path = os.environ.get("IG_TRIAGE_DB", "").strip()
    if env_path:
        return Path(env_path)
    try:
        from system.paths import triage_db_path

        return triage_db_path()
    except Exception:
        pass
    try:
        from system.node_profile import get_node_profile

        return get_node_profile().triage_db
    except Exception:
        pass
    raise RuntimeError(
        "TriageLogger: unresolved DB path — v30 isolated analytics required"
    )


def resolve_node_env_label() -> str:
    try:
        from system.node_profile import get_node_profile

        return get_node_profile().kind
    except Exception:
        import os

        return os.environ.get("IG_NODE_PROFILE", "production") or "production"


@dataclass(frozen=True)
class ClosedPositionRecord:
    ticket: str
    asset: str
    size: float
    entry_price: float
    exit_price: float
    direction: str
    gross_pnl: float
    net_pnl: float
    exit_timestamp: str = ""
    epic: str = ""
    result: str = ""

    @classmethod
    def from_legacy(
        cls,
        *,
        ticket: str,
        asset: str,
        size: float,
        entry: float,
        exit: float,
        execution_side: str,
        gross_pnl: float,
        net_pnl: float,
        closed_at: str = "",
        epic: str = "",
        result: str = "",
    ) -> ClosedPositionRecord:
        """Map v29.1 settlement field names onto v30 schema."""
        return cls(
            ticket=ticket,
            asset=asset,
            size=size,
            entry_price=entry,
            exit_price=exit,
            direction=execution_side,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            exit_timestamp=closed_at,
            epic=epic,
            result=result,
        )


@dataclass(frozen=True)
class LatencyMetricRecord:
    timestamp: float
    tick_arrival_us: float
    processing_latency_us: float
    node_env: str
    slip_distance_points: float | None = None
    spread_penalty_points: float | None = None
    epic: str = ""
    event_type: str = "tick"
    session_window: str = ""


@dataclass
class SessionPerformanceSnapshot:
    """Rolling session analytics computed in the background worker."""

    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    gross_pnl_sum: float = 0.0
    net_pnl_sum: float = 0.0
    sharpe_ratio: float = 0.0
    expectancy_gbp: float = 0.0
    rolling_drawdown_gbp: float = 0.0
    peak_equity_gbp: float = 0.0
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_count": self.trade_count,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "gross_pnl_sum": round(self.gross_pnl_sum, 4),
            "net_pnl_sum": round(self.net_pnl_sum, 4),
            "sharpe_ratio": round(self.sharpe_ratio, 6),
            "expectancy_gbp": round(self.expectancy_gbp, 4),
            "rolling_drawdown_gbp": round(self.rolling_drawdown_gbp, 4),
            "peak_equity_gbp": round(self.peak_equity_gbp, 4),
            "updated_at": self.updated_at,
        }


class SessionPerformanceTracker:
    """In-memory session metrics — Sharpe, expectancy, rolling drawdown."""

    def __init__(self, *, baseline_gbp: float = 10_000.0) -> None:
        self._baseline = float(baseline_gbp)
        self._returns: list[float] = []
        self._equity_curve: list[float] = [self._baseline]
        self._peak = self._baseline
        self._wins = 0
        self._losses = 0
        self._lock = threading.Lock()

    def record_closed_trade(self, net_pnl: float) -> SessionPerformanceSnapshot:
        pnl = float(net_pnl)
        with self._lock:
            self._returns.append(pnl)
            if len(self._returns) > 512:
                self._returns = self._returns[-512:]
            if pnl >= 0:
                self._wins += 1
            else:
                self._losses += 1
            equity = self._equity_curve[-1] + pnl
            self._equity_curve.append(equity)
            if len(self._equity_curve) > 513:
                self._equity_curve = [self._equity_curve[0], *self._equity_curve[-512:]]
            self._peak = max(self._peak, equity)
            drawdown = max(0.0, self._peak - equity)
            return self._snapshot(drawdown)

    def current_snapshot(self) -> SessionPerformanceSnapshot:
        with self._lock:
            equity = self._equity_curve[-1] if self._equity_curve else self._baseline
            drawdown = max(0.0, self._peak - equity)
            return self._snapshot(drawdown)

    def to_dict(self) -> dict[str, Any]:
        return self.current_snapshot().to_dict()

    def snapshot_from_latencies(self, latencies_us: list[float]) -> dict[str, float]:
        """Auxiliary microstructure stats from Worker D latency ring."""
        if not latencies_us:
            return {"latency_p50_us": 0.0, "latency_p95_us": 0.0}
        arr = np.asarray(latencies_us, dtype=_FLOAT64)
        return {
            "latency_p50_us": float(np.percentile(arr, 50)),
            "latency_p95_us": float(np.percentile(arr, 95)),
        }

    def _snapshot(self, drawdown: float) -> SessionPerformanceSnapshot:
        rets = np.asarray(self._returns, dtype=_FLOAT64)
        n = len(rets)
        expectancy = float(np.mean(rets)) if n else 0.0
        sharpe = 0.0
        if n >= 2:
            std = float(np.std(rets, ddof=1))
            if std > 1e-12:
                sharpe = (expectancy / std) * math.sqrt(min(n, 252))
        return SessionPerformanceSnapshot(
            trade_count=n,
            win_count=self._wins,
            loss_count=self._losses,
            gross_pnl_sum=float(np.sum(rets)) if n else 0.0,
            net_pnl_sum=float(np.sum(rets)) if n else 0.0,
            sharpe_ratio=sharpe,
            expectancy_gbp=expectancy,
            rolling_drawdown_gbp=drawdown,
            peak_equity_gbp=self._peak,
            updated_at=time.time(),
        )


def quantify_slippage(
    *,
    direction: str,
    requested_price: float,
    fill_price: float,
    spread_points: float,
    session_window: str = "",
) -> dict[str, float]:
    """
    Quantify unfavourable slippage and spread-fee premium for volatile overlap sessions.

    Returns slip_distance_points (absolute) and spread_penalty_points (overlap-weighted).
    """
    req = float(requested_price)
    fill = float(fill_price)
    spread = max(0.0, float(spread_points))
    side = str(direction or "").upper()
    slip_abs = abs(fill - req)
    if side == "BUY":
        unfavourable = max(0.0, fill - req)
    elif side == "SELL":
        unfavourable = max(0.0, req - fill)
    else:
        unfavourable = slip_abs
    overlap_mult = 1.25 if session_window in _OVERLAP_SESSIONS else 1.0
    spread_penalty = spread * overlap_mult
    if session_window in _OVERLAP_SESSIONS and unfavourable > 0:
        spread_penalty += unfavourable * 0.5
    return {
        "slip_distance_points": slip_abs,
        "unfavourable_slip_points": unfavourable,
        "spread_penalty_points": spread_penalty,
    }


def analyze_broker_fill_slippage(
    *,
    epic: str,
    direction: str,
    requested_price: float,
    broker_confirm: dict[str, Any],
    spread_points: float | None = None,
) -> dict[str, Any]:
    """
    Cross-reference requested entry against IG broker confirm payload.

    Reads ``level``, ``fillPrice``, ``openLevel``, or ``price`` from broker JSON.
    """
    fill_raw = (
        broker_confirm.get("level")
        or broker_confirm.get("fillPrice")
        or broker_confirm.get("openLevel")
        or broker_confirm.get("price")
    )
    if fill_raw is None:
        affected = broker_confirm.get("affectedDeals") or []
        if affected and isinstance(affected[0], dict):
            fill_raw = affected[0].get("level") or affected[0].get("price")
    fill_price = float(fill_raw) if fill_raw is not None else float(requested_price)
    spread = float(spread_points) if spread_points is not None else float(
        broker_confirm.get("spread") or broker_confirm.get("spread_points") or 0.0
    )
    try:
        from signals.indicators import session_name

        session_window = session_name()
    except Exception:
        session_window = str(broker_confirm.get("session_window") or "")
    metrics = quantify_slippage(
        direction=direction,
        requested_price=requested_price,
        fill_price=fill_price,
        spread_points=spread,
        session_window=session_window,
    )
    return {
        "epic": epic,
        "direction": direction,
        "requested_price": float(requested_price),
        "fill_price": fill_price,
        "session_window": session_window,
        **metrics,
        "broker_deal_ref": str(
            broker_confirm.get("dealReference")
            or broker_confirm.get("dealId")
            or broker_confirm.get("deal_id")
            or ""
        ),
    }


def bootstrap_triage_db_wal(path: Path | None = None) -> None:
    """Boot-time WAL truncate — clears frozen journal residue from prior kills."""
    import sqlite3

    db_path = path or resolve_triage_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        conn.commit()
    finally:
        conn.close()


class TriageLogger:
    """Thread-safe enqueue / detached asyncio SQLite writer."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or resolve_triage_db_path()
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=_QUEUE_MAX)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False
        self._lock = threading.Lock()
        self._dropped = 0
        self._written = 0
        self._session = SessionPerformanceTracker()
        self._latency_ring: list[float] = []

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def session_tracker(self) -> SessionPerformanceTracker:
        return self._session

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        try:
            bootstrap_triage_db_wal(self._db_path)
        except Exception as exc:
            log_engine(
                f"TriageLogger: WAL bootstrap skipped: {type(exc).__name__}: {exc}"
            )
        self._thread = threading.Thread(
            target=self._run_worker, name="apex-triage-logger", daemon=True
        )
        self._thread.start()
        log_engine(f"TriageLogger: async worker started ({self._db_path})")

    def stop(self, *, timeout: float = 5.0) -> None:
        if not self._started:
            return
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._started = False

    def _run_worker(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._worker_main())
        finally:
            self._loop.close()
            self._loop = None

    async def _migrate_schema_if_needed(self, db: Any) -> None:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='closed_positions'"
        )
        row = await cursor.fetchone()
        if not row:
            return
        cursor = await db.execute("PRAGMA table_info(closed_positions)")
        cols = {r[1] for r in await cursor.fetchall()}
        if "entry_price" in cols:
            return
        if "entry" not in cols:
            return
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS closed_positions_v30 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket TEXT NOT NULL,
                asset TEXT NOT NULL,
                size REAL NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                direction TEXT NOT NULL,
                gross_pnl REAL NOT NULL,
                net_pnl REAL NOT NULL,
                exit_timestamp TEXT NOT NULL,
                epic TEXT,
                result TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO closed_positions_v30 (
                ticket, asset, size, entry_price, exit_price, direction,
                gross_pnl, net_pnl, exit_timestamp, epic, result, created_at
            )
            SELECT
                ticket, asset, size, entry, exit, execution_side,
                gross_pnl, net_pnl, closed_at, epic, result, created_at
            FROM closed_positions;
            DROP TABLE closed_positions;
            ALTER TABLE closed_positions_v30 RENAME TO closed_positions;
            """
        )
        await db.commit()
        log_engine("TriageLogger: migrated closed_positions to v30 schema")

        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='latency_metrics'"
        )
        if await cursor.fetchone():
            cursor = await db.execute("PRAGMA table_info(latency_metrics)")
            lcols = {r[1] for r in await cursor.fetchall()}
            if "tick_arrival_us" not in lcols and "tick_arrival_ts" in lcols:
                await db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS latency_metrics_v30 (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        tick_arrival_us REAL NOT NULL,
                        processing_latency_us REAL NOT NULL,
                        slip_distance_points REAL,
                        spread_penalty_points REAL,
                        node_env TEXT NOT NULL,
                        epic TEXT,
                        event_type TEXT NOT NULL DEFAULT 'tick',
                        session_window TEXT,
                        created_at TEXT NOT NULL DEFAULT (datetime('now'))
                    );
                    INSERT INTO latency_metrics_v30 (
                        timestamp, tick_arrival_us, processing_latency_us,
                        slip_distance_points, spread_penalty_points, node_env,
                        epic, event_type, created_at
                    )
                    SELECT
                        tick_arrival_ts, tick_arrival_ts * 1000000.0, processing_latency_us,
                        slip_distance_pts, spread_penalty_pts, 'production',
                        epic, event_type, created_at
                    FROM latency_metrics;
                    DROP TABLE latency_metrics;
                    ALTER TABLE latency_metrics_v30 RENAME TO latency_metrics;
                    """
                )
                await db.commit()
                log_engine("TriageLogger: migrated latency_metrics to v30 schema")

    async def _worker_main(self) -> None:
        import aiosqlite

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(self._db_path), timeout=30.0) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA busy_timeout=30000;")
            await db.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            await db.execute("PRAGMA synchronous=NORMAL;")
            await db.execute("PRAGMA wal_autocheckpoint=1000;")
            await db.execute("PRAGMA temp_store=MEMORY;")
            await db.execute("PRAGMA mmap_size=268435456;")
            await db.execute("PRAGMA cache_size=-64000;")
            await db.executescript(_CLOSED_POSITIONS_DDL)
            await db.executescript(_LATENCY_METRICS_DDL)
            await db.executescript(_META_DDL)
            await db.executescript(_ML_FEATURE_EXECUTIONS_DDL)
            await self._migrate_schema_if_needed(db)
            await db.execute(
                "INSERT OR REPLACE INTO triage_meta(key, value) VALUES(?, ?)",
                ("schema_version", str(_SCHEMA_VERSION)),
            )
            await db.execute(
                "INSERT OR REPLACE INTO triage_meta(key, value) VALUES(?, ?)",
                ("node_env", resolve_node_env_label()),
            )
            await db.commit()

            while True:
                item = await asyncio.to_thread(self._queue.get)
                if item is None:
                    break
                try:
                    await self._persist(db, item)
                    self._written += 1
                except Exception as exc:
                    log_engine(
                        f"TriageLogger persist error: {type(exc).__name__}: {exc}"
                    )
                finally:
                    self._queue.task_done()

    async def _persist(self, db: Any, item: dict[str, Any]) -> None:
        await db.execute("BEGIN IMMEDIATE")
        kind = str(item.get("kind") or "")
        if kind == "closed_position":
            snap = self._session.record_closed_trade(float(item.get("net_pnl") or 0.0))
            await db.execute(
                """
                INSERT INTO closed_positions (
                    ticket, asset, size, entry_price, exit_price, direction,
                    gross_pnl, net_pnl, exit_timestamp, epic, result
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(item.get("ticket") or ""),
                    str(item.get("asset") or ""),
                    float(item.get("size") or 0.0),
                    float(item.get("entry_price") or item.get("entry") or 0.0),
                    float(item.get("exit_price") or item.get("exit") or 0.0),
                    str(item.get("direction") or item.get("execution_side") or ""),
                    float(item.get("gross_pnl") or 0.0),
                    float(item.get("net_pnl") or 0.0),
                    str(
                        item.get("exit_timestamp")
                        or item.get("closed_at")
                        or time.strftime("%Y-%m-%d %H:%M:%S")
                    ),
                    str(item.get("epic") or ""),
                    str(item.get("result") or ""),
                ),
            )
            await db.execute(
                "INSERT OR REPLACE INTO triage_meta(key, value) VALUES(?, ?)",
                ("session_performance", json.dumps(snap.to_dict(), separators=(",", ":"))),
            )
        elif kind == "latency_metric":
            lat_us = float(item.get("processing_latency_us") or 0.0)
            self._latency_ring.append(lat_us)
            if len(self._latency_ring) > 256:
                self._latency_ring = self._latency_ring[-256:]
            await db.execute(
                """
                INSERT INTO latency_metrics (
                    timestamp, tick_arrival_us, processing_latency_us,
                    slip_distance_points, spread_penalty_points, node_env,
                    epic, event_type, session_window
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    float(item.get("timestamp") or time.time()),
                    float(item.get("tick_arrival_us") or time.time() * 1_000_000.0),
                    lat_us,
                    item.get("slip_distance_points", item.get("slip_distance_pts")),
                    item.get("spread_penalty_points", item.get("spread_penalty_pts")),
                    str(item.get("node_env") or resolve_node_env_label()),
                    str(item.get("epic") or ""),
                    str(item.get("event_type") or "tick"),
                    str(item.get("session_window") or ""),
                ),
            )
        elif kind == "slippage_fill":
            analysis = item.get("analysis") or item
            await db.execute(
                """
                INSERT INTO latency_metrics (
                    timestamp, tick_arrival_us, processing_latency_us,
                    slip_distance_points, spread_penalty_points, node_env,
                    epic, event_type, session_window
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    float(item.get("timestamp") or time.time()),
                    float(item.get("tick_arrival_us") or time.time() * 1_000_000.0),
                    float(item.get("processing_latency_us") or 0.0),
                    float(analysis.get("slip_distance_points") or 0.0),
                    float(analysis.get("spread_penalty_points") or 0.0),
                    str(item.get("node_env") or resolve_node_env_label()),
                    str(analysis.get("epic") or item.get("epic") or ""),
                    "slippage_fill",
                    str(analysis.get("session_window") or ""),
                ),
            )
        elif kind == "session_snapshot":
            snap = self._session.current_snapshot()
            lat_stats = self._session.snapshot_from_latencies(self._latency_ring)
            payload = {**snap.to_dict(), **lat_stats}
            await db.execute(
                "INSERT OR REPLACE INTO triage_meta(key, value) VALUES(?, ?)",
                ("session_performance", json.dumps(payload, separators=(",", ":"))),
            )
        elif kind == "ml_feature_execution":
            await db.execute(
                """
                INSERT INTO ml_feature_executions (
                    timestamp, deal_ref, epic, direction, win_probability,
                    model_verdict, feature_vector
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    float(item.get("timestamp") or time.time()),
                    str(item.get("deal_ref") or ""),
                    str(item.get("epic") or ""),
                    str(item.get("direction") or ""),
                    float(item.get("win_probability") or 0.0),
                    str(item.get("model_verdict") or ""),
                    item.get("feature_vector"),
                ),
            )
        elif kind == "ml_feature_close":
            await db.execute(
                """
                UPDATE ml_feature_executions
                SET result = ?, net_pnl = ?
                WHERE deal_ref = ?
                """,
                (
                    str(item.get("result") or ""),
                    float(item.get("net_pnl") or 0.0),
                    str(item.get("deal_ref") or ""),
                ),
            )
        elif kind == "triage_meta":
            await db.execute(
                "INSERT OR REPLACE INTO triage_meta(key, value) VALUES(?, ?)",
                (str(item.get("key") or ""), str(item.get("value") or "")),
            )
        else:
            return
        await db.commit()

    def dispatch(self, payload: dict[str, Any]) -> bool:
        """Non-blocking enqueue — safe from trading hot-path and Worker D."""
        if not self._started:
            self.start()
        try:
            self._queue.put_nowait(payload)
            return True
        except queue.Full:
            self._dropped += 1
            return False

    def log_closed_position(self, record: ClosedPositionRecord) -> None:
        self.dispatch(
            {
                "kind": "closed_position",
                "ticket": record.ticket,
                "asset": record.asset,
                "size": record.size,
                "entry_price": record.entry_price,
                "exit_price": record.exit_price,
                "direction": record.direction,
                "gross_pnl": record.gross_pnl,
                "net_pnl": record.net_pnl,
                "exit_timestamp": record.exit_timestamp,
                "epic": record.epic,
                "result": record.result,
            }
        )

    def log_latency_metric(self, record: LatencyMetricRecord) -> None:
        self.dispatch(
            {
                "kind": "latency_metric",
                "timestamp": record.timestamp,
                "tick_arrival_us": record.tick_arrival_us,
                "processing_latency_us": record.processing_latency_us,
                "slip_distance_points": record.slip_distance_points,
                "spread_penalty_points": record.spread_penalty_points,
                "node_env": record.node_env,
                "epic": record.epic,
                "event_type": record.event_type,
                "session_window": record.session_window,
            }
        )

    def log_slippage_analysis(self, analysis: dict[str, Any]) -> None:
        self.dispatch(
            {
                "kind": "slippage_fill",
                "timestamp": time.time(),
                "tick_arrival_us": time.time() * 1_000_000.0,
                "analysis": analysis,
            }
        )

    def log_session_snapshot(self) -> None:
        self.dispatch({"kind": "session_snapshot"})

    def stats(self) -> dict[str, Any]:
        return {
            "db_path": str(self._db_path),
            "node_env": resolve_node_env_label(),
            "started": self._started,
            "queue_size": self._queue.qsize(),
            "written": self._written,
            "dropped": self._dropped,
            "session": self._session.to_dict(),
        }


_logger: TriageLogger | None = None
_logger_lock = threading.Lock()


def get_triage_logger() -> TriageLogger:
    global _logger
    with _logger_lock:
        if _logger is None:
            _logger = TriageLogger()
        return _logger


def reset_triage_logger_for_tests() -> None:
    global _logger
    with _logger_lock:
        if _logger is not None:
            _logger.stop(timeout=1.0)
        _logger = None


def dispatch_triage_event(payload: dict[str, Any]) -> bool:
    """Generic async dispatch — execution loops enqueue without blocking."""
    return get_triage_logger().dispatch(payload)


def log_trade_settlement(
    *,
    ticket: str,
    asset: str,
    epic: str,
    size: float,
    entry: float,
    exit_price: float,
    execution_side: str,
    gross_pnl: float,
    net_pnl: float,
    result: str = "",
    closed_at: str = "",
) -> None:
    """v29.1-compatible settlement hook for learning_store / execution paths."""
    get_triage_logger().log_closed_position(
        ClosedPositionRecord.from_legacy(
            ticket=str(ticket),
            asset=str(asset),
            epic=str(epic),
            size=float(size),
            entry=float(entry),
            exit=float(exit_price),
            execution_side=str(execution_side).upper(),
            gross_pnl=float(gross_pnl),
            net_pnl=float(net_pnl),
            result=str(result),
            closed_at=closed_at or time.strftime("%Y-%m-%d %H:%M:%S"),
        )
    )
    try:
        from trading.continuous_optimization_worker import get_continuous_optimization_worker

        get_continuous_optimization_worker().on_trade_closed(
            deal_ref=str(ticket),
            result=str(result),
            net_pnl=float(net_pnl),
        )
        dispatch_triage_event(
            {
                "kind": "ml_feature_close",
                "deal_ref": str(ticket),
                "result": str(result),
                "net_pnl": float(net_pnl),
            }
        )
    except Exception:
        pass


def read_triage_meta(key: str) -> str | None:
    """Synchronous meta read for optimization worker weight bootstrap."""
    path = resolve_triage_db_path()
    if not path.is_file():
        return None
    try:
        import sqlite3

        conn = sqlite3.connect(str(path), timeout=30.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=30000;")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            row = conn.execute(
                "SELECT value FROM triage_meta WHERE key = ? LIMIT 1", (str(key),)
            ).fetchone()
        finally:
            conn.close()
        return str(row[0]) if row else None
    except Exception:
        return None


def write_triage_meta(key: str, value: str) -> None:
    """Enqueue meta write — non-blocking from hot path."""
    dispatch_triage_event(
        {"kind": "triage_meta", "key": str(key), "value": str(value)}
    )


def log_tick_latency(
    *,
    epic: str,
    tick_arrival_ts: float | None = None,
    processing_latency_us: float,
    slip_distance_pts: float | None = None,
    spread_penalty_pts: float | None = None,
    session_window: str = "",
) -> None:
    """Worker D / micro-kernel latency hook (v29.1 field names accepted)."""
    ts = float(tick_arrival_ts if tick_arrival_ts is not None else time.time())
    get_triage_logger().log_latency_metric(
        LatencyMetricRecord(
            timestamp=ts,
            tick_arrival_us=ts * 1_000_000.0,
            processing_latency_us=float(processing_latency_us),
            slip_distance_points=slip_distance_pts,
            spread_penalty_points=spread_penalty_pts,
            node_env=resolve_node_env_label(),
            epic=epic,
            event_type="tick",
            session_window=session_window,
        )
    )


def log_broker_slippage(
    *,
    epic: str,
    direction: str,
    requested_price: float,
    broker_confirm: dict[str, Any],
    spread_points: float | None = None,
) -> dict[str, Any]:
    """Analyze IG fill vs request and enqueue slippage row asynchronously."""
    analysis = analyze_broker_fill_slippage(
        epic=epic,
        direction=direction,
        requested_price=requested_price,
        broker_confirm=broker_confirm,
        spread_points=spread_points,
    )
    get_triage_logger().log_slippage_analysis(analysis)
    return analysis
