"""
Throttled IG connection keepalive and status helpers for the Pro GUI strip.

Prevents hammering GET /accounts on every UI tick (major cause of IG rate limits).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from system.config_loader import get_config
from system.engine_log import log_engine
from system.market_data_hub import get_market_data_hub
from system.rate_limit_manager import get_rate_limit_manager

_KEEPALIVE_INTERVAL_SEC = 90.0
_last_keepalive_ts = 0.0
_sync_thread: threading.Thread | None = None
_preview_thread: threading.Thread | None = None
_flatten_thread: threading.Thread | None = None
_last_pre_close_flatten_ts = 0.0
_last_close_transition_flatten_ts = 0.0


def _run_position_sync(sync: Any) -> None:
    try:
        sync.sync_once()
    except Exception as e:
        log_engine(f"keepalive position sync failed: {type(e).__name__}: {e}")


def maybe_refresh_demo_on_market_open(bot: Any) -> None:
    """Detect closed->open transition and restart stale DEMO sessions (FIX 1)."""
    try:
        cfg = get_config()
        if not cfg.market_watch_enabled:
            return
        from system.market_watch.calendar import detect_market_closed_to_open_transition

        if not detect_market_closed_to_open_transition(cfg.epic):
            return
        from system.market_watch.calendar import begin_market_open_rest_pause

        begin_market_open_rest_pause()
        refresh = getattr(bot, "refresh_demo_session_on_market_open", None)
        if callable(refresh):
            threading.Thread(
                target=refresh,
                daemon=True,
                name="MarketOpenDemoRefresh",
            ).start()
    except Exception as e:
        log_engine(f"Market open DEMO refresh check failed: {type(e).__name__}: {e}")


def _play_error_alert() -> None:
    """Audible alert for operator-visible session-end flatten failures."""

    def _beep() -> None:
        try:
            import sys

            sys.stdout.write("\a")
            sys.stdout.flush()
        except Exception:
            pass
        try:
            import platform
            import subprocess

            if platform.system() == "Darwin":
                subprocess.Popen(
                    ["afplay", "/System/Library/Sounds/Basso.aiff"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass

    threading.Thread(target=_beep, daemon=True, name="SessionEndAlert").start()


def _persist_session_end_journal(bot: Any, *, epic: str, closed: int, reason: str) -> None:
    journal = getattr(bot, "journal", None)
    if journal is None or not hasattr(journal, "write"):
        return
    daily_pnl = 0.0
    store = getattr(bot, "store", None)
    if store is not None and hasattr(store, "sum_daily_pnl"):
        from system.pnl_calculator import _today_str

        try:
            daily_pnl = float(store.sum_daily_pnl(_today_str()))
        except Exception:
            daily_pnl = 0.0
    try:
        journal.write(
            market="session_end",
            epic=epic,
            signal="FLAT",
            quote=0.0,
            raw_confidence=0.0,
            adjusted_confidence=0.0,
            learning_delta=0.0,
            setup_key="session_end",
            action="SESSION_END_FLATTEN",
            notes=f"closed={closed} daily_pnl={daily_pnl:.2f} reason={reason}",
        )
    except Exception as e:
        log_engine(f"Session end journal write failed: {type(e).__name__}: {e}")


def _flatten_allowed(bot: Any) -> bool:
    from execution.types import ExecutionMode

    cfg = get_config()
    if not cfg.auto_flatten_on_session_end:
        return False
    mode = getattr(bot, "_mode_enum", None)
    if mode == ExecutionMode.DEMO:
        return bool(getattr(bot, "_demo_session_active", False)) or bool(
            getattr(bot.state, "running", False)
        )
    if mode == ExecutionMode.LIVE:
        return bool(cfg.allow_live_trading)
    return False


def _run_session_end_flatten(bot: Any, *, reason: str) -> None:
    try:
        cfg = get_config()
        epic = getattr(bot.state, "epic", "") or cfg.epic
        rest = getattr(bot, "_rest", None)
        if not epic or rest is None or not hasattr(rest, "flatten_epic_positions"):
            log_engine(f"ERROR: Session end flatten skipped — REST unavailable epic={epic}")
            return
        open_count = 0
        if hasattr(rest, "count_open_positions"):
            open_count = int(rest.count_open_positions(epic))
        elif hasattr(rest, "open_positions"):
            open_count = sum(
                1
                for item in rest.open_positions()
                if (item.get("market") or {}).get("epic") == epic
            )
        if open_count <= 0:
            return
        log_engine(f"Session end approaching — closing {open_count} positions ({reason})")
        closed = int(rest.flatten_epic_positions(epic))
        remaining = int(rest.count_open_positions(epic)) if hasattr(rest, "count_open_positions") else 0
        if remaining > 0:
            log_engine(
                f"ERROR: Session end flatten incomplete — {remaining} positions remain on {epic}"
            )
            _play_error_alert()
            return
        log_engine(f"Session end flatten complete — positions=0 ({reason})")
        _persist_session_end_journal(bot, epic=epic, closed=closed, reason=reason)
        sync = getattr(bot, "ensure_position_sync", None)
        if callable(sync):
            try:
                sync_obj = sync()
                if sync_obj is not None:
                    sync_obj.sync_once()
            except Exception as e:
                log_engine(f"Session end flatten reconcile failed: {type(e).__name__}: {e}")
    except Exception as e:
        log_engine(f"ERROR: Session end flatten failed — {type(e).__name__}: {e}")
        _play_error_alert()


def maybe_flatten_on_session_end(bot: Any) -> None:
    """Flatten open epic positions before session close (DEMO by default)."""
    global _flatten_thread, _last_pre_close_flatten_ts, _last_close_transition_flatten_ts

    try:
        cfg = get_config()
        if not cfg.market_watch_enabled or not _flatten_allowed(bot):
            return
        from system.market_watch.calendar import (
            detect_market_open_to_closed_transition,
            is_session_end_flatten_window,
        )

        epic = getattr(bot.state, "epic", "") or cfg.epic
        if not epic:
            return

        now = time.time()
        trigger = False
        reason = ""
        if is_session_end_flatten_window(epic) and now - _last_pre_close_flatten_ts >= 120.0:
            trigger = True
            reason = "T-5m pre-close window"
            _last_pre_close_flatten_ts = now
        elif detect_market_open_to_closed_transition(epic) and now - _last_close_transition_flatten_ts >= 60.0:
            trigger = True
            reason = "market closed transition"
            _last_close_transition_flatten_ts = now

        if not trigger:
            return
        t = _flatten_thread
        if t is not None and t.is_alive():
            return

        def work() -> None:
            _run_session_end_flatten(bot, reason=reason)

        _flatten_thread = threading.Thread(
            target=work, daemon=True, name="SessionEndFlatten"
        )
        _flatten_thread.start()
    except Exception as e:
        log_engine(f"Session end flatten check failed: {type(e).__name__}: {e}")


def run_connection_keepalive(bot: Any) -> None:
    """
    Periodic REST + position sync while DEMO/LIVE runs (UI thread safe).
    Spaced to stay under IG allowance alongside stream and sync loops.
    """
    global _last_keepalive_ts, _sync_thread
    if not getattr(bot.state, "running", False):
        return
    from system.market_watch.calendar import background_rest_paused

    if background_rest_paused("keepalive_sync"):
        return
    mgr = get_rate_limit_manager()
    if mgr.is_rest_blocked():
        return
    now = time.time()
    if now - _last_keepalive_ts < _KEEPALIVE_INTERVAL_SEC:
        return
    _last_keepalive_ts = now

    tracker = getattr(bot, "trade_tracker", None)
    sync = getattr(tracker, "_position_sync", None) if tracker else None
    if sync is not None:
        try:
            if not sync.is_fresh():
                t = _sync_thread
                if t is None or not t.is_alive():
                    _sync_thread = threading.Thread(
                        target=_run_position_sync,
                        args=(sync,),
                        daemon=True,
                        name="KeepalivePositionSync",
                    )
                    _sync_thread.start()
        except Exception as e:
            log_engine(f"keepalive position sync schedule failed: {type(e).__name__}")

    # Transaction history has its own background thread — do not double-poll here.

    # Account summary refreshes on position-sync tick — avoid duplicate GET /accounts here.


def schedule_wire_bot_shared_rest(
    bot: Any,
    *,
    on_done: Callable[[bool], None] | None = None,
) -> None:
    """Wire shared REST on a worker thread — safe from Tk callbacks after OK / Start DEMO."""

    def work() -> None:
        ok = False
        try:
            ok = wire_bot_shared_rest(bot)
        except Exception as e:
            log_engine(f"wire_bot_shared_rest failed: {type(e).__name__}: {e}")
        if on_done is not None:
            try:
                on_done(ok)
            except Exception as e:
                log_engine(f"wire_bot_shared_rest callback failed: {type(e).__name__}: {e}")

    threading.Thread(target=work, daemon=True, name="WireSharedRest").start()


def wire_bot_shared_rest(bot: Any) -> bool:
    """
    Attach the process-wide authenticated IG REST client to the bot.

    Startup pipeline authenticates via ensure_shared_authenticated but does not
    set bot._rest; without this the status strip shows DISCONNECTED after OK.
    """
    rest = getattr(bot, "_rest", None)
    if rest is not None:
        session = getattr(rest, "session", None)
        if session and getattr(session, "is_valid", False):
            return True
    try:
        from system.credentials_holder import get_credentials_holder
        from system.ig_rest_session import ensure_shared_authenticated

        holder = get_credentials_holder()
        if not holder.live_allowed or not holder.credentials:
            return False
        creds = holder.credentials
        if creds.account_type not in ("DEMO", "LIVE"):
            return False
        rest = ensure_shared_authenticated(creds)
        bot._rest = rest
        cfg = get_config()
        hub = get_market_data_hub()
        hub.attach_rest(rest)
        hub.set_min_fetch_interval(float(cfg.stream_poll_seconds))
        return True
    except Exception:
        return False


def hub_quotes_fresh(bot: Any, *, max_age: float = 20.0) -> bool:
    try:
        epic = getattr(bot.state, "epic", "") or get_config().epic
        if not epic:
            return False
        return get_market_data_hub().is_fresh(epic, max_age=max_age)
    except Exception:
        return False


def refresh_preview_quotes(bot: Any) -> None:
    """
    Poll IG prices for the status strip when MODE OFF (preview / idle).

    Also used while DEMO is starting if the stream is not connected yet.
    """
    from system.degraded_mode import allow_rest_operation
    from system.market_watch.calendar import allow_rest_activity

    if not allow_rest_operation("preview_quote"):
        return
    mgr = get_rate_limit_manager()
    if mgr.is_rest_blocked():
        return
    stream = getattr(bot, "stream", None)
    if getattr(bot.state, "running", False) and stream is not None:
        conn = getattr(stream, "state", None)
        conn_val = getattr(conn, "value", str(conn)) if conn is not None else ""
        if conn_val == "connected":
            return
    try:
        cfg = get_config()
        epic = getattr(bot.state, "epic", "") or cfg.epic
        if not epic:
            return
        if not allow_rest_activity(
            "preview_quote",
            epic,
            bot_running=bool(getattr(bot.state, "running", False)),
        ):
            return
        if not wire_bot_shared_rest(bot):
            return
        rest = getattr(bot, "_rest", None)
        hub = get_market_data_hub()
        hub.fetch_if_stale(epic, min_interval=float(cfg.stream_poll_seconds))
    except Exception:
        pass


def schedule_preview_quotes(bot: Any) -> None:
    """Non-blocking preview quote fetch — must not run IG REST on the Tk main thread."""
    global _preview_thread
    t = _preview_thread
    if t is not None and t.is_alive():
        return

    def work() -> None:
        try:
            refresh_preview_quotes(bot)
        except Exception as e:
            log_engine(f"Preview quotes background fetch failed: {type(e).__name__}: {e}")

    _preview_thread = threading.Thread(target=work, daemon=True, name="PreviewQuotes")
    _preview_thread.start()
