"""
Background IG open-position synchronisation — aligns trade store with broker state.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from system.demo_execution_trace import trace_execution, update_demo_diagnostics
from system.engine_log import log_engine
from system.market_display import format_market_display_name
from system.pnl_math import classify_result, close_from_ig_position
from system.trade_audit import log_trade_audit
from system.ig_rest_sync_lock import ig_rest_sync_lock
from system.rate_limit_manager import get_rate_limit_manager
from system.sync_task_guard import SyncTaskGuard
from system.trade_lifecycle_bus import (
    STAGE_POSITION_CLOSED,
    STAGE_POSITION_TRACKING,
    STATUS_OK,
    get_lifecycle_bus,
)

SYNC_INTERVAL_SEC = 5.0
RATE_LIMIT_PAUSE_SEC = 60.0
FRESHNESS_SEC = 10.0
OPEN_GRACE_SEC = 45.0


@dataclass
class SyncedPosition:
    deal_id: str
    epic: str
    direction: str
    size: float
    level: float
    upl: float
    market_name: str = ""
    deal_reference: str = ""
    stop_level: float = 0.0
    limit_level: float = 0.0
    bid: float = 0.0
    offer: float = 0.0
    currency: str = ""


@dataclass
class IgSyncSnapshot:
    positions: list[SyncedPosition] = field(default_factory=list)
    by_epic: dict[str, int] = field(default_factory=dict)
    total_open: int = 0
    account_upl: float = 0.0
    last_sync_at: str = ""
    sync_status: str = "idle"
    rate_limit_paused: bool = False
    last_error: str = ""
    source: str = "ig"
    last_ig_event: str = ""
    last_closed_summary: str = ""


class IgPositionSync:
    """Polls GET /positions and reconciles LearningStore open trades."""

    def __init__(
        self,
        rest_client: Any,
        store: Any,
        *,
        epic: str = "",
        interval_seconds: float = SYNC_INTERVAL_SEC,
        on_alert: Callable[[str], None] | None = None,
        on_changed: Callable[[], None] | None = None,
        transaction_sync: Any | None = None,
        points_engine: Any | None = None,
        managed_epics: set[str] | frozenset[str] | None = None,
    ) -> None:
        self._rest = rest_client
        self._store = store
        self._txn_sync = transaction_sync
        self._points_engine = points_engine
        self._managed_epics = frozenset(managed_epics or ())
        self._epic = epic
        self._interval = interval_seconds
        self._on_alert = on_alert
        self._on_changed = on_changed
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._snapshot = IgSyncSnapshot()
        self._paused_until = 0.0
        self._last_sync_ts = 0.0
        self._last_known: dict[str, SyncedPosition] = {}
        self._seen_on_ig: set[str] = set()
        self._txn_sync_pending = False
        self._protection_attempted: set[str] = set()
        self._reconcile_lock = threading.Lock()
        self._close_log_lock = threading.Lock()
        self._recent_close_log: dict[str, float] = {}
        self._last_logged_interval: float | None = None
        self._run_guard = SyncTaskGuard("IG position sync")

    def register_on_changed(self, callback: Callable[[], None]) -> None:
        self._on_changed = callback

    def _configured_account_id(self) -> str:
        return str(getattr(self._rest, "account_id", "") or "").strip().upper()

    def _session_account_id(self) -> str:
        auth = getattr(self._rest, "_auth", None)
        tokens = getattr(auth, "tokens", None) if auth is not None else None
        return str(getattr(tokens, "account_id", "") or "").strip().upper()

    def _is_managed_epic(self, epic: str) -> bool:
        if not self._managed_epics:
            return True
        return str(epic or "").strip() in self._managed_epics

    def _positions_for_sync(self, positions: list[SyncedPosition]) -> list[SyncedPosition]:
        """Keep only agent-managed epics; IG positions are already session/account scoped."""
        if not self._managed_epics:
            return positions
        kept = [p for p in positions if self._is_managed_epic(p.epic)]
        skipped = len(positions) - len(kept)
        if skipped:
            sample = next((p for p in positions if not self._is_managed_epic(p.epic)), None)
            epic_hint = sample.epic if sample else "?"
            log_engine(
                f"IG position sync ignored {skipped} non-managed position(s) "
                f"(example epic={epic_hint}, managed={sorted(self._managed_epics)})"
            )
        return kept

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="IgPositionSync")
        self._thread.start()
        log_engine("IG position sync started")
        trace_execution("SYNC", "IgPositionSync.start", decision="background loop started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        log_engine("IG position sync stopped")

    def is_fresh(self) -> bool:
        with self._lock:
            if self._snapshot.rate_limit_paused and time.time() < self._paused_until:
                return False
            max_age = max(FRESHNESS_SEC, self._interval * 2.5)
            return (
                self._last_sync_ts > 0
                and (time.time() - self._last_sync_ts) <= max_age
                and self._snapshot.sync_status == "ok"
            )

    def count_for_epic(self, epic: str) -> int:
        with self._lock:
            return int(self._snapshot.by_epic.get(epic, 0))

    def total_open(self) -> int:
        with self._lock:
            return int(self._snapshot.total_open)

    def snapshot(self) -> IgSyncSnapshot:
        with self._lock:
            return IgSyncSnapshot(
                positions=list(self._snapshot.positions),
                by_epic=dict(self._snapshot.by_epic),
                total_open=self._snapshot.total_open,
                account_upl=self._snapshot.account_upl,
                last_sync_at=self._snapshot.last_sync_at,
                sync_status=self._snapshot.sync_status,
                rate_limit_paused=self._snapshot.rate_limit_paused,
                last_error=self._snapshot.last_error,
                source=self._snapshot.source,
                last_ig_event=self._snapshot.last_ig_event,
                last_closed_summary=self._snapshot.last_closed_summary,
            )

    def _open_mins_for_deal(self, deal_id: str) -> float | None:
        """Look up opened_at from local store and return minutes since entry."""
        if not self._store or not deal_id:
            return None
        try:
            row = self._store.find_open_by_deal_id(deal_id)
            if row is None:
                return None
            opened_raw = str(row["opened_at"] or "") if "opened_at" in row.keys() else ""
            if not opened_raw:
                return None
            opened = datetime.fromisoformat(opened_raw.replace("Z", ""))
            return max(0.0, (datetime.now() - opened).total_seconds() / 60.0)
        except Exception:
            return None

    def _position_to_dict(self, p: SyncedPosition) -> dict[str, Any]:
        return {
            "deal_id": p.deal_id,
            "epic": p.epic,
            "direction": p.direction,
            "side": p.direction,
            "size": p.size,
            "level": p.level,
            "entry": p.level,
            "upl": p.upl,
            "pnl_gbp": p.upl,
            "market_name": p.market_name,
            "deal_reference": p.deal_reference,
            "stop_level": p.stop_level,
            "limit_level": p.limit_level,
            "stop": p.stop_level or None,
            "target": p.limit_level or None,
            "bid": p.bid,
            "offer": p.offer,
            "current": p.bid if p.direction == "BUY" else p.offer,
            "currency": p.currency,
            "open_mins": self._open_mins_for_deal(p.deal_id),
        }

    def snapshot_dict(self) -> dict[str, Any]:
        s = self.snapshot()
        return {
            "source": s.source,
            "total_open": s.total_open,
            "by_epic": s.by_epic,
            "account_upl": s.account_upl,
            "last_sync_at": s.last_sync_at,
            "sync_status": s.sync_status,
            "rate_limit_paused": s.rate_limit_paused,
            "last_error": s.last_error,
            "last_ig_event": s.last_ig_event,
            "last_closed_summary": s.last_closed_summary,
            "positions": [
                self._position_to_dict(p)
                for p in s.positions
            ],
        }

    def _snapshot_confidence_pct(self) -> float | None:
        """Latest dashboard/trading-loop signal confidence (0–100), if published."""
        try:
            from api.snapshot_store import get_tick

            tick = get_tick()
            sig = tick.get("signal") if isinstance(tick, dict) else None
            if isinstance(sig, dict) and sig.get("confidence") is not None:
                return float(sig["confidence"])
        except Exception:
            pass
        return None

    def _needs_fast_position_sync(self) -> bool:
        """True when broker state or in-flight orders need frequent GET /positions."""
        from system.rest_api_budget import is_order_in_flight

        if is_order_in_flight():
            return True
        with self._lock:
            for p in self._snapshot.positions:
                if p.stop_level <= 0 or p.limit_level <= 0:
                    return True
        return False

    def _effective_interval(self) -> float:
        """
        Adaptive poll: 15s when managing open risk, 30s when flat or low signal.

        ``position_sync_seconds`` from config is not the loop wait — it only feeds
        account refresh spacing on successful sync (``max(60, interval * 4)``).
        """
        from system.config_loader import get_config

        cfg = get_config(reload=False)
        fast = float(getattr(cfg, "position_sync_open_fast_seconds", 15.0))
        relaxed = float(getattr(cfg, "position_sync_open_relaxed_seconds", 30.0))
        conf_floor = float(getattr(cfg, "position_sync_relaxed_below_confidence", 70.0))

        with self._lock:
            open_positions = int(self._snapshot.total_open)
        if open_positions <= 0:
            interval = relaxed
            reason = "flat — no positions"
        elif self._needs_fast_position_sync():
            interval = fast
            noun = "position" if open_positions == 1 else "positions"
            reason = f"{open_positions} {noun} open (protection or order in flight)"
        else:
            conf = self._snapshot_confidence_pct()
            if conf is not None and conf < conf_floor:
                interval = relaxed
                noun = "position" if open_positions == 1 else "positions"
                reason = (
                    f"{open_positions} {noun} open, signal {conf:.0f}% "
                    f"< {conf_floor:.0f}%"
                )
            else:
                interval = fast
                noun = "position" if open_positions == 1 else "positions"
                conf_s = f"{conf:.0f}%" if conf is not None else "n/a"
                reason = f"{open_positions} {noun} open (signal {conf_s})"

        if getattr(self, "_last_logged_interval", None) != interval:
            self._last_logged_interval = interval
            log_engine(f"Position sync interval: {interval:.0f}s ({reason})")
        return interval

    def sync_once(self) -> IgSyncSnapshot:
        with self._run_guard.guarded_run() as active:
            if not active:
                return self.snapshot()
            return self._sync_once_body()

    def _sync_once_body(self) -> IgSyncSnapshot:
        from system.market_watch.calendar import background_rest_paused
        from system.rest_api_budget import is_order_in_flight

        if background_rest_paused("position_sync") and not is_order_in_flight():
            with self._lock:
                self._snapshot.sync_status = "paused (market open stream priority)"
            self._update_diagnostics()
            return self.snapshot()

        mgr = get_rate_limit_manager()
        now = time.time()
        if mgr.is_rest_blocked() or now < self._paused_until:
            with self._lock:
                self._snapshot.rate_limit_paused = True
                if mgr.is_rest_blocked():
                    rem = int(mgr.seconds_until_rest_reset())
                    self._snapshot.sync_status = f"paused (IG rate limit ~{rem}s)"
                else:
                    self._snapshot.sync_status = "paused (rate limit)"
                self._snapshot.last_ig_event = self._snapshot.sync_status
            self._update_diagnostics()
            return self.snapshot()

        changed = False
        ui_refresh = False
        prev = self.snapshot()
        try:
            with ig_rest_sync_lock():
                mgr.check_rest_allowed()
                configured = self._configured_account_id()
                session_acct = self._session_account_id()
                if configured and session_acct and configured != session_acct:
                    log_engine(
                        f"IG position sync session/account mismatch: "
                        f"configured={configured} session={session_acct}"
                    )
                raw = self._rest.open_positions()
                ig_positions = self._positions_for_sync(self._parse_positions(raw))
                for p in ig_positions:
                    self._seen_on_ig.add(p.deal_id)
                changed = self._reconcile(ig_positions)
                with self._lock:
                    self._build_snapshot(ig_positions)
                    self._snapshot.sync_status = "ok"
                    self._snapshot.rate_limit_paused = False
                    self._snapshot.last_error = ""
                    self._last_sync_ts = time.time()
                    new_upl = float(self._snapshot.account_upl)
                from execution.entry_inflight import clear_entry_on_reconciled_position
                from execution.exit_inflight import clear_exit_on_reconciled_close
                from execution.pending_order_reconcile import (
                    log_unresolved_if_due,
                    reconcile_pending_via_position_state,
                )

                for epic_name, open_count in self._snapshot.by_epic.items():
                    if int(open_count) > 0:
                        clear_entry_on_reconciled_position(epic_name)
                        reconcile_pending_via_position_state(
                            epic_name, position_present=True
                        )
                if self._epic:
                    epic_open = int(self._snapshot.by_epic.get(self._epic, 0))
                    if epic_open <= 0:
                        clear_exit_on_reconciled_close(self._epic)
                        reconcile_pending_via_position_state(
                            self._epic, position_present=False
                        )
                    log_unresolved_if_due(self._epic)
                prev_ids = {p.deal_id for p in prev.positions}
                new_ids = {p.deal_id for p in ig_positions}
                if (
                    len(ig_positions) != prev.total_open
                    or prev_ids != new_ids
                    or abs(new_upl - float(prev.account_upl)) > 0.01
                ):
                    ui_refresh = True
                self._repair_missing_protection(ig_positions)
                get_lifecycle_bus().emit(
                    STAGE_POSITION_TRACKING,
                    STATUS_OK,
                    f"sync ok open={len(ig_positions)}",
                    total_open=len(ig_positions),
                    by_epic=self._snapshot.by_epic,
                )
                trace_execution(
                    "SYNC",
                    "IgPositionSync.sync_once",
                    decision=f"ok open={len(ig_positions)} changed={changed}",
                    params={"by_epic": self._snapshot.by_epic, "upl": self._snapshot.account_upl},
                )
                if hasattr(self._rest, "maybe_refresh_account_summary"):
                    try:
                        self._rest.maybe_refresh_account_summary(
                            min_interval=max(60.0, self._interval * 4)
                        )
                    except Exception:
                        pass
        except Exception as e:
            err = str(e)
            status_code = getattr(e, "status_code", None)
            if status_code == 403 or "rate limit" in err.lower():
                self._paused_until = time.time() + RATE_LIMIT_PAUSE_SEC
                with self._lock:
                    self._snapshot.rate_limit_paused = True
                    self._snapshot.sync_status = "paused (403 rate limit)"
                    self._snapshot.last_ig_event = "rate limit — sync paused 60s"
                log_engine(f"IG position sync paused 60s: {err}")
                trace_execution("SYNC", "IgPositionSync.sync_once", decision=f"rate limit: {err}")
            else:
                with self._lock:
                    self._snapshot.sync_status = f"error: {type(e).__name__}"
                    self._snapshot.last_error = err
                    self._snapshot.last_ig_event = f"sync error: {type(e).__name__}"
                log_engine(f"IG position sync error: {err}")
                trace_execution("SYNC", "IgPositionSync.sync_once", decision=f"error: {err}")
        self._update_diagnostics()
        if (changed or ui_refresh) and self._on_changed:
            try:
                self._on_changed()
            except Exception as e:
                log_engine(f"IG sync on_changed callback error: {e}")
        try:
            self._check_stale_pending_trades()
        except Exception as e:
            log_engine(f"stale pending trade check failed: {type(e).__name__}: {e}")
        return self.snapshot()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.sync_once()
            self._stop.wait(self._effective_interval())

    @staticmethod
    def _parse_positions(raw: list[dict[str, Any]]) -> list[SyncedPosition]:
        out: list[SyncedPosition] = []
        for item in raw:
            pos = item.get("position") or {}
            mkt = item.get("market") or {}
            size = float(pos.get("size") or 0)
            if size <= 0:
                continue
            deal_id = str(pos.get("dealId") or pos.get("dealID") or "")
            if not deal_id:
                continue
            out.append(
                SyncedPosition(
                    deal_id=deal_id,
                    epic=str(mkt.get("epic") or ""),
                    direction=str(pos.get("direction") or "").upper(),
                    size=size,
                    level=float(pos.get("level") or 0),
                    upl=float(pos.get("upl") or 0),
                    market_name=str(mkt.get("instrumentName") or mkt.get("instrumentType") or ""),
                    deal_reference=str(pos.get("dealReference") or ""),
                    stop_level=float(pos.get("stopLevel") or 0),
                    limit_level=float(pos.get("limitLevel") or 0),
                    bid=float(mkt.get("bid") or 0),
                    offer=float(mkt.get("offer") or 0),
                    currency=str(pos.get("currency") or mkt.get("currency") or ""),
                )
            )
        return out

    def _repair_missing_protection(self, positions: list[SyncedPosition]) -> None:
        """Back-fill take-profit / stop on IG positions that opened without full protection."""
        if not hasattr(self._rest, "ensure_protective_stops"):
            return
        from system.config_loader import get_config

        cfg = get_config()
        stop_dist = float(cfg.stop_distance_points)
        limit_dist = float(cfg.limit_distance_points)
        for p in positions:
            if p.stop_level > 0 and p.limit_level > 0:
                continue
            key = f"{p.deal_id}|{p.stop_level:.1f}|{p.limit_level:.1f}"
            if key in self._protection_attempted:
                continue
            self._protection_attempted.add(key)
            try:
                ok = self._rest.ensure_protective_stops(
                    p.deal_id,
                    epic=p.epic,
                    stop_distance=stop_dist,
                    limit_distance=limit_dist,
                )
                if ok:
                    log_engine(
                        f"IG protection repair deal={p.deal_id} "
                        f"stop={'yes' if p.stop_level > 0 else 'added'} "
                        f"limit={'yes' if p.limit_level > 0 else 'added'}"
                    )
            except Exception as e:
                log_engine(f"IG protection repair failed deal={p.deal_id}: {type(e).__name__}: {e}")

    def _build_snapshot(self, positions: list[SyncedPosition]) -> None:
        by_epic: dict[str, int] = {}
        upl = 0.0
        for p in positions:
            by_epic[p.epic] = by_epic.get(p.epic, 0) + 1
            upl += p.upl
            self._last_known[p.deal_id] = p
        self._snapshot.positions = positions
        self._snapshot.by_epic = by_epic
        self._snapshot.total_open = len(positions)
        self._snapshot.account_upl = upl
        self._snapshot.last_sync_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._snapshot.source = "ig"

    def _resolve_close_pnl(
        self,
        *,
        deal_id: str,
        deal_ref: str,
        side: str,
        entry: float,
        size: float,
        last: SyncedPosition | None,
    ) -> tuple[float, float, str, float | None, str | None]:
        """
        Resolve exit price, P&L, result, IG currency P&L, and close deal ref when available.

        IG transaction history is the source of truth; force a fetch on close
        because the transaction cache is often empty on fast closes.
        """
        ig_level = last.level if last else entry
        ig_upl = last.upl if last else 0.0
        ig_pnl_currency: float | None = None
        ig_close_deal_id: str | None = None

        if self._txn_sync:
            ig_row = None
            if hasattr(self._txn_sync, "lookup_row"):
                ig_row = self._txn_sync.lookup_row(deal_id, deal_ref)
            if ig_row is None:
                ig_pnl_currency = self._txn_sync.lookup_pnl(deal_id, deal_ref)
            else:
                ig_pnl_currency = ig_row.get("ig_pnl_currency")
                if ig_pnl_currency is not None:
                    ig_pnl_currency = float(ig_pnl_currency)
                ig_close_deal_id = str(
                    ig_row.get("ig_deal_id") or ig_row.get("deal_reference") or ""
                ).strip() or None
            if ig_pnl_currency is None:
                try:
                    self._txn_sync.sync_once(force=True, fetch_activity=True)
                except Exception as e:
                    log_engine(f"IG transaction sync on close failed: {e}")
                if hasattr(self._txn_sync, "lookup_row"):
                    ig_row = self._txn_sync.lookup_row(deal_id, deal_ref)
                    if ig_row:
                        ig_pnl_currency = ig_row.get("ig_pnl_currency")
                        if ig_pnl_currency is not None:
                            ig_pnl_currency = float(ig_pnl_currency)
                        ig_close_deal_id = str(
                            ig_row.get("ig_deal_id") or ig_row.get("deal_reference") or ""
                        ).strip() or None
                if ig_pnl_currency is None:
                    ig_pnl_currency = self._txn_sync.lookup_pnl(deal_id, deal_ref)

        if ig_pnl_currency is not None:
            pnl = float(ig_pnl_currency)
            result = classify_result(pnl)
            exit_px = ig_level if ig_level > 0 else entry
            return exit_px, pnl, result, ig_pnl_currency, ig_close_deal_id

        if last and abs(ig_upl) >= 0.05:
            # Use UPL as the currency P&L estimate, but derive index-point P&L
            # from entry/exit levels so pnl_points stays in the correct unit.
            exit_px, pnl_pts, _ = close_from_ig_position(
                side, entry, size, level=ig_level, upl=ig_upl
            )
            result = classify_result(ig_upl)
            return exit_px, pnl_pts, result, float(ig_upl), ig_close_deal_id

        exit_px, pnl_pts, result = close_from_ig_position(
            side, entry, size, level=ig_level, upl=ig_upl
        )
        if result == "BREAKEVEN" and last is None:
            result = "UNKNOWN"
        return exit_px, pnl_pts, result, None, ig_close_deal_id

    @staticmethod
    def _opened_within_seconds(opened_at: str, seconds: float) -> bool:
        text = str(opened_at or "").strip()
        if not text:
            return False
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                opened = datetime.strptime(text[:19], fmt)
                return (datetime.now() - opened).total_seconds() < seconds
            except ValueError:
                continue
        return True

    @staticmethod
    def _prefer_store_row(rows: list[Any]) -> Any:
        from data.learning_store import LearningStore

        preferred = LearningStore._prefer_execution_row(rows)
        return preferred if preferred is not None else rows[0]

    def _open_rows_for_broker_deal(
        self,
        *,
        deal_id: str,
        deal_ref: str,
    ) -> list[Any]:
        rows: list[Any] = []
        for row in self._store.active_trades():
            keys = row.keys()
            row_deal = str(row["ig_deal_id"] or "") if "ig_deal_id" in keys else ""
            row_ref = str(row["deal_reference"] or "")
            if deal_id and row_deal == deal_id:
                rows.append(row)
            elif deal_ref and row_ref == deal_ref:
                rows.append(row)
        return rows

    def _deduped_open_rows(self) -> list[Any]:
        """One local row per broker deal — prefer execution over IG_IMPORT."""
        rows = list(self._store.active_trades())
        by_deal: dict[str, Any] = {}
        by_ref: dict[str, Any] = {}
        orphans: list[Any] = []
        for row in rows:
            keys = row.keys()
            deal_id = str(row["ig_deal_id"] or "") if "ig_deal_id" in keys else ""
            ref = str(row["deal_reference"] or "")
            if deal_id:
                prev = by_deal.get(deal_id)
                by_deal[deal_id] = row if prev is None else self._prefer_store_row([prev, row])
            elif ref:
                prev = by_ref.get(ref)
                by_ref[ref] = row if prev is None else self._prefer_store_row([prev, row])
            else:
                orphans.append(row)
        out = list(by_deal.values()) + list(by_ref.values()) + orphans
        seen_ids: set[int] = set()
        unique: list[Any] = []
        for row in out:
            tid = int(row["id"])
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            unique.append(row)
        return unique

    @staticmethod
    def _close_log_key(*, deal_id: str, deal_ref: str, trade_id: int) -> str:
        if deal_id:
            return f"deal:{deal_id}"
        if deal_ref:
            return f"ref:{deal_ref}"
        return f"trade:{trade_id}"

    def _should_log_close(self, key: str) -> bool:
        now = time.time()
        with self._close_log_lock:
            last = self._recent_close_log.get(key, 0.0)
            if now - last < 3.0:
                return False
            self._recent_close_log[key] = now
            return True

    def _reconcile(self, ig_positions: list[SyncedPosition]) -> bool:
        with self._reconcile_lock:
            return self._reconcile_locked(ig_positions)

    def _reconcile_locked(self, ig_positions: list[SyncedPosition]) -> bool:
        ig_by_deal = {p.deal_id: p for p in ig_positions}
        changed = False

        for row in self._deduped_open_rows():
            trade_id = int(row["id"])
            keys = row.keys()
            deal_id = str(row["ig_deal_id"] or "") if "ig_deal_id" in keys else ""
            ref = str(row["deal_reference"] or "")
            epic = str(row["epic"] or "")
            side = str(row["side"] or "BUY")
            size = float(row["size"] or 0)
            entry = float(row["entry"] or 0)

            matched: SyncedPosition | None = None
            if deal_id and deal_id in ig_by_deal:
                matched = ig_by_deal[deal_id]
            elif ref:
                matched = next(
                    (p for p in ig_positions if p.deal_reference and p.deal_reference == ref),
                    None,
                )
                if matched and not deal_id:
                    self._store.set_ig_deal_id(trade_id, matched.deal_id)
                    deal_id = matched.deal_id

            if matched is None:
                opened_at = str(row["opened_at"] or "") if "opened_at" in keys else ""
                if not deal_id and ref:
                    continue
                if (
                    self._seen_on_ig
                    and deal_id
                    and deal_id not in self._seen_on_ig
                    and self._opened_within_seconds(opened_at, OPEN_GRACE_SEC)
                ):
                    continue
                last = self._last_known.get(deal_id) if deal_id else None
                exit_px, pnl, result, ig_pnl_currency, ig_close_deal_id = self._resolve_close_pnl(
                    deal_id=deal_id,
                    deal_ref=ref,
                    side=side,
                    entry=entry,
                    size=size,
                    last=last,
                )
                ig_level = last.level if last else entry
                ig_upl = last.upl if last else 0.0
                close_type = "manual/external close (IG)"
                if last and abs(ig_upl) > 0.05:
                    close_type = "stop/limit or manual close (IG)"
                self._store.close_trade(
                    trade_id,
                    exit_px,
                    pnl,
                    result,
                    notes=f"IG sync: {close_type} level={ig_level:.1f} upl={ig_upl:.2f}",
                    ig_pnl_currency=ig_pnl_currency,
                    ig_close_deal_id=ig_close_deal_id,
                )
                for dup in self._open_rows_for_broker_deal(deal_id=deal_id, deal_ref=ref):
                    dup_id = int(dup["id"])
                    if dup_id == trade_id:
                        continue
                    self._store.close_trade(
                        dup_id,
                        exit_px,
                        pnl,
                        result,
                        notes=f"IG sync: duplicate local row closed with deal={deal_id or ref}",
                        ig_pnl_currency=ig_pnl_currency,
                        ig_close_deal_id=ig_close_deal_id,
                    )
                log_trade_audit(
                    "position_closed",
                    source="ig_sync",
                    trade_id=trade_id,
                    epic=epic,
                    side=side,
                    entry=entry,
                    exit=exit_px,
                    pnl_points=pnl,
                    result=result,
                    ig_level=ig_level,
                    ig_upl=ig_upl,
                    deal_id=deal_id,
                )
                market_label = format_market_display_name("", epic=epic)
                with self._lock:
                    self._snapshot.last_ig_event = f"{close_type} deal={deal_id or ref} epic={epic}"
                    pnl_label = f"{pnl:+.2f}" if ig_pnl_currency is not None else f"{pnl:+.1f} pts"
                    self._snapshot.last_closed_summary = (
                        f"{market_label} {side} {result} {pnl_label} @ {datetime.now().strftime('%H:%M:%S')}"
                    )
                msg = f"IG sync closed trade id={trade_id} epic={epic} {result} {pnl_label}"
                log_key = self._close_log_key(deal_id=deal_id, deal_ref=ref, trade_id=trade_id)
                if self._should_log_close(log_key):
                    get_lifecycle_bus().mark_position_closed(
                        message=msg,
                        result=result,
                        pnl=pnl,
                        pnl_is_currency=ig_pnl_currency is not None,
                        source="ig_sync",
                        epic=epic,
                        direction=side,
                        deal_id=deal_id,
                        trade_id=trade_id,
                    )
                    if self._on_alert:
                        self._on_alert(msg)
                    else:
                        log_engine(msg)
                    trace_execution("SYNC", "IgPositionSync._reconcile", decision=msg)
                changed = True
                self._txn_sync_pending = True
                continue

            if hasattr(self._store, "update_trade_upl"):
                self._store.update_trade_upl(trade_id, matched.upl, matched.level)

            if matched.size < size - 1e-6:
                new_size = float(matched.size)
                self._store.update_trade_size(trade_id, new_size)
                self._confirm_partial_close(
                    trade_id=trade_id,
                    deal_id=deal_id,
                    side=side,
                    entry=float(entry),
                    old_size=float(size),
                    new_size=new_size,
                    px=float(matched.level or entry),
                    row=row,
                )
                with self._lock:
                    self._snapshot.last_ig_event = (
                        f"partial close deal={deal_id} size {size}->{matched.size}"
                    )
                get_lifecycle_bus().emit(
                    STAGE_POSITION_TRACKING,
                    STATUS_OK,
                    f"partial close {size}->{matched.size}",
                    trade_id=trade_id,
                    deal_id=deal_id,
                )
                trace_execution(
                    "SYNC",
                    "IgPositionSync._reconcile",
                    decision=f"partial close id={trade_id}",
                    params={"size_before": size, "size_after": matched.size},
                )
                changed = True

        for deal_id, ig_pos in ig_by_deal.items():
            if self._store.find_open_by_deal_id(deal_id):
                continue
            if ig_pos.deal_reference and self._store.find_open_by_deal_reference(ig_pos.deal_reference):
                continue
            tid = self._store.import_ig_position(
                epic=ig_pos.epic,
                market=ig_pos.market_name or ig_pos.epic,
                side=ig_pos.direction,
                entry=ig_pos.level,
                size=ig_pos.size,
                deal_id=deal_id,
                deal_reference=ig_pos.deal_reference,
                notes="IG sync: imported open position from broker",
                stop_level=ig_pos.stop_level,
                limit_level=ig_pos.limit_level,
            )
            with self._lock:
                self._snapshot.last_ig_event = f"imported position deal={deal_id} epic={ig_pos.epic}"
            log_engine(f"IG sync imported IG position dealId={deal_id} trade_id={tid}")
            trace_execution(
                "SYNC",
                "IgPositionSync._reconcile",
                decision=f"imported deal={deal_id}",
                params={"trade_id": tid, "epic": ig_pos.epic},
            )
            changed = True

        # --- Ghost position cleanup ---
        # Close any local-open rows with a confirmed IG deal_id that are no longer
        # present on IG.  This catches positions closed on IG while the bot was
        # offline (manual close, stop hit during downtime, etc.).
        for row in self._deduped_open_rows():
            keys = row.keys()
            deal_id = str(row["ig_deal_id"] or "") if "ig_deal_id" in keys else ""
            row_epic = str(row["epic"] or "") if "epic" in keys else ""
            if not self._is_managed_epic(row_epic):
                continue
            if not deal_id or deal_id in ig_by_deal:
                continue
            trade_id = int(row["id"])
            ref = str(row["deal_reference"] or "")
            opened_at = str(row["opened_at"] or "")
            # Grace period: skip positions that were just opened (might be in-flight)
            if self._opened_within_seconds(opened_at, OPEN_GRACE_SEC * 2):
                continue
            # Close broker-confirmed deals missing from IG (including after bot restart).
            side = str(row["side"] or "BUY")
            entry = float(row["entry"] or 0)
            size = float(row["size"] or 0)
            last = self._last_known.get(deal_id)
            exit_px, pnl, result, ig_pnl_currency, ig_close_deal_id = self._resolve_close_pnl(
                deal_id=deal_id, deal_ref=ref, side=side, entry=entry, size=size, last=last,
            )
            self._store.close_trade(
                trade_id, exit_px, pnl, result,
                notes=f"IG sync: ghost cleanup — position not on IG (deal={deal_id})",
                ig_pnl_currency=ig_pnl_currency,
                ig_close_deal_id=ig_close_deal_id,
            )
            with self._lock:
                self._snapshot.last_ig_event = f"ghost cleanup deal={deal_id}"
            log_engine(
                f"IG sync ghost cleanup: closed local trade id={trade_id} "
                f"deal={deal_id} (not found on IG) {result}"
            )
            changed = True

        if self._txn_sync_pending and self._txn_sync:
            try:
                self._txn_sync.request_sync(force=True, reason="position_close")
            except Exception as e:
                log_engine(f"IG transaction sync batch failed: {e}")
            self._txn_sync_pending = False

        return changed

    def _confirm_partial_close(
        self,
        *,
        trade_id: int,
        deal_id: str,
        side: str,
        entry: float,
        old_size: float,
        new_size: float,
        px: float,
        row: Any,
    ) -> None:
        if old_size <= 0 or new_size >= old_size:
            return
        from system.pnl_math import classify_result, realised_pnl_points

        closed_frac = (old_size - new_size) / old_size
        unit_pnl = realised_pnl_points(side, entry, px)
        banked_pts = unit_pnl * closed_frac
        result = classify_result(banked_pts)
        keys = row.keys()
        conf = float(row["adjusted_confidence"] or 0) if "adjusted_confidence" in keys else 0.0
        # Guard: only score once per partial close using the done-flag
        already_scored = False
        if hasattr(self._store, "is_partial_close_done"):
            try:
                already_scored = self._store.is_partial_close_done(trade_id)
            except Exception:
                pass
        if self._points_engine is not None and not already_scored:
            try:
                self._points_engine.record_trade(result, conf, banked_pts)
            except Exception as e:
                log_engine(f"points_engine partial close score failed: {type(e).__name__}: {e}")
        if hasattr(self._store, "mark_partial_close_done"):
            try:
                self._store.mark_partial_close_done(trade_id)
            except Exception:
                pass
        log_engine(
            f"PARTIAL CLOSE confirmed deal={deal_id} "
            f"size {old_size:.2f}->{new_size:.2f} banked {banked_pts:.1f} pts"
        )

    def _check_stale_pending_trades(self) -> None:
        """Warn when open trades lack IG deal confirmation for > 24 hours."""
        from system.engine_log import record_engine_warning

        cutoff = datetime.now() - timedelta(hours=24)
        for row in self._deduped_open_rows():
            keys = row.keys()
            deal_id = str(row["ig_deal_id"] or "") if "ig_deal_id" in keys else ""
            if deal_id:
                continue
            opened_raw = str(row["opened_at"] or "") if "opened_at" in keys else ""
            if not opened_raw:
                continue
            try:
                opened = datetime.fromisoformat(opened_raw.replace("Z", ""))
            except ValueError:
                continue
            if opened > cutoff:
                continue
            ref = str(row["deal_reference"] or "") if "deal_reference" in keys else ""
            warn_id = ref or f"trade-{row['id']}"
            msg = f"PENDING trade >24h — deal_id missing ({warn_id})"
            log_engine(f"WARNING: {msg}")
            record_engine_warning("pending_trade_stale", msg)

    def _update_diagnostics(self) -> None:
        s = self.snapshot()
        update_demo_diagnostics(
            ig_open_positions_total=s.total_open,
            ig_open_positions_by_epic=str(s.by_epic) if s.by_epic else "{}",
            ig_account_upl=s.account_upl,
            ig_position_sync_status=s.sync_status,
            ig_position_sync_at=s.last_sync_at,
            last_ig_event=s.last_ig_event,
            last_closed_trade_summary=s.last_closed_summary,
        )
