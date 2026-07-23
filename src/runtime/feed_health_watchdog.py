"""Non-blocking feed-health watchdog — entry veto + catastrophic flatten.

Runs as a daemon thread (async-safe from the tick lane). When live quote age
exceeds ``QUOTE_STALE_SEC``, ``system_state.is_healthy`` flips False and new
entries are hard-blocked. Open risk is flattened via the single exit gate and
the multi-feed / Lightstreamer container is hard-reset.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from system.engine_log import log_engine

QUOTE_STALE_SEC = 5.0
# Lightstreamer / WS ideal; rest_poll hosts use transport budget (~10s).
OPERATIONAL_QUOTE_AGE_SEC = 0.5
POLL_SEC = 1.0
RESET_COOLDOWN_SEC = 30.0
FLATTEN_COOLDOWN_SEC = 15.0


def _transport_quote_budget_sec() -> float:
    """Transport-aware freshness ceiling for operational badge + soft health."""
    try:
        from system.market_integrity import effective_entry_quote_budget_sec

        return max(OPERATIONAL_QUOTE_AGE_SEC, float(effective_entry_quote_budget_sec()))
    except Exception:
        return OPERATIONAL_QUOTE_AGE_SEC


def _stale_threshold_sec() -> float:
    """Catastrophic stale threshold — never tighter than transport budget on Mini."""
    return max(QUOTE_STALE_SEC, _transport_quote_budget_sec())


class SystemHealthState:
    """In-RAM health bitmask — no disk I/O on the hot path."""

    __slots__ = (
        "is_healthy",
        "quote_age_sec",
        "last_reason",
        "last_reset_at",
        "last_flatten_at",
        "entries_blocked",
        "updated_at",
        "warming_since",
    )

    def __init__(self) -> None:
        self.is_healthy = True
        self.quote_age_sec: float | None = None
        self.last_reason = ""
        self.last_reset_at = 0.0
        self.last_flatten_at = 0.0
        self.entries_blocked = False
        self.updated_at = 0.0
        self.warming_since = 0.0

    def snapshot(self) -> dict[str, Any]:
        budget = _transport_quote_budget_sec()
        age = self.quote_age_sec
        operational = bool(
            self.is_healthy
            and age is not None
            and float(age) < budget
        )
        # Prefer sub-500ms emerald when transport can actually deliver it.
        if (
            operational
            and age is not None
            and float(age) < OPERATIONAL_QUOTE_AGE_SEC
        ):
            operational = True
        return {
            "is_healthy": bool(self.is_healthy),
            "quote_age_sec": self.quote_age_sec,
            "quote_age_ms": (
                None
                if self.quote_age_sec is None
                else round(float(self.quote_age_sec) * 1000.0, 1)
            ),
            "quote_budget_sec": budget,
            "last_reason": self.last_reason,
            "entries_blocked": bool(self.entries_blocked),
            "operational_badge": operational,
            "updated_at": self.updated_at,
        }


_STATE = SystemHealthState()
_STATE_LOCK = threading.RLock()
_STOP = threading.Event()
_THREAD: threading.Thread | None = None
_STARTED = False


def get_system_health_state() -> SystemHealthState:
    return _STATE


def is_system_healthy() -> bool:
    with _STATE_LOCK:
        return bool(_STATE.is_healthy)


def entries_blocked_by_feed_health() -> bool:
    """True only when the watchdog still believes feeds are unhealthy.

    If MarketDataHub / data-feed state already shows a full fresh set, clear a
    sticky block left behind by a transient stale tick + stream reset gap
    (age=None warming used to refresh updated_at forever and never recover).
    """
    with _STATE_LOCK:
        blocked = bool(_STATE.entries_blocked) or not bool(_STATE.is_healthy)
    if not blocked:
        return False
    try:
        from system.market_data_hub import get_market_data_hub

        hub = get_market_data_hub()
        fresh = 0
        total = 0
        for epic in (
            "IX.D.DOW.IFM.IP",
            "IX.D.NIKKEI.IFM.IP",
            "CS.D.CFPGOLD.CFP.IP",
            "CS.D.EURUSD.CFD.IP",
            "IX.D.FTSE.IFM.IP",
            "IX.D.DAX.IFM.IP",
            "CS.D.CRUDE.CFD.IP",
        ):
            total += 1
            q = hub.get_snapshot(epic)
            if q is None:
                continue
            try:
                if float(q.age_seconds()) <= _stale_threshold_sec():
                    fresh += 1
            except Exception:
                continue
        if total > 0 and fresh >= max(3, total - 1):
            _mark_healthy(_resolve_quote_age_sec())
            return False
    except Exception:
        pass
    return True


def system_health_snapshot() -> dict[str, Any]:
    with _STATE_LOCK:
        return _STATE.snapshot()


def reset_feed_health_watchdog_for_tests() -> None:
    global _STARTED, _THREAD
    stop_feed_health_watchdog()
    with _STATE_LOCK:
        _STATE.is_healthy = True
        _STATE.quote_age_sec = None
        _STATE.last_reason = ""
        _STATE.last_reset_at = 0.0
        _STATE.last_flatten_at = 0.0
        _STATE.entries_blocked = False
        _STATE.updated_at = 0.0
        _STATE.warming_since = 0.0
    _STARTED = False


def _resolve_quote_age_sec() -> float | None:
    """Best-effort live quote age — never blocks on disk.

    Prefer MarketDataHub / fulfillment ages over memory_context. The unhealthy
    path used to write age=999 into memory_context, which then poisoned every
    subsequent tick (permanent entry block + stream-reset storm).
    """
    ages: list[float] = []
    try:
        from system.market_data_hub import get_market_data_hub

        hub = get_market_data_hub()
        for epic in (
            "IX.D.DOW.IFM.IP",
            "IX.D.FTSE.IFM.IP",
            "CS.D.CFPGOLD.CFP.IP",
            "CS.D.EURUSD.CFD.IP",
            "IX.D.NIKKEI.IFM.IP",
        ):
            q = hub.get_snapshot(epic)
            if q is None:
                continue
            try:
                ages.append(float(q.age_seconds()))
            except Exception:
                continue
    except Exception:
        pass
    if ages:
        return min(ages)

    try:
        from system.unified_fulfillment_cache import get_fulfillment_snapshot

        snap = get_fulfillment_snapshot() or {}
        qf = snap.get("quote_freshness") or {}
        if qf.get("age_sec") is not None:
            return float(qf["age_sec"])
        if snap.get("quote_age_sec") is not None:
            return float(snap["quote_age_sec"])
    except Exception:
        pass

    try:
        from system.memory_context import get_memory_context

        age = get_memory_context().quote_age_sec()
        # Ignore poisoned sentinel ages written by a prior unhealthy mark.
        if age is not None and float(age) < 100.0:
            return float(age)
    except Exception:
        pass
    return None


def _is_rest_coalesce_error(exc: BaseException | str) -> bool:
    text = str(exc)
    return "positions_coalesce_pressure" in text or "REST deferred" in text


def _mark_unhealthy(age: float | None, reason: str) -> None:
    with _STATE_LOCK:
        _STATE.is_healthy = False
        _STATE.entries_blocked = True
        _STATE.quote_age_sec = age
        _STATE.last_reason = reason
        _STATE.updated_at = time.time()
    # Do NOT write a 999s poison age into memory_context — that locks the desk.


def _mark_healthy(age: float | None) -> None:
    with _STATE_LOCK:
        _STATE.is_healthy = True
        _STATE.entries_blocked = False
        _STATE.quote_age_sec = age
        _STATE.last_reason = "feed_ok"
        _STATE.updated_at = time.time()
    try:
        from system.memory_context import get_memory_context

        if age is not None:
            get_memory_context().set_quote_freshness(age)
    except Exception:
        pass


def _hard_reset_streams(reason: str) -> None:
    with _STATE_LOCK:
        last = float(_STATE.last_reset_at)
    now = time.time()
    if now - last < RESET_COOLDOWN_SEC:
        return
    with _STATE_LOCK:
        _STATE.last_reset_at = now
    try:
        from system.feeds.multi_feed_hub import hard_reset_multi_feed_hub

        hard_reset_multi_feed_hub(reason=reason)
    except Exception as exc:
        log_engine(f"FeedHealthWatchdog: multi-feed reset failed: {type(exc).__name__}: {exc}")
    # Transport-aware: Lightstreamer / streaming container when present
    # (Mac Mini DEMO typically uses rest_poll — multi-feed hard reset is primary).
    try:
        from system.market_data_hub import get_market_data_hub

        hub = get_market_data_hub()
        for name in (
            "hard_reset_streaming",
            "reset_lightstreamer",
            "reconnect_streaming",
            "force_reconnect",
        ):
            fn = getattr(hub, name, None)
            if not callable(fn):
                continue
            try:
                fn(reason=reason)
            except TypeError:
                fn()
            break
    except Exception as exc:
        log_engine(
            f"FeedHealthWatchdog: stream container reset skipped: {type(exc).__name__}"
        )


def _catastrophic_flatten(reason: str) -> dict[str, Any]:
    """Bypass indicators — single exit gate + REST market close for all opens."""
    with _STATE_LOCK:
        last = float(_STATE.last_flatten_at)
    now = time.time()
    if now - last < FLATTEN_COOLDOWN_SEC:
        return {"ok": False, "skipped": "cooldown"}
    with _STATE_LOCK:
        _STATE.last_flatten_at = now

    report: dict[str, Any] = {"ok": True, "closed": [], "errors": [], "reason": reason}
    try:
        from system.config_loader import load_active_config
        from system.credentials_loader import try_load_credentials
        from system.ig_rest_session import ensure_shared_authenticated
        from execution.exit_execution_gate import request_flatten

        status = try_load_credentials()
        if not status.ok or status.credentials is None:
            return {"ok": False, "error": "no_credentials"}
        cfg = load_active_config(validate=False)
        rest = ensure_shared_authenticated(status.credentials)
        items = list(rest.open_positions(budget_priority=True) or [])
        if not items:
            report["broker_open"] = 0
            return report
        for item in items:
            pos = item.get("position") or {}
            mkt = item.get("market") or {}
            deal_id = str(pos.get("dealId") or "").strip()
            epic = str(mkt.get("epic") or "").strip()
            direction = str(pos.get("direction") or "BUY").upper()
            size = float(pos.get("size") or 0)
            if not deal_id or size <= 0:
                continue
            result = request_flatten(
                rest=rest,
                deal_id=deal_id,
                epic=epic,
                direction=direction,
                size=size,
                reason=f"feed_health:{reason}",
                pnl_gbp=None,
                cfg=cfg,
                source="feed_health_watchdog",
            )
            if result.get("ok"):
                report["closed"].append(deal_id)
            else:
                report["errors"].append(
                    f"{deal_id}:{result.get('error') or result.get('reason') or 'fail'}"
                )
        if not report["closed"] and hasattr(rest, "flatten_all_positions"):
            try:
                report["flatten_all_fallback"] = int(rest.flatten_all_positions() or 0)
            except Exception as exc:
                report["errors"].append(f"flatten_all:{type(exc).__name__}:{exc}")
        report["broker_open_after"] = len(
            list(rest.open_positions(budget_priority=True) or [])
        )
    except Exception as exc:
        report["ok"] = False
        report["errors"].append(f"{type(exc).__name__}:{exc}")
        log_engine(f"FeedHealthWatchdog: flatten failed: {exc}")
    log_engine(
        f"FeedHealthWatchdog: catastrophic flatten reason={reason} "
        f"closed={len(report.get('closed') or [])} errors={len(report.get('errors') or [])}"
    )
    return report


def mark_orchestrator_fault(reason: str) -> None:
    """Fail-closed health mark from v33 orchestrator — no flatten / stream reset."""
    age = _resolve_quote_age_sec()
    _mark_unhealthy(age, f"orchestrator:{reason[:120]}")


def note_api_error(exc: BaseException | str, *, flatten: bool = True) -> None:
    """Call from unhandled API paths — marks unhealthy and may flatten."""
    reason = f"api_error:{type(exc).__name__ if isinstance(exc, BaseException) else exc}"
    age = _resolve_quote_age_sec()
    _mark_unhealthy(age, reason)
    if flatten:
        try:
            from system.credentials_loader import try_load_credentials
            from system.ig_rest_session import ensure_shared_authenticated

            status = try_load_credentials()
            if status.ok and status.credentials is not None:
                rest = ensure_shared_authenticated(status.credentials)
                opens = list(rest.open_positions(budget_priority=True) or [])
                if opens:
                    _catastrophic_flatten(reason)
        except Exception:
            pass
    _hard_reset_streams(reason)


def _tick_once() -> None:
    age = _resolve_quote_age_sec()
    if age is None:
        # No quote telemetry yet — warm without sticky-blocking forever.
        # Do NOT refresh warming_since each tick (that never expired).
        now = time.time()
        with _STATE_LOCK:
            if _STATE.warming_since <= 0.0:
                _STATE.warming_since = now
            warming = (now - _STATE.warming_since) < 15.0
            _STATE.quote_age_sec = None
            _STATE.last_reason = "warming"
            _STATE.updated_at = now
        if warming:
            return
        _mark_unhealthy(None, "no_quote_telemetry")
        _hard_reset_streams("no_quote_telemetry")
        return

    with _STATE_LOCK:
        _STATE.warming_since = 0.0

    stale_at = _stale_threshold_sec()
    if float(age) > stale_at:
        _mark_unhealthy(float(age), f"quote_age>{stale_at:.1f}s")
        try:
            from system.credentials_loader import try_load_credentials
            from system.ig_rest_session import ensure_shared_authenticated

            status = try_load_credentials()
            if status.ok and status.credentials is not None:
                rest = ensure_shared_authenticated(status.credentials)
                if list(rest.open_positions(budget_priority=True) or []):
                    _catastrophic_flatten(f"quote_stale_{age:.1f}s")
        except Exception as exc:
            log_engine(f"FeedHealthWatchdog: open-check failed: {type(exc).__name__}")
            # REST coalesce / deferred is not a feed failure — do not poison
            # health or storm stream resets (was locking the desk permanently).
            if not _is_rest_coalesce_error(exc):
                note_api_error(exc, flatten=False)
        _hard_reset_streams(f"quote_stale_{age:.1f}s")
        return

    _mark_healthy(float(age))


def _loop() -> None:
    while not _STOP.is_set():
        try:
            _tick_once()
        except Exception as exc:
            log_engine(f"FeedHealthWatchdog: tick error {type(exc).__name__}: {exc}")
            try:
                note_api_error(exc, flatten=True)
            except Exception:
                pass
        _STOP.wait(POLL_SEC)


def start_feed_health_watchdog() -> None:
    """Arm the non-blocking watchdog (idempotent)."""
    global _STARTED, _THREAD
    if _STARTED and _THREAD is not None and _THREAD.is_alive():
        return
    _STOP.clear()
    _THREAD = threading.Thread(
        target=_loop,
        name="feed-health-watchdog",
        daemon=True,
    )
    _THREAD.start()
    _STARTED = True
    log_engine(
        f"FeedHealthWatchdog: armed (stale>{QUOTE_STALE_SEC}s → block+flatten+stream reset)"
    )


def stop_feed_health_watchdog() -> None:
    global _STARTED, _THREAD
    _STOP.set()
    t = _THREAD
    if t is not None and t.is_alive():
        t.join(timeout=2.0)
    _THREAD = None
    _STARTED = False
