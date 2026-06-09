"""
Sync closed-trade P&L from IG transaction history (source of truth for realised P&L).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Callable

from system.engine_log import log_engine
from system.ig_rest_sync_lock import ig_rest_sync_lock
from system.ig_transactions import (
    build_activity_time_lookup,
    filter_rows_last_hours,
    ig_date_range_dd_mm_yyyy,
    ig_deal_key_variants,
    parse_ig_transaction_row,
)
from system.rate_limit_manager import get_rate_limit_manager
from system.sync_task_guard import SyncTaskGuard

SYNC_INTERVAL_SEC = 30.0
DISPLAY_STALE_SEC = 90.0
SOFT_MIN_GAP_SEC = 45.0
PENDING_POLL_INTERVAL_SEC = 60.0
PENDING_MIN_GAP_SEC = 55.0

_instance: "IgTransactionSync | None" = None


def get_transaction_sync_instance() -> "IgTransactionSync | None":
    """Return the running IgTransactionSync singleton (set by agent_bootstrap)."""
    return _instance


def force_immediate_transaction_sync(*, reason: str = "operator") -> bool:
    """Schedule an immediate IG transaction history sync (force=True)."""
    sync = get_transaction_sync_instance()
    if sync is None:
        return False
    return sync.request_sync(force=True, reason=reason)


def _set_transaction_sync_instance(sync: "IgTransactionSync | None") -> None:
    global _instance
    _instance = sync


class IgTransactionSync:
    """Polls IG /history/transactions and updates store + UI cache."""

    def __init__(
        self,
        rest_client: Any,
        store: Any,
        *,
        interval_seconds: float = SYNC_INTERVAL_SEC,
        min_gap_seconds: float = 300.0,
        on_changed: Callable[[], None] | None = None,
        epic_filter: str = "",
        history_days: int = 2,
        display_hours: float = 24.0,
        display_stale_seconds: float = DISPLAY_STALE_SEC,
    ) -> None:
        self._rest = rest_client
        self._store = store
        self._interval = interval_seconds
        self._min_gap_seconds = max(60.0, float(min_gap_seconds))
        self._display_stale_seconds = max(30.0, float(display_stale_seconds))
        self._on_changed = on_changed
        self._epic_filter = epic_filter
        self._history_days = max(1, int(history_days))
        self._display_hours = display_hours
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._request_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._display_rows: list[dict[str, Any]] = []
        self._last_sync = ""
        self._last_sync_ts = 0.0
        self._last_error = ""
        self._last_skip_log_ts = 0.0
        self._run_guard = SyncTaskGuard("IG transaction sync")

    def _log_skip(self, reason: str, *, force: bool = False) -> None:
        """Throttled engine log when a sync is skipped (rate limit, gap, market gate)."""
        if force:
            return
        now = time.time()
        if now - self._last_skip_log_ts < 60.0:
            return
        self._last_skip_log_ts = now
        log_engine(f"IG transaction sync skipped ({reason})")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="IgTransactionSync"
        )
        self._thread.start()
        log_engine("IG transaction sync started")

        from system.config_loader import get_config
        from system.market_watch.calendar import is_market_open

        cfg = get_config()
        epic = str(self._epic_filter or cfg.epic)
        startup_defer_sec = 120.0

        def _startup_sync() -> None:
            if not self._stop.is_set():
                self.request_sync(force=True, reason="startup")

        if epic and is_market_open(epic):
            pause_sec = int(startup_defer_sec)
            log_engine(
                f"Startup transaction sync deferred {pause_sec}s — market open at launch"
            )
            timer = threading.Timer(startup_defer_sec, _startup_sync)
            timer.daemon = True
            timer.start()
        else:
            self.request_sync(force=True, reason="startup")

    def stop(self) -> None:
        self._stop.set()
        if self._request_thread and self._request_thread.is_alive():
            self._request_thread.join(timeout=5.0)
            self._request_thread = None
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None

    def cache_age_seconds(self) -> float:
        with self._lock:
            if self._last_sync_ts <= 0:
                return float("inf")
            return max(0.0, time.time() - self._last_sync_ts)

    def is_display_stale(self) -> bool:
        return self.cache_age_seconds() > self._display_stale_seconds

    def request_sync(self, *, force: bool = False, reason: str = "") -> bool:
        """
        Schedule a background history fetch when appropriate (non-blocking).

        force=True bypasses min-gap (position close, startup, verify).
        Otherwise refreshes only when the display cache is stale and soft gap elapsed.
        """
        if self._stop.is_set():
            self._log_skip("sync stopped", force=force)
            return False
        from system.rest_api_budget import order_in_flight_paused

        if order_in_flight_paused("transaction_history"):
            self._log_skip("order in flight — transaction sync deferred", force=force)
            return False
        mgr = get_rate_limit_manager()
        if mgr.is_rest_blocked():
            rem = int(mgr.seconds_until_rest_reset())
            self._log_skip(f"IG REST rate limit ~{rem}s remaining", force=force)
            return False

        from system.config_loader import get_config
        from system.market_watch.calendar import (
            allow_rest_activity,
            seconds_until_transaction_history_resume,
            transaction_history_open_pause_active,
        )

        cfg = get_config()
        epic = str(getattr(self, "_epic_filter", "") or cfg.epic)
        if transaction_history_open_pause_active():
            rem = int(seconds_until_transaction_history_resume())
            self._log_skip(
                f"Transaction sync paused — market open window ({rem}s remaining)",
                force=force,
            )
            return False

        if not force:
            if epic and not allow_rest_activity("transaction_history", epic):
                self._log_skip("market closed for transaction_history")
                return False

        now = time.time()
        effective_last = (
            self._last_sync_ts if self._last_sync_ts > 0 else (now - SOFT_MIN_GAP_SEC)
        )
        if not force:
            if now - effective_last < SOFT_MIN_GAP_SEC:
                self._log_skip(f"soft gap {SOFT_MIN_GAP_SEC:.0f}s")
                return False
            if now - effective_last < self._min_gap_seconds:
                if not self.is_display_stale():
                    self._log_skip(f"min gap {self._min_gap_seconds:.0f}s, cache fresh")
                    return False
            elif self._last_sync_ts > 0 and not self.is_display_stale():
                self._log_skip("display cache fresh")
                return False

        if self._request_thread and self._request_thread.is_alive():
            return False

        if reason:
            log_engine(f"IG transaction sync scheduled ({reason}, force={force})")

        def work() -> None:
            try:
                self.sync_once(
                    force=force, fetch_activity=force or self.is_display_stale()
                )
            except Exception as e:
                log_engine(
                    f"IG transaction sync request failed ({reason}): {type(e).__name__}: {e}"
                )

        self._request_thread = threading.Thread(
            target=work,
            daemon=True,
            name="IgTxnSyncRequest",
        )
        self._request_thread.start()
        return True

    def sync_once(
        self,
        *,
        force: bool = False,
        fetch_activity: bool = True,
    ) -> int:
        with self._run_guard.guarded_run() as active:
            if not active:
                return 0
            return self._sync_once_body(force=force, fetch_activity=fetch_activity)

    def _sync_once_body(
        self,
        *,
        force: bool = False,
        fetch_activity: bool = True,
    ) -> int:
        mgr = get_rate_limit_manager()
        if mgr.is_rest_blocked():
            rem = int(mgr.seconds_until_rest_reset())
            self._log_skip(f"IG REST rate limit ~{rem}s remaining", force=force)
            return 0
        from system.rest_api_budget import order_in_flight_paused

        if order_in_flight_paused("transaction_history"):
            self._log_skip("order in flight — transaction sync deferred", force=force)
            return 0
        try:
            mgr.check_rest_allowed()
        except Exception as e:
            self._log_skip(f"REST blocked ({type(e).__name__})", force=force)
            return 0

        from system.market_watch.calendar import (
            seconds_until_transaction_history_resume,
            transaction_history_open_pause_active,
        )

        if transaction_history_open_pause_active():
            rem = int(seconds_until_transaction_history_resume())
            self._log_skip(
                f"Transaction sync paused — market open window ({rem}s remaining)",
                force=force,
            )
            return 0

        now = time.time()
        # Apply gap check unconditionally — including the first call after startup
        # or rate-limit recovery. When _last_sync_ts is 0 (never run), treat it as
        # if a sync ran SOFT_MIN_GAP_SEC ago so the first call must wait only that
        # gap rather than firing immediately alongside other startup REST traffic.
        effective_last = (
            self._last_sync_ts if self._last_sync_ts > 0 else (now - SOFT_MIN_GAP_SEC)
        )
        if not force:
            if now - effective_last < SOFT_MIN_GAP_SEC:
                self._log_skip(f"soft gap {SOFT_MIN_GAP_SEC:.0f}s", force=force)
                return 0
            if now - effective_last < self._min_gap_seconds:
                self._log_skip(f"min gap {self._min_gap_seconds:.0f}s", force=force)
                return 0

        start, end = ig_date_range_dd_mm_yyyy(days_back=self._history_days)
        try:
            with ig_rest_sync_lock():
                # Re-check inside the lock: a thread that was queued behind another
                # sync may have been waiting while that sync triggered a rate limit.
                if mgr.is_rest_blocked():
                    rem = int(mgr.seconds_until_rest_reset())
                    self._log_skip(
                        f"IG REST rate limit ~{rem}s remaining (inside lock)",
                        force=force,
                    )
                    return 0
                txns = self._rest.fetch_transactions(
                    start,
                    end,
                    transaction_type="ALL_DEAL",
                    page_size=500,
                )
                activity_times: dict[str, str] = {}
                if fetch_activity and hasattr(self._rest, "fetch_account_activity"):
                    try:
                        activities = self._rest.fetch_account_activity(start, end)
                        activity_times = build_activity_time_lookup(activities)
                    except Exception:
                        activity_times = {}
            self._last_error = ""
        except Exception as e:
            self._last_error = f"{type(e).__name__}: {e}"
            log_engine(f"IG transaction fetch failed: {self._last_error}")
            if self._on_changed:
                self._on_changed()
            return 0

        rows: list[dict[str, Any]] = []
        updated = 0
        for txn in txns:
            row = parse_ig_transaction_row(
                txn,
                epic_filter=self._epic_filter,
                activity_times=activity_times,
            )
            if not row:
                continue
            rows.append(row)
            if hasattr(self._store, "ingest_ig_closed_transaction"):
                if self._store.ingest_ig_closed_transaction(row):
                    updated += 1
            elif hasattr(self._store, "apply_ig_transaction_pnl"):
                close_ref = str(
                    row.get("ig_deal_id") or row.get("deal_reference") or ""
                )
                if self._store.apply_ig_transaction_pnl(
                    row.get("deal_reference") or "",
                    row.get("ig_deal_id") or "",
                    float(row["ig_pnl_currency"] or 0),
                    row.get("result") or "CLOSED",
                    ig_close_deal_id=close_ref,
                ):
                    updated += 1

        rows.sort(key=lambda r: str(r.get("closed_at") or ""), reverse=True)
        rows = filter_rows_last_hours(rows, self._display_hours)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self._display_rows = rows
            self._last_sync = now_str
            self._last_sync_ts = time.time()

        reconciled = self._reconcile_open_close_deals(rows)
        if reconciled:
            updated += reconciled

        if rows:
            refs = [
                str(r.get("deal_reference") or r.get("ig_deal_id") or "")[:16]
                for r in rows[:12]
            ]
            ref_txt = ", ".join(r for r in refs if r)
            log_engine(
                f"IG transaction sync: {len(rows)} closed deals (last {self._display_hours:.0f}h)"
                + (f" [{ref_txt}]" if ref_txt else "")
            )
        elif txns:
            log_engine(
                f"IG transaction sync: {len(txns)} raw txns, 0 deal closes in window"
            )

        if (updated or rows) and self._on_changed:
            self._on_changed()
        return len(rows)

    @staticmethod
    def _parse_closed_ts(raw: str) -> datetime | None:
        text = str(raw or "").strip()
        if not text:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                part = text[:19] if " " in fmt else text[:10]
                return datetime.strptime(part, fmt)
            except ValueError:
                continue
        return None

    def _row_keys(self, *values: str) -> set[str]:
        keys: set[str] = set()
        for val in values:
            text = str(val or "").strip()
            if text:
                keys.update(ig_deal_key_variants(text))
        return keys

    def lookup_row(
        self,
        deal_id: str = "",
        deal_reference: str = "",
        *,
        ig_close_deal_id: str = "",
    ) -> dict[str, Any] | None:
        """Find a cached IG transaction row by open id, order ref, or close deal id."""
        keys = self._row_keys(deal_id, deal_reference, ig_close_deal_id)
        if not keys:
            return None
        with self._lock:
            for row in self._display_rows:
                if keys & self._row_keys(
                    str(row.get("ig_deal_id") or ""),
                    str(row.get("deal_reference") or ""),
                ):
                    return dict(row)
        return None

    def lookup_pnl(self, deal_id: str = "", deal_reference: str = "") -> float | None:
        """Find realised P&L for a deal from the latest cache."""
        row = self.lookup_row(deal_id, deal_reference)
        if not row:
            return None
        v = row.get("ig_pnl_currency")
        return float(v) if v is not None else None

    def _reconcile_open_close_deals(self, ig_rows: list[dict[str, Any]]) -> int:
        """
        Link local open deal ids to IG close transaction refs and confirm ig_pnl_currency.
        """
        if not self._store or not hasattr(self._store, "recent_closed_trades"):
            return 0
        from system.account_currency import account_currency_symbol

        local_rows = self._store.recent_closed_trades(120)
        claimed_close_refs: set[str] = set()
        reconciled = 0
        sym = account_currency_symbol()

        for local in local_rows:
            if local.get("ig_pnl_currency") is not None:
                continue
            open_id = str(local.get("ig_deal_id") or "").strip()
            order_ref = str(local.get("deal_reference") or "").strip()
            close_id = str(local.get("ig_close_deal_id") or "").strip()
            ig_row = self.lookup_row(open_id, order_ref, ig_close_deal_id=close_id)
            if ig_row is None:
                ig_row = self._heuristic_match(local, ig_rows, claimed_close_refs)
            if not ig_row:
                continue
            close_ref = str(
                ig_row.get("ig_deal_id") or ig_row.get("deal_reference") or ""
            )
            if close_ref:
                claimed_close_refs.add(close_ref.upper())
            pnl = float(ig_row.get("ig_pnl_currency") or 0)
            result = str(ig_row.get("result") or "")
            if hasattr(self._store, "apply_ig_transaction_pnl"):
                ok = self._store.apply_ig_transaction_pnl(
                    order_ref or open_id,
                    open_id or order_ref,
                    pnl,
                    result,
                    ig_close_deal_id=close_ref,
                )
            else:
                ok = False
            if ok:
                reconciled += 1
                log_engine(
                    f"Deal reconciled: open={open_id[:16] or order_ref[:16]} "
                    f"close={close_ref[:16]} — IG P&L confirmed {sym}{pnl:+.2f}"
                )
                try:
                    from trading.trade_autopsy import write_trade_autopsy

                    deal_key = open_id or order_ref or close_ref
                    write_trade_autopsy(
                        deal_key,
                        {
                            "entry_time": local.get("opened_at"),
                            "exit_time": local.get("closed_at"),
                            "side": local.get("side"),
                            "size": local.get("size"),
                            "entry_price": local.get("entry"),
                            "exit_price": local.get("exit"),
                            "setup_key": local.get("setup_key"),
                            "confidence_at_entry": local.get("adjusted_confidence"),
                            "ml_score_at_entry": local.get("ml_score"),
                            "fitness_score_at_entry": local.get("fitness_score"),
                            "atr_at_entry": local.get("atr_at_entry"),
                            "trail_exit_vs_fixed_target": local.get(
                                "trail_exit_vs_fixed"
                            ),
                            "partial_close_vs_hold": local.get("partial_close_info"),
                            "session_time_at_entry": local.get("session_time"),
                            "points_state_at_entry": local.get("points_state"),
                            "regime_at_entry": local.get("regime"),
                            "replay_prediction_vs_actual": None,
                            "exit_reason": "ig_transaction_sync",
                            "pnl_gbp": pnl,
                            "pnl_pts": local.get("pnl_points"),
                            "deal_id": deal_key,
                        },
                        ig_pnl_currency_confirmed=bool(ig_row.get("ig_pnl_currency")),
                        source=str(local.get("source") or ""),
                    )
                except Exception:
                    pass
        # Sweep stale UNCONFIRMED trades (>24h old) → CANCELLED
        if hasattr(self._store, "mark_stale_unconfirmed_as_cancelled"):
            try:
                cancelled = self._store.mark_stale_unconfirmed_as_cancelled(
                    after_hours=24.0
                )
                if cancelled:
                    log_engine(
                        f"IG transaction sync: {cancelled} stale UNCONFIRMED trade(s) "
                        "marked CANCELLED (>24h, no IG history match)"
                    )
            except Exception:
                pass

        return reconciled

    @staticmethod
    def _heuristic_match(
        local: dict[str, Any],
        ig_rows: list[dict[str, Any]],
        claimed: set[str],
    ) -> dict[str, Any] | None:
        """Match open DIAAA deal to IG close ref when ids differ (e.g. LBH6GFAG)."""
        epic = str(local.get("epic") or "")
        closed_dt = IgTransactionSync._parse_closed_ts(
            str(local.get("closed_at") or "")
        )
        open_id = str(local.get("ig_deal_id") or "").strip()
        if not closed_dt or not open_id:
            return None
        best: dict[str, Any] | None = None
        best_delta = 999999.0
        for ig in ig_rows:
            close_ref = str(
                ig.get("ig_deal_id") or ig.get("deal_reference") or ""
            ).upper()
            if not close_ref or close_ref in claimed:
                continue
            if epic and str(ig.get("epic") or "") and str(ig.get("epic") or "") != epic:
                continue
            ig_dt = IgTransactionSync._parse_closed_ts(str(ig.get("closed_at") or ""))
            if not ig_dt:
                continue
            delta = abs((closed_dt - ig_dt).total_seconds())
            if delta <= 900 and delta < best_delta:
                best = ig
                best_delta = delta
        return best

    def get_display_rows(
        self,
        limit: int = 50,
        *,
        hours: float | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._display_rows)
        if hours is not None and hours > 0:
            rows = filter_rows_last_hours(rows, hours)
        return rows[:limit]

    @property
    def last_sync_at(self) -> str:
        with self._lock:
            return self._last_sync

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    def _has_pending_trades(self) -> bool:
        """Return True if any closed strategy trades still lack IG-confirmed P&L."""
        if self._store is None or not hasattr(
            self._store, "count_unconfirmed_closed_trades"
        ):
            return False
        try:
            return self._store.count_unconfirmed_closed_trades() > 0
        except Exception:
            return False

    def _loop(self) -> None:
        # Initial wait before first poll — use full interval so startup REST burst is avoided.
        if self._stop.wait(self._interval):
            return
        while not self._stop.is_set():
            has_pending = self._has_pending_trades()
            if has_pending:
                # Temporarily lower min_gap so the shorter interval actually fires.
                saved = self._min_gap_seconds
                self._min_gap_seconds = PENDING_MIN_GAP_SEC
                try:
                    self.request_sync(force=False, reason="pending-interval")
                finally:
                    self._min_gap_seconds = saved
            else:
                self.request_sync(force=False, reason="interval")
            wait_sec = PENDING_POLL_INTERVAL_SEC if has_pending else self._interval
            if self._stop.wait(wait_sec):
                break
