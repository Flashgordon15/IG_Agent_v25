"""
Unified data-feed orchestrator — first-past-the-post Yahoo / Finnhub / Twelve Data.

Primary feeds drive ML and signals; IG REST is execution-only and must not block boot.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

from system.engine_log import log_engine
from system.market_data_hub import NIGHT_MATRIX_EPICS, get_market_data_hub, night_matrix_signal_fresh_count

_PRIMARY_ORDER = ("yahoo", "finnhub", "twelve_data")
_SIGNAL_MAX_AGE_SEC = float(os.environ.get("IG_SIGNAL_QUOTE_MAX_AGE_SEC", "45"))
_BOOTSTRAP_EPICS = NIGHT_MATRIX_EPICS  # stagger-sync all night-matrix epics at boot
_BOOTSTRAP_EPIC_GAP_SEC = float(os.environ.get("IG_YAHOO_BOOTSTRAP_EPIC_GAP_SEC", "0.35"))

_lock = threading.Lock()
_started = False
_retry_thread: threading.Thread | None = None
_retry_stop = threading.Event()
_state: dict[str, Any] = {
    "primary_feed": "",
    "fallback_active": False,
    "health": "offline",
    "feeds": {},
    "last_error": "",
    "retry_counts": {},
    "fresh_count": 0,
    "total_epics": len(NIGHT_MATRIX_EPICS),
    "started_at": "",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _hub_fresh_counts(*, max_age: float = _SIGNAL_MAX_AGE_SEC) -> tuple[int, int, str]:
    fresh, total = night_matrix_signal_fresh_count(max_age_sec=max_age)
    primary = ""
    hub = get_market_data_hub()
    for epic in NIGHT_MATRIX_EPICS:
        snap = hub.get_snapshot(epic)
        if snap is None or snap.bid <= 0 or snap.offer <= 0:
            continue
        if snap.age_seconds() > max_age:
            continue
        src = str(snap.source or "").lower()
        if not primary:
            if "yahoo" in src:
                primary = "yahoo"
            elif "finnhub" in src:
                primary = "finnhub"
            elif "twelve" in src:
                primary = "twelve_data"
            elif src not in ("ig_execution", "execution"):
                primary = src or "unknown"
    if not primary and fresh > 0:
        primary = "yahoo"
    return fresh, total, primary


def _yahoo_hub_fresh_count(*, max_age: float = _SIGNAL_MAX_AGE_SEC) -> int:
    hub = get_market_data_hub()
    count = 0
    for epic in NIGHT_MATRIX_EPICS:
        snap = hub.get_snapshot(epic)
        if snap is None or snap.bid <= 0 or snap.offer <= 0:
            continue
        if snap.age_seconds() > max_age:
            continue
        if "yahoo" in str(snap.source or "").lower():
            count += 1
    return count


def _yahoo_last_hub_tick_iso() -> str:
    """Latest Yahoo-sourced hub quote timestamp (not wall-clock now)."""
    try:
        from datetime import datetime, timezone

        hub = get_market_data_hub()
        latest = 0.0
        for epic in NIGHT_MATRIX_EPICS:
            snap = hub.get_snapshot(epic)
            if snap is None or float(snap.bid or 0) <= 0:
                continue
            if "yahoo" not in str(snap.source or "").lower():
                continue
            latest = max(latest, float(snap._reference_epoch()))
        if latest > 0:
            return datetime.fromtimestamp(latest, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            )
    except Exception:
        pass
    return ""


def _feed_record(name: str) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "name": name,
        "health": "offline",
        "last_error": "",
        "retry_count": 0,
        "last_tick_at": "",
        "alive": False,
    }
    if name == "yahoo":
        try:
            from feeder.yahoo_quote_poller import get_yahoo_quote_poller, yahoo_poller_active

            poller = get_yahoo_quote_poller()
            rec["alive"] = yahoo_poller_active()
            hub_yahoo = _yahoo_hub_fresh_count()
            if poller is not None:
                stats = poller.stats()
                rec["retry_count"] = int(stats.get("errors") or 0)
            if hub_yahoo >= 1:
                rec["health"] = "ok"
                tick_at = _yahoo_last_hub_tick_iso()
                if tick_at:
                    rec["last_tick_at"] = tick_at
                rec["alive"] = True
            elif poller is not None and int(poller.stats().get("published") or 0) > 0:
                rec["health"] = "ok"
                tick_at = _yahoo_last_hub_tick_iso()
                if tick_at:
                    rec["last_tick_at"] = tick_at
        except Exception as exc:
            rec["last_error"] = f"{type(exc).__name__}: {exc}"
    elif name in ("finnhub", "twelve_data"):
        try:
            from system.feeds.multi_feed_hub import feed_hub_telemetry

            tel = feed_hub_telemetry()
            key = "twelvedata" if name == "twelve_data" else name
            prov = (tel.get("providers") or {}).get(key) or {}
            rec["alive"] = bool(prov.get("alive"))
            rec["retry_count"] = int(prov.get("timeouts") or 0)
            if rec["alive"]:
                rec["health"] = "ok"
            elif int(prov.get("wins") or 0) > 0:
                rec["health"] = "degraded"
        except Exception as exc:
            rec["last_error"] = f"{type(exc).__name__}: {exc}"
    return rec


def _compose_state() -> dict[str, Any]:
    fresh, total, primary = _hub_fresh_counts()
    feeds = {name: _feed_record(name) for name in _PRIMARY_ORDER}
    yahoo_ok = feeds["yahoo"]["health"] == "ok"
    backup_ok = any(
        feeds[n]["health"] in ("ok", "degraded") for n in ("finnhub", "twelve_data")
    )
    if fresh >= 1 and (yahoo_ok or backup_ok):
        health = "ok"
    elif fresh >= 1 and not ig_used_for_signal_path():
        # Hub has non-IG quotes — primary path live even if poller telemetry lags.
        health = "ok" if fresh >= max(1, total - 1) else "degraded"
        if not yahoo_ok and _yahoo_hub_fresh_count() >= 1:
            feeds["yahoo"]["health"] = "ok"
            primary = primary or "yahoo"
    elif fresh >= 1:
        health = "degraded"
        # Hub may have Yahoo ticks before poller stats update — infer from snapshots.
        if not yahoo_ok:
            hub = get_market_data_hub()
            for epic in NIGHT_MATRIX_EPICS:
                snap = hub.get_snapshot(epic)
                if snap is None or snap.bid <= 0:
                    continue
                if snap.age_seconds() > _SIGNAL_MAX_AGE_SEC:
                    continue
                if "yahoo" in str(snap.source or "").lower():
                    yahoo_ok = True
                    feeds["yahoo"]["health"] = "ok"
                    primary = primary or "yahoo"
                    break
    elif yahoo_ok or backup_ok:
        health = "degraded"
    else:
        health = "offline"
    fallback = bool(primary and primary not in ("yahoo", "") and fresh >= 1)
    if not primary and fresh >= 1:
        primary = "yahoo" if yahoo_ok else "backup"
    last_error = str(_state.get("last_error") or "")
    retry_backoff_sec = 0.0
    try:
        from feeder.yahoo_quote_poller import yahoo_rate_limit_backoff_sec

        retry_backoff_sec = float(yahoo_rate_limit_backoff_sec())
    except Exception:
        pass
    if fresh >= total and total > 0:
        last_error = ""
    elif fresh >= max(1, total - 1) and health == "ok":
        # One epic may lag the poller — not a bootstrap failure once hub is live.
        # NOTE: caller (get_data_feed_state) already holds _lock, which is a
        # plain non-reentrant Lock — re-acquiring it here self-deadlocked the
        # uvicorn event loop the moment feeds hydrated, freezing every /api
        # endpoint a few minutes into each boot.
        last_error = ""
        _state["last_error"] = ""
    return {
        "ok": health == "ok",
        "ts": _utc_now_iso(),
        "primary_feed": primary or ("yahoo" if yahoo_ok else ""),
        "backup_feeds": [n for n in ("finnhub", "twelve_data") if feeds[n]["alive"]],
        "fallback_active": fallback,
        "health": health,
        "feeds": feeds,
        "fresh_count": fresh,
        "total_epics": total,
        "signal_path": "yahoo_first",
        "execution_path": "ig_only",
        "last_error": last_error,
        "retry_backoff_sec": round(retry_backoff_sec, 1),
        "retry_counts": {k: feeds[k]["retry_count"] for k in feeds},
        "started_at": _state.get("started_at") or "",
    }


def get_data_feed_state() -> dict[str, Any]:
    with _lock:
        return _compose_state()


def signal_feed_health_ok(*, min_fresh: int = 1) -> bool:
    body = get_data_feed_state()
    fresh = int(body.get("fresh_count") or 0)
    health = str(body.get("health") or "")
    if fresh < min_fresh:
        return False
    if health not in ("ok", "degraded"):
        return False
    if ig_used_for_signal_path():
        return False
    return bool(body.get("primary_feed")) or fresh >= min_fresh


def wait_for_signal_feed(*, timeout_sec: float = 12.0, min_fresh: int = 1) -> bool:
    """Block up to *timeout_sec* for a healthy non-IG primary feed (boot hydration)."""
    deadline = time.monotonic() + max(0.5, float(timeout_sec))
    while time.monotonic() < deadline:
        if signal_feed_health_ok(min_fresh=min_fresh):
            return True
        time.sleep(0.25)
    return signal_feed_health_ok(min_fresh=min_fresh)


def primary_feed_active() -> bool:
    body = get_data_feed_state()
    return bool(body.get("primary_feed")) and int(body.get("fresh_count") or 0) >= 1


def _bootstrap_yahoo_sync(epics: tuple[str, ...]) -> int:
    """Blocking first tick for stacked primary epics — unblocks cockpit without waiting poll cycle."""
    try:
        from feeder.yahoo_quote_poller import get_yahoo_quote_poller, start_yahoo_quote_poller
        from feeder.pricing_transport import yahoo_poll_seconds
        from system.config_loader import get_config

        cfg = get_config()
        poll_sec = yahoo_poll_seconds(cfg)
        timeout = float(os.environ.get("IG_YAHOO_BOOTSTRAP_TIMEOUT_SEC", "2.5"))
        poller = start_yahoo_quote_poller(epics, poll_sec=poll_sec, timeout_sec=timeout)
        published = 0
        attempts = int(os.environ.get("IG_YAHOO_BOOTSTRAP_ATTEMPTS", "3"))
        for attempt in range(max(1, attempts)):
            for epic in epics:
                if poller.poll_epic(epic) is not None:
                    published += 1
                if _BOOTSTRAP_EPIC_GAP_SEC > 0:
                    time.sleep(_BOOTSTRAP_EPIC_GAP_SEC)
            if published >= 1:
                break
            if attempt + 1 < attempts:
                time.sleep(0.4 * (attempt + 1))
        if published:
            with _lock:
                _state["last_error"] = ""
            log_engine(
                f"DataFeedOrchestrator: bootstrap sync published {published}/{len(epics)} "
                f"primary quote(s)"
            )
        else:
            with _lock:
                _state["last_error"] = "bootstrap: no ticks after sync retries"
            log_engine("DataFeedOrchestrator: bootstrap sync produced 0 quotes — retry loop armed")
        return published
    except Exception as exc:
        with _lock:
            _state["last_error"] = f"bootstrap: {type(exc).__name__}: {exc}"
        log_engine(f"DataFeedOrchestrator: bootstrap sync failed: {type(exc).__name__}: {exc}")
        return 0


def _retry_loop() -> None:
    base_interval = float(os.environ.get("IG_FEED_RETRY_INTERVAL_SEC", "15"))
    backoff = 1.0
    while not _retry_stop.wait(base_interval * backoff):
        try:
            body = get_data_feed_state()
            stale_quotes = False
            try:
                hub = get_market_data_hub()
                for epic in NIGHT_MATRIX_EPICS[:4]:
                    snap = hub.get_snapshot(epic)
                    if snap is not None and float(snap.bid or 0) > 0 and snap.age_seconds() > 20.0:
                        stale_quotes = True
                        break
            except Exception:
                pass
            if body.get("health") == "ok" and not stale_quotes:
                backoff = 1.0
                continue
            try:
                from system.chaos_guardian import (
                    compute_reconnect_delay,
                    notify_channel_disconnected,
                    should_delay_reconnect,
                )

                for ch in ("yahoo_feed", "finnhub_ws", "ig_stream"):
                    delayed, wait = should_delay_reconnect(ch)
                    if delayed:
                        continue
                    notify_channel_disconnected(ch, reason="feed_health_degraded")
                    backoff = compute_reconnect_delay(1)
                    _retry_stop.wait(min(backoff, 30.0))
            except Exception:
                backoff = min(4.0, backoff * 1.25)
            from feeder.pricing_transport import reference_transport_is_yahoo

            if reference_transport_is_yahoo():
                from feeder.yahoo_quote_poller import get_yahoo_quote_poller, start_yahoo_quote_poller
                from feeder.pricing_transport import yahoo_poll_seconds
                from system.config_loader import get_config

                cfg = get_config()
                if get_yahoo_quote_poller() is None or not body["feeds"]["yahoo"]["alive"]:
                    start_yahoo_quote_poller(
                        NIGHT_MATRIX_EPICS,
                        poll_sec=yahoo_poll_seconds(cfg),
                    )
            try:
                from system.feeds.multi_feed_hub import start_racing_multi_feed_hub

                start_racing_multi_feed_hub()
            except Exception:
                pass
        except Exception as exc:
            with _lock:
                _state["last_error"] = f"retry: {type(exc).__name__}: {exc}"


def start_data_feed_orchestrator(
    epics: list[str] | tuple[str, ...] | None = None,
    *,
    cfg: Any | None = None,
) -> None:
    """
    Staggered non-blocking feed startup:
    1) Yahoo poller + sync bootstrap (Wall St + Gold)
    2) Racing multi-feed hub (Finnhub / Twelve Data) on background thread
    3) Retry loop for degraded providers
    """
    global _started, _retry_thread
    universe = tuple(epics or NIGHT_MATRIX_EPICS)
    with _lock:
        if _started:
            try:
                from feeder.pricing_transport import reference_transport_is_yahoo, yahoo_poll_seconds
                from feeder.yahoo_quote_poller import start_yahoo_quote_poller

                if reference_transport_is_yahoo(cfg):
                    start_yahoo_quote_poller(
                        universe,
                        poll_sec=yahoo_poll_seconds(cfg),
                        timeout_sec=float(os.environ.get("IG_YAHOO_POLL_TIMEOUT_SEC", "1.5")),
                    )
            except Exception as exc:
                log_engine(
                    f"DataFeedOrchestrator: epics extend skipped: {type(exc).__name__}: {exc}"
                )
            return
        _started = True
        _state["started_at"] = _utc_now_iso()

    os.environ.setdefault("IG_PRICING_REFERENCE", "yahoo")
    bootstrap_epics = tuple(e for e in _BOOTSTRAP_EPICS if e in universe) or universe[:2]

    try:
        from feeder.pricing_transport import reference_transport_is_yahoo, yahoo_poll_seconds
        from feeder.yahoo_quote_poller import start_yahoo_quote_poller

        if reference_transport_is_yahoo(cfg):
            poll_sec = yahoo_poll_seconds(cfg)
            timeout = float(os.environ.get("IG_YAHOO_POLL_TIMEOUT_SEC", "1.5"))
            start_yahoo_quote_poller(universe, poll_sec=poll_sec, timeout_sec=timeout)
            log_engine(
                f"DataFeedOrchestrator: Yahoo primary armed "
                f"({len(universe)} epics poll={poll_sec}s timeout={timeout}s)"
            )

            def _bootstrap_bg() -> None:
                try:
                    _bootstrap_yahoo_sync(bootstrap_epics)
                except Exception as exc:
                    log_engine(
                        f"DataFeedOrchestrator: bootstrap bg failed: "
                        f"{type(exc).__name__}: {exc}"
                    )

            threading.Thread(
                target=_bootstrap_bg,
                name="feed-orchestrator-bootstrap",
                daemon=True,
            ).start()
    except Exception as exc:
        with _lock:
            _state["last_error"] = f"yahoo_start: {type(exc).__name__}: {exc}"
        log_engine(f"DataFeedOrchestrator: Yahoo start failed: {type(exc).__name__}: {exc}")

    def _arm_racing_hub() -> None:
        try:
            from system.feeds.multi_feed_hub import start_racing_multi_feed_hub

            start_racing_multi_feed_hub()
            log_engine("DataFeedOrchestrator: multi-feed racer armed (Finnhub/TwelveData)")
        except Exception as exc:
            log_engine(
                f"DataFeedOrchestrator: multi-feed hub skipped: {type(exc).__name__}: {exc}"
            )

    threading.Thread(target=_arm_racing_hub, name="feed-orchestrator-racing", daemon=True).start()

    if _retry_thread is None or not _retry_thread.is_alive():
        _retry_stop.clear()
        _retry_thread = threading.Thread(target=_retry_loop, name="feed-orchestrator-retry", daemon=True)
        _retry_thread.start()

    snap = get_data_feed_state()
    log_engine(
        f"DataFeedOrchestrator: started health={snap.get('health')} "
        f"fresh={snap.get('fresh_count')}/{snap.get('total_epics')} "
        f"primary={snap.get('primary_feed') or 'pending'}"
    )


def ensure_data_feed_orchestrator_running(
    epics: list[str] | tuple[str, ...] | None = None,
    *,
    cfg: Any | None = None,
) -> None:
    if not _started:
        start_data_feed_orchestrator(epics, cfg=cfg)


def reset_data_feed_orchestrator_for_tests() -> None:
    global _started, _retry_thread
    _retry_stop.set()
    _started = False
    _state["started_at"] = ""
    _state["last_error"] = ""
    _retry_thread = None


def ig_used_for_signal_path() -> bool:
    """Diagnostic — True if any fresh hub quote is IG-sourced (policy violation)."""
    hub = get_market_data_hub()
    for epic in NIGHT_MATRIX_EPICS:
        snap = hub.get_snapshot(epic)
        if snap is None or snap.bid <= 0:
            continue
        if snap.age_seconds() > _SIGNAL_MAX_AGE_SEC:
            continue
        src = str(snap.source or "").lower()
        if src in ("ig", "ig_rest", "rest", "stream", "lightstreamer", "rest_poll"):
            return True
    return False
